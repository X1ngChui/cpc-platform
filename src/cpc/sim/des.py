"""Discrete-event simulator.

The simulator drives the same data-centre runtime as the site agent (cpc.datacenter) on a simulated
clock and calls the same global policy interfaces as the gateway and the controller, so scenarios,
datasets, server models and global and local policies are shared between simulation and live
experiments. Specific to the simulator:
- clock and events: request arrival (streamed in trace order), arrival at the site (after the
  one-way network delay), completion, end of booting, control period;
- execution: prefill and decode times from the server performance model;
- all servers are emulated (real pools with their profile).

Input completeness follows the live controller: when the capacity policy requires grid signals and a
site has none, the control period is skipped and counted. Cost and carbon are accounted only for
time bins with signals; the summary gives the covered share of facility energy.

Time unit: simulated seconds from scenario clock.start.
"""

from __future__ import annotations

import heapq
import math
from collections import Counter
from datetime import datetime, timedelta
from enum import IntEnum
from functools import partial

import numpy as np

from ..config import ConfigError, Scenario
from ..datacenter import Action, Datacenter, Job, Overloaded, Server
from ..datasets.workload import WorkloadTrace, assign_sources, load_workload
from ..grid.signals import GridModel, GridSignal, SignalUnavailable
from ..models import prefill_s, rtt_matrix, tpot_s
from ..results import RunResult
from ..schemas import DemandSnapshot
from ..sched import CapacityPolicy, ClusterView, ControlView, RequestInfo, RoutingPolicy, SiteView
from ..sched.local import make_dispatch, make_power

SIGNAL_REFRESH_S = 60.0  # refresh period of grid signals (simulated s)


class Ev(IntEnum):
    SITE_ARRIVE = 0
    FINISH = 1
    BOOT = 2
    CONTROL = 3


class St(IntEnum):
    """Request status."""

    PENDING = 0
    SERVED = 1
    REJECTED = 2
    UNROUTED = 3


STATUS = {St.PENDING: "unfinished", St.SERVED: "served", St.REJECTED: "rejected", St.UNROUTED: "unrouted"}


class Simulator:
    def __init__(self, scn: Scenario, routing: RoutingPolicy, capacity: CapacityPolicy, hours: float = 24.0,
                 bin_s: float = 300.0, control_interval_s: float | None = None, initial_active: int | None = None,
                 trace: WorkloadTrace | None = None):
        self.scn = scn
        self.routing = routing
        self.capacity = capacity
        self.horizon = hours * 3600.0
        self.bin_s = bin_s
        self.ctl_s = control_interval_s or scn.scheduler.sim_control_interval_s
        self.grid = GridModel(scn)
        self.trace = trace if trace is not None else load_workload(scn)
        w = scn.workload
        span = self.trace.duration_s if w.duration_s is None else w.duration_s / w.time_scale
        if span + 1e-6 < self.horizon:
            raise ConfigError(f"workload {self.trace.ref} covers only {span / 3600:.2f} h, less than the simulated "
                              f"{hours:g} h; set the duration (--hours or hours of the experiment) to at most {span / 3600:.2f}")
        self.rtt = rtt_matrix(scn)
        self.sites = {s.id: Datacenter(
            s, scn.node_profiles, dispatch=make_dispatch(s.scheduler.dispatch.policy, s.scheduler.dispatch.params),
            power=make_power(s.scheduler.power.policy, s.scheduler.power.params),
            queue_limit_per_slot=scn.scheduler.queue_limit_per_slot, rate_window_s=scn.scheduler.rate_window_s,
            now=0.0, initial_active=initial_active, emulate_all=True,
            on_start=partial(self.on_start, s.id), on_action=partial(self.on_action, s.id)) for s in scn.sites}
        self.site_ids = list(self.sites)
        self.site_index = {sid: k for k, sid in enumerate(self.site_ids)}
        self.inflight = Counter()  # routed requests still on the wide-area network
        self.weights: dict[str, dict[str, float]] = {}
        self.ev: list = []
        self.seq = 0
        # request records, in trace order
        self.n = n = int(np.searchsorted(self.trace.t, self.horizon, side="left"))
        self.src_ids = [s.id for s in scn.sources]
        self.src = assign_sources(n, self.src_ids, [s.share for s in scn.sources], w.seed)
        self.r_status = np.zeros(n, dtype=np.int8)
        self.r_site = np.full(n, -1, dtype=np.int16)
        self.r_wait, self.r_ttft, self.r_e2e, self.r_tpot = (np.full(n, np.nan) for _ in range(4))
        self.r_server: list[str | None] = [None] * n
        self.r_reason: list[str] = [""] * n
        self.src_window: Counter[str] = Counter()
        self.nb = math.ceil(self.horizon / bin_s)
        self.bin_energy = {sid: np.zeros(self.nb) for sid in self.sites}  # J (IT)
        self.bin_live = {sid: np.zeros(self.nb) for sid in self.sites}  # server-seconds not off
        self.bin_arrivals = {sid: np.zeros(self.nb) for sid in self.sites}
        self._meter_last = dict.fromkeys(self.sites, (0.0, 0.0))
        self.decisions: list[dict] = []
        self.route_reasons: Counter[str] = Counter()
        self.skipped_cycles: Counter[str] = Counter()
        self.signals: dict[str, GridSignal] = {}

    # ------------------------------------------------------------ helpers

    def push(self, t: float, kind: Ev, data) -> None:
        self.seq += 1
        heapq.heappush(self.ev, (t, self.seq, kind, data))

    def dt(self, t: float) -> datetime:
        return self.scn.clock.start + timedelta(seconds=t)

    def close_bin(self, b: int) -> None:
        """End of time bin b: energy and online server time of the bin from the differences of the site meters."""
        t = min((b + 1) * self.bin_s, self.horizon)
        for sid, dc in self.sites.items():
            m = dc.meter(t)
            e0, l0 = self._meter_last[sid]
            self.bin_energy[sid][b] = m.energy_j - e0
            self.bin_live[sid][b] = m.live_s - l0
            self._meter_last[sid] = (m.energy_j, m.live_s)

    # ------------------------------------------------------------ events

    def view(self, t: float) -> ClusterView:
        sv = {sid: SiteView(snap=dc.summary(t, detail=False), signal=self.signals.get(sid), pending=self.inflight[sid])
              for sid, dc in self.sites.items()}
        return ClusterView(now=self.dt(t), sites=sv, rtt_ms=self.rtt, slo=self.scn.slo, weights=self.weights)

    def info(self, i: int) -> RequestInfo:
        tr = self.trace
        return RequestInfo(source=str(self.src[i]), input_tokens=int(tr.input_tokens[i]),
                           output_tokens=max(1, int(tr.output_tokens[i])), id=str(i), attrs=tr.attrs_of(i) if tr.attrs else {})

    def on_arrive(self, t: float, i: int) -> None:
        req = self.info(i)
        self.src_window[req.source] += 1
        decision = self.routing.choose(req, self.view(t))
        self.route_reasons[decision.reason] += 1
        self.r_reason[i] = decision.reason
        if (sid := decision.site) is None:
            self.r_status[i] = St.UNROUTED
            return
        self.inflight[sid] += 1
        self.r_site[i] = self.site_index[sid]
        self.push(t + self.rtt[req.source][sid] / 2000.0, Ev.SITE_ARRIVE, (sid, i, req))

    def on_site_arrive(self, t: float, data) -> None:
        sid, i, req = data
        self.inflight[sid] -= 1
        if (b := int(t // self.bin_s)) < self.nb:
            self.bin_arrivals[sid][b] += 1
        try:
            self.sites[sid].submit(Job(req, i), t)
        except Overloaded:
            self.r_status[i] = St.REJECTED

    def on_start(self, sid: str, job: Job, server: Server, now: float) -> None:
        i, req, p = job.payload, job.info, server.profile
        pre = prefill_s(p, req.input_tokens, server.cap_ratio)
        tp = tpot_s(p, server.occupancy, server.cap_ratio)
        svc = pre + req.output_tokens * tp
        half_rtt = self.rtt[req.source][sid] / 2000.0
        t_arr = float(self.trace.t[i])
        self.r_wait[i] = 0.0 if job.t_queued is None else now - job.t_queued
        self.r_ttft[i] = now + pre + tp + half_rtt - t_arr
        self.r_e2e[i] = now + svc + half_rtt - t_arr
        self.r_tpot[i] = tp
        self.r_server[i] = server.id
        self.r_status[i] = St.SERVED
        self.push(now + svc, Ev.FINISH, (sid, server, svc))

    def on_action(self, sid: str, a: Action) -> None:
        if a.kind == "power_on":
            self.push(a.server.boot_until, Ev.BOOT, (sid, a.server, a.server.boot_until))

    def on_finish(self, t: float, data) -> None:
        sid, server, svc = data
        self.sites[sid].finish(server, t, svc)

    def on_boot(self, t: float, data) -> None:
        sid, server, until = data
        if server.state == "booting" and server.boot_until == until:  # stale after a cancelled and repeated boot
            self.sites[sid].boot_complete(server, t)

    def on_control(self, t: float) -> None:
        for dc in self.sites.values():
            dc.update_rate(t)
        rates = {k: self.src_window[k] / self.ctl_s for k in self.src_ids}
        self.src_window.clear()
        if t + self.ctl_s < self.horizon:
            self.push(t + self.ctl_s, Ev.CONTROL, None)
        if "signals" in self.capacity.requires and len(self.signals) < len(self.sites):
            self.skipped_cycles["incomplete_signals"] += 1
            return
        view = ControlView(now=self.dt(t), interval_s=self.ctl_s, sites={k: dc.summary(t) for k, dc in self.sites.items()},
                           signals=self.signals, demand=DemandSnapshot(rates=rates), rtt_ms=self.rtt, slo=self.scn.slo,
                           min_active={s.id: s.min_active for s in self.scn.sites})
        plan = self.capacity.decide(view)
        for sid, tgt in plan.targets.items():
            self.sites[sid].apply(tgt, t)
        if plan.weights is not None:
            self.weights = plan.weights
        self.decisions.append({"t": t, "targets": {k: v.model_dump() for k, v in plan.targets.items()}, "notes": plan.notes})

    # ------------------------------------------------------------ main loop

    def run(self) -> RunResult:
        self.push(0.0, Ev.CONTROL, None)
        arr, ev = self.trace.t, self.ev
        i, b = 0, 0
        sig_t = -math.inf  # the first event refreshes the signals, so decisions have complete inputs
        while True:
            t_ev = ev[0][0] if ev else math.inf
            t_arr = float(arr[i]) if i < self.n else math.inf
            if (t := min(t_ev, t_arr)) >= self.horizon:
                break
            while (b + 1) * self.bin_s <= t:
                self.close_bin(b)
                b += 1
            if t - sig_t >= SIGNAL_REFRESH_S:
                self.signals, _ = self.grid.snapshot(self.dt(t))
                sig_t = t
            if t_arr <= t_ev:
                self.on_arrive(t, i)
                i += 1
                continue
            _, _, kind, data = heapq.heappop(ev)
            match kind:
                case Ev.SITE_ARRIVE:
                    self.on_site_arrive(t, data)
                case Ev.FINISH:
                    self.on_finish(t, data)
                case Ev.BOOT:
                    self.on_boot(t, data)
                case Ev.CONTROL:
                    self.on_control(t)
        for b in range(b, self.nb):
            self.close_bin(b)
        return self.result()

    def _signal(self, sid: str, t: float) -> GridSignal | None:
        try:
            return self.grid.signal(sid, self.dt(t))
        except SignalUnavailable:
            return None

    def result(self) -> RunResult:
        keys = ("it_kwh", "facility_kwh", "cost", "carbon_kg")
        ts, tot = [], dict.fromkeys(keys, 0.0)
        per_site = {sid: {**dict.fromkeys(keys, 0.0), "requests": 0} for sid in self.sites}
        priced = 0.0  # facility energy with a grid signal (kWh)
        for b in range(self.nb):
            t0 = b * self.bin_s
            width = min(self.bin_s, self.horizon - t0)
            row = {"t_h": round(t0 / 3600, 4), "time": self.dt(t0).isoformat()}
            for sid, dc in self.sites.items():
                it_kwh = self.bin_energy[sid][b] / 3.6e6
                fac = it_kwh * dc.cfg.pue
                g = self._signal(sid, t0 + width / 2)
                for d in (tot, per_site[sid]):
                    d["it_kwh"] += it_kwh
                    d["facility_kwh"] += fac
                    if g is not None:
                        d["cost"] += fac * g.price
                        d["carbon_kg"] += fac * g.carbon_intensity
                if g is not None:
                    priced += fac
                row |= {f"{sid}_it_kw": round(self.bin_energy[sid][b] / width / 1000, 4),
                        f"{sid}_nodes": round(self.bin_live[sid][b] / width, 3),
                        f"{sid}_rps": round(self.bin_arrivals[sid][b] / width, 4),
                        f"{sid}_price": None if g is None else g.price,
                        f"{sid}_carbon": None if g is None else g.carbon_intensity,
                        f"{sid}_cap_kw": None if g is None else g.power_cap_kw}
            ts.append(row)
        slo = self.scn.slo
        served = self.r_status == St.SERVED
        ok = served & (self.r_ttft <= slo.ttft_s) & (self.r_tpot <= slo.tpot_s)
        ttft, e2e = self.r_ttft[served], self.r_e2e[served]
        for sid, k in self.site_index.items():
            per_site[sid]["requests"] = int(np.sum(served & (self.r_site == k)))
        ns = int(served.sum())
        pct = lambda a, q: float(np.percentile(a, q)) if ns else None  # noqa: E731
        summary = {
            "routing": self.routing.name, "capacity": self.capacity.name, "hours": self.horizon / 3600,
            "workload": self.trace.summary(), "grid": self.grid.sources(), "currency": self.grid.currency,
            "rps": round(self.n / self.horizon, 4), "arrived": self.n,
            **{name: int(np.sum(self.r_status == k)) for k, name in STATUS.items()},
            "slo_attainment": float(ok.sum() / ns) if ns else 0.0,
            "ttft_p50_s": pct(ttft, 50), "ttft_p99_s": pct(ttft, 99), "e2e_p50_s": pct(e2e, 50),
            **{k: round(v, 4) for k, v in tot.items()},
            "wh_per_request": round(tot["facility_kwh"] * 1000 / max(ns, 1), 4),
            "sites": {k: {kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in per_site.items()},
            "route_reasons": dict(sorted(self.route_reasons.items())),
            "skipped_cycles": dict(self.skipped_cycles),
            "signal_coverage": round(priced / tot["facility_kwh"], 4) if tot["facility_kwh"] else None,
        }
        site_names = np.array([*self.site_ids, ""], dtype=object)
        n = self.n
        reqs = {
            "arrival_s": self.trace.t[:n], "source": self.src, "site": site_names[self.r_site],
            "server": np.array(self.r_server, dtype=object),
            "status": np.array([STATUS[St(s)] for s in self.r_status], dtype=object),
            "route_reason": np.array(self.r_reason, dtype=object), "input_tokens": self.trace.input_tokens[:n],
            "output_tokens": self.trace.output_tokens[:n], "queue_s": self.r_wait, "ttft_s": self.r_ttft,
            "e2e_s": self.r_e2e, "tpot_s": self.r_tpot, "slo_ok": ok,
        }
        return RunResult(summary, ts, reqs, self.decisions)
