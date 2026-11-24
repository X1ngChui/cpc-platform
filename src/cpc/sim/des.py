"""离散事件仿真器。

仿真器以仿真时钟驱动与站点代理相同的数据中心运行时（cpc.datacenter），并调用与网关、控制器相同的全局策略接口，
因此场景、数据集、服务器模型、全局与本地策略在仿真与实验中完全共用。仿真器特有的部分：
- 时钟与事件：请求到达（按负载 trace 顺序流式读取）、到达站点（经单程网络时延）、服务完成、服务器启动完成、控制周期；
- 请求执行：按服务器性能模型计算预填充与解码时间；
- 全部服务器按服务器模型仿真（真实服务器池按其 profile 仿真）。

输入完整性与在线控制器一致：容量策略声明需要电网信号而某些站点的信号不可用时，跳过该控制周期并计数；
成本与碳排放只对信号可用的时间片核算，汇总中给出按设施电量计的覆盖率。

时间单位：仿真秒，零点为场景 clock.start。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

import numpy as np

from ..config import Scenario
from ..datacenter import Action, Datacenter, Job, Overloaded, Server
from ..datasets.workload import WorkloadTrace, assign_sources, load_workload
from ..grid.signals import GridModel, GridSignal, SignalUnavailable
from ..models import prefill_s, rtt_matrix, tpot_s
from ..schemas import DemandSnapshot
from ..sched import CapacityPolicy, ClusterView, ControlView, RequestInfo, RoutingPolicy, SiteView
from ..sched.local import make_dispatch, make_power

SITE_ARRIVE, FINISH, BOOT, CONTROL = range(4)
SIGNAL_REFRESH_S = 60.0  # 电网信号刷新间隔（仿真秒）

# 请求状态
PENDING, SERVED, REJECTED, UNROUTED = 0, 1, 2, 3
STATUS = {PENDING: "unfinished", SERVED: "served", REJECTED: "rejected", UNROUTED: "unrouted"}


@dataclass
class SimResult:
    summary: dict
    timeseries: list[dict]
    requests: dict[str, np.ndarray]
    decisions: list[dict]


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
        span = self.trace.duration_s if scn.workload.duration_s is None else scn.workload.duration_s / scn.workload.time_scale
        if span + 1e-6 < self.horizon:
            raise ValueError(f"负载 {self.trace.ref} 变换后覆盖 {span / 3600:.2f} h，短于仿真时长 {hours} h")
        self.rtt = rtt_matrix(scn)
        self.sites: dict[str, Datacenter] = {}
        for s in scn.sites:
            self.sites[s.id] = Datacenter(
                s, scn.node_profiles, dispatch=make_dispatch(s.scheduler.dispatch.policy, s.scheduler.dispatch.params),
                power=make_power(s.scheduler.power.policy, s.scheduler.power.params),
                queue_limit_per_slot=scn.scheduler.queue_limit_per_slot, rate_window_s=scn.scheduler.rate_window_s,
                now=0.0, initial_active=initial_active, emulate_all=True,
                on_start=partial(self.on_start, s.id), on_action=partial(self.on_action, s.id))
        self.site_ids = list(self.sites)
        self.inflight = {sid: 0 for sid in self.sites}  # 已路由、尚在广域网上的请求
        self.weights: dict[str, dict[str, float]] = {}
        self.ev: list = []
        self.seq = 0
        # 请求记录（按 trace 中的顺序）
        n = int(np.searchsorted(self.trace.t, self.horizon, side="left"))
        self.n = n
        self.src_ids = [s.id for s in scn.sources]
        self.src = assign_sources(n, self.src_ids, [s.share for s in scn.sources], scn.workload.seed)
        self.r_status = np.zeros(n, dtype=np.int8)
        self.r_site = np.full(n, -1, dtype=np.int16)
        self.r_wait = np.full(n, np.nan)
        self.r_ttft = np.full(n, np.nan)
        self.r_e2e = np.full(n, np.nan)
        self.r_tpot = np.full(n, np.nan)
        self.r_server: list[str | None] = [None] * n
        self.r_reason: list[str] = [""] * n
        self.src_window = {k: 0 for k in self.src_ids}
        nb = int(math.ceil(self.horizon / bin_s))
        self.nb = nb
        self.bin_energy = {sid: np.zeros(nb) for sid in self.sites}  # J（IT）
        self.bin_live = {sid: np.zeros(nb) for sid in self.sites}  # 服务器·秒（非关机）
        self.bin_arrivals = {sid: np.zeros(nb) for sid in self.sites}
        self._meter_last = {sid: (0.0, 0.0) for sid in self.sites}
        self.decisions: list[dict] = []
        self.route_reasons: dict[str, int] = {}
        self.skipped_cycles: dict[str, int] = {}
        self.signals: dict[str, GridSignal] = {}

    # ------------------------------------------------------------ 工具

    def push(self, t: float, kind: int, data) -> None:
        self.seq += 1
        heapq.heappush(self.ev, (t, self.seq, kind, data))

    def dt(self, t: float) -> datetime:
        return self.scn.clock.start + timedelta(seconds=t)

    def close_bin(self, b: int) -> None:
        """时间片 b 结束：按各站点的累计计量差分得到该时间片的能耗与在线服务器时长。"""
        t = min((b + 1) * self.bin_s, self.horizon)
        for sid, dc in self.sites.items():
            m = dc.meter(t)
            e0, l0 = self._meter_last[sid]
            self.bin_energy[sid][b] = m.energy_j - e0
            self.bin_live[sid][b] = m.live_s - l0
            self._meter_last[sid] = (m.energy_j, m.live_s)

    # ------------------------------------------------------------ 事件处理

    def view(self, t: float) -> ClusterView:
        sv = {sid: SiteView(snap=dc.summary(t, detail=False), signal=self.signals.get(sid), pending=self.inflight[sid])
              for sid, dc in self.sites.items()}
        return ClusterView(now=self.dt(t), sites=sv, rtt_ms=self.rtt, slo=self.scn.slo, weights=self.weights)

    def info(self, i: int) -> RequestInfo:
        tr = self.trace
        return RequestInfo(source=str(self.src[i]), input_tokens=int(tr.input_tokens[i]), output_tokens=max(1, int(tr.output_tokens[i])),
                           id=str(i), attrs=tr.attrs_of(i) if tr.attrs else {})

    def on_arrive(self, t: float, i: int) -> None:
        req = self.info(i)
        self.src_window[req.source] += 1
        decision = self.routing.choose(req, self.view(t))
        self.route_reasons[decision.reason] = self.route_reasons.get(decision.reason, 0) + 1
        self.r_reason[i] = decision.reason
        sid = decision.site
        if sid is None:
            self.r_status[i] = UNROUTED
            return
        self.inflight[sid] += 1
        self.r_site[i] = self.site_ids.index(sid)
        self.push(t + self.rtt[req.source][sid] / 2000.0, SITE_ARRIVE, (sid, i, req))

    def on_site_arrive(self, t: float, data) -> None:
        sid, i, req = data
        self.inflight[sid] -= 1
        b = int(t // self.bin_s)
        if b < self.nb:
            self.bin_arrivals[sid][b] += 1
        try:
            self.sites[sid].submit(Job(req, i), t)
        except Overloaded:
            self.r_status[i] = REJECTED

    def on_start(self, sid: str, job: Job, server: Server, now: float) -> None:
        i = job.payload
        req = job.info
        p = server.profile
        pre = prefill_s(p, req.input_tokens, server.cap_ratio)
        tp = tpot_s(p, server.occupancy, server.cap_ratio)
        svc = pre + req.output_tokens * tp
        half_rtt = self.rtt[req.source][sid] / 2000.0
        t_arr = float(self.trace.t[i])
        self.r_wait[i] = now - (job.t_queued if job.t_queued is not None else now)
        self.r_ttft[i] = now + pre + tp + half_rtt - t_arr
        self.r_e2e[i] = now + svc + half_rtt - t_arr
        self.r_tpot[i] = tp
        self.r_server[i] = server.id
        self.r_status[i] = SERVED
        self.push(now + svc, FINISH, (sid, server, svc))

    def on_action(self, sid: str, a: Action) -> None:
        if a.kind == "power_on":
            self.push(a.server.boot_until, BOOT, (sid, a.server, a.server.boot_until))

    def on_finish(self, t: float, data) -> None:
        sid, server, svc = data
        self.sites[sid].finish(server, t, svc)

    def on_boot(self, t: float, data) -> None:
        sid, server, until = data
        if server.state == "booting" and server.boot_until == until:  # 启动期间被取消又重新开机时，旧事件失效
            self.sites[sid].boot_complete(server, t)

    def on_control(self, t: float) -> None:
        for dc in self.sites.values():
            dc.update_rate(t)
        rates = {k: v / self.ctl_s for k, v in self.src_window.items()}
        self.src_window = {k: 0 for k in self.src_window}
        if t + self.ctl_s < self.horizon:
            self.push(t + self.ctl_s, CONTROL, None)
        if "signals" in self.capacity.requires and len(self.signals) < len(self.sites):
            self.skipped_cycles["incomplete_signals"] = self.skipped_cycles.get("incomplete_signals", 0) + 1
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

    # ------------------------------------------------------------ 主循环

    def run(self) -> SimResult:
        self.push(0.0, CONTROL, None)
        arr = self.trace.t
        i, n = 0, self.n
        sig_t = float("-inf")  # 首个事件即刷新信号，保证决策输入完整
        b = 0
        ev = self.ev
        while True:
            t_ev = ev[0][0] if ev else math.inf
            t_arr = float(arr[i]) if i < n else math.inf
            t = min(t_ev, t_arr)
            if t >= self.horizon:
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
            if kind == SITE_ARRIVE:
                self.on_site_arrive(t, data)
            elif kind == FINISH:
                self.on_finish(t, data)
            elif kind == BOOT:
                self.on_boot(t, data)
            elif kind == CONTROL:
                self.on_control(t)
        while b < self.nb:
            self.close_bin(b)
            b += 1
        return self.result()

    def result(self) -> SimResult:
        keys = ("it_kwh", "facility_kwh", "cost", "carbon_kg")
        ts, tot = [], dict.fromkeys(keys, 0.0)
        per_site = {sid: {**dict.fromkeys(keys, 0.0), "requests": 0} for sid in self.sites}
        priced = 0.0  # 有电网信号的设施电量（kWh）
        for b in range(self.nb):
            t0 = b * self.bin_s
            width = min(self.bin_s, self.horizon - t0)
            row = {"t_h": round(t0 / 3600, 4), "time": self.dt(t0).isoformat()}
            for sid, dc in self.sites.items():
                it_kwh = self.bin_energy[sid][b] / 3.6e6
                fac = it_kwh * dc.cfg.pue
                try:
                    g = self.grid.signal(sid, self.dt(t0 + width / 2))
                except SignalUnavailable:
                    g = None
                for d in (tot, per_site[sid]):
                    d["it_kwh"] += it_kwh
                    d["facility_kwh"] += fac
                    if g is not None:
                        d["cost"] += fac * g.price
                        d["carbon_kg"] += fac * g.carbon_intensity
                if g is not None:
                    priced += fac
                row[f"{sid}_it_kw"] = round(self.bin_energy[sid][b] / width / 1000, 4)
                row[f"{sid}_nodes"] = round(self.bin_live[sid][b] / width, 3)
                row[f"{sid}_rps"] = round(self.bin_arrivals[sid][b] / width, 4)
                row[f"{sid}_price"] = None if g is None else g.price
                row[f"{sid}_carbon"] = None if g is None else g.carbon_intensity
                row[f"{sid}_cap_kw"] = None if g is None else g.power_cap_kw
            ts.append(row)
        served = self.r_status == SERVED
        ok = served & (self.r_ttft <= self.scn.slo.ttft_s) & (self.r_tpot <= self.scn.slo.tpot_s)
        ttft, e2e = self.r_ttft[served], self.r_e2e[served]
        for k, sid in enumerate(self.site_ids):
            per_site[sid]["requests"] = int(np.sum(served & (self.r_site == k)))
        ns = int(served.sum())
        counts = {STATUS[k]: int(np.sum(self.r_status == k)) for k in STATUS}
        summary = {
            "routing": self.routing.name, "capacity": self.capacity.name, "hours": self.horizon / 3600,
            "workload": self.trace.summary(), "grid": self.grid.sources(), "currency": self.grid.currency,
            "rps": round(self.n / self.horizon, 4), "arrived": self.n, **counts,
            "slo_attainment": float(ok.sum() / ns) if ns else 0.0,
            "ttft_p50_s": float(np.percentile(ttft, 50)) if ns else None,
            "ttft_p99_s": float(np.percentile(ttft, 99)) if ns else None,
            "e2e_p50_s": float(np.percentile(e2e, 50)) if ns else None,
            **{k: round(v, 4) for k, v in tot.items()},
            "wh_per_request": round(tot["facility_kwh"] * 1000 / max(ns, 1), 4),
            "sites": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in per_site.items()},
            "route_reasons": dict(sorted(self.route_reasons.items())),
            "skipped_cycles": self.skipped_cycles,
            "signal_coverage": round(priced / tot["facility_kwh"], 4) if tot["facility_kwh"] else None,
        }
        site_names = np.array(self.site_ids + [""], dtype=object)
        reqs = {
            "arrival_s": self.trace.t[:self.n], "source": self.src, "site": site_names[self.r_site],
            "server": np.array(self.r_server, dtype=object), "status": np.array([STATUS[s] for s in self.r_status], dtype=object),
            "route_reason": np.array(self.r_reason, dtype=object), "input_tokens": self.trace.input_tokens[:self.n],
            "output_tokens": self.trace.output_tokens[:self.n], "queue_s": self.r_wait, "ttft_s": self.r_ttft,
            "e2e_s": self.r_e2e, "tpot_s": self.r_tpot, "slo_ok": ok,
        }
        return SimResult(summary, ts, reqs, self.decisions)
