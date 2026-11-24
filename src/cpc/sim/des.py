"""离散事件仿真器（仿真模式）。

用途：不启动任何服务，在几十秒内仿真若干小时的跨数据中心运行，快速比较调度策略。
它复用在线平台的全部"可替换部分"：场景配置、功率/性能模型、电网信号模型、路由策略、容量策略，
因此在仿真中写好的策略可以不改代码地放到在线平台（网关/控制器）中运行。

时间单位：仿真秒（从场景 clock.start 开始）。在仿真模式下不存在时间加速，所有时间均为"物理"时间。
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from ..config import CST, NodeProfile, Scenario, SiteConfig
from ..grid.signals import GridModel, GridSignal
from ..loadgen.client import lognormal
from ..models import diurnal_factor, node_idle_w, node_peak_w, node_power_w, prefill_s, rtt_matrix, service_s, tpot_s
from ..schemas import CapacityTarget, DemandSnapshot, NodeSnapshot, SiteSnapshot
from ..sched import CapacityPolicy, ClusterView, ControlView, RequestInfo, RoutingPolicy, SiteView

ARRIVE, SITE_ARRIVE, FINISH, BOOT, CONTROL = range(5)


@dataclass
class SimNode:
    id: str
    slots: int
    state: str = "on"
    busy: int = 0
    cap: float = 1.0
    power: float = 0.0


@dataclass
class SimSite:
    cfg: SiteConfig
    prof: NodeProfile
    queue_limit_per_slot: float
    nodes: list[SimNode] = field(default_factory=list)
    queue: deque = field(default_factory=deque)
    power: float = 0.0  # 当前 IT 功率（W）
    last_t: float = 0.0
    est_service_s: float = 5.0
    arrivals_window: int = 0
    arrival_rate: float = 0.0
    inflight_wan: int = 0  # 已路由、尚在广域网上的请求

    def __post_init__(self) -> None:
        self.nodes = [SimNode(f"{self.cfg.id}-n{i:02d}", self.prof.slots) for i in range(self.cfg.nodes)]
        self.est_service_s = service_s(self.prof, 800, 200, 0.5)
        for n in self.nodes:
            self._node_power(n)
        self.power = sum(n.power for n in self.nodes)

    def _node_power(self, n: SimNode) -> float:
        old = n.power
        n.power = node_power_w(self.prof, n.state, n.busy / n.slots, n.cap)[0]
        return n.power - old

    def update(self, n: SimNode) -> None:
        self.power += self._node_power(n)

    @property
    def slots_on(self) -> int:
        return sum(n.slots for n in self.nodes if n.state == "on")

    @property
    def queue_limit(self) -> int:
        return max(1, math.ceil(max(self.slots_on, self.prof.slots) * self.queue_limit_per_slot))

    def free_node(self) -> SimNode | None:
        best = None
        for n in self.nodes:
            if n.state == "on" and n.busy < n.slots and (best is None or n.busy < best.busy):
                best = n
        return best

    def snapshot(self, t: float, with_nodes: bool = False) -> SiteSnapshot:
        """与站点代理 /state 相同的结构。路由时每个请求都要构造，故用 model_construct 跳过校验以提速。"""
        on = [n for n in self.nodes if n.state == "on"]
        return SiteSnapshot.model_construct(
            site=self.cfg.id, ts=t, backend="sim", healthy=True, energy_it_j=0.0,
            nodes_total=len(self.nodes), nodes_on=len(on),
            nodes_booting=sum(n.state == "booting" for n in self.nodes),
            nodes_draining=sum(n.state == "draining" for n in self.nodes),
            slots_per_node=self.prof.slots, slots_on=sum(n.slots for n in on), busy=sum(n.busy for n in self.nodes),
            queue_len=len(self.queue), queue_limit=self.queue_limit, cap_ratio=self.nodes[0].cap if self.nodes else 1.0,
            it_power_w=self.power, facility_power_w=self.power * self.cfg.pue, pue=self.cfg.pue,
            est_service_s=self.est_service_s, arrival_rate=self.arrival_rate,
            node_idle_w=node_idle_w(self.prof), node_peak_w=node_peak_w(self.prof),
            nodes=[NodeSnapshot(id=n.id, state=n.state, slots=n.slots, busy=n.busy, cap_ratio=n.cap, it_power_w=n.power)
                   for n in self.nodes] if with_nodes else [],
        )


@dataclass
class SimResult:
    summary: dict
    timeseries: list[dict]
    requests: dict[str, np.ndarray]
    decisions: list[dict]


class Simulator:
    def __init__(self, scn: Scenario, routing: RoutingPolicy, capacity: CapacityPolicy, hours: float = 24.0,
                 bin_s: float = 300.0, rps: float | None = None, control_interval_s: float | None = None,
                 initial_active: int | None = None):
        self.scn = scn
        self.routing = routing
        self.capacity = capacity
        self.horizon = hours * 3600.0
        self.bin_s = bin_s
        self.rps = rps if rps is not None else scn.workload.rps
        self.ctl_s = control_interval_s or scn.scheduler.sim_control_interval_s
        self.grid = GridModel(scn)
        self.rtt = rtt_matrix(scn)
        self.sites = {s.id: SimSite(s, scn.profile_of(s.id), scn.scheduler.queue_limit_per_slot) for s in scn.sites}
        if initial_active is not None:
            for s in self.sites.values():
                for i, n in enumerate(s.nodes):
                    n.state = "on" if i < initial_active else "off"
                    s.update(n)
        self.weights: dict[str, dict[str, float]] = {}
        self.rng = np.random.default_rng(scn.workload.seed)
        self.ev: list = []
        self.seq = 0
        nb = int(math.ceil(self.horizon / bin_s))
        self.bin_energy = {sid: np.zeros(nb) for sid in self.sites}  # J（IT）
        self.bin_nodes = {sid: np.zeros(nb) for sid in self.sites}  # 节点·秒（在线+启动中）
        self.bin_arrivals = {sid: np.zeros(nb) for sid in self.sites}
        self.src_window = {s.id: 0 for s in scn.sources}
        self.decisions: list[dict] = []
        # 请求记录
        self.rec_src: list[str] = []
        self.rec_site: list[str] = []
        self.rec_ttft: list[float] = []
        self.rec_e2e: list[float] = []
        self.rec_tpot: list[float] = []
        self.rec_ok: list[int] = []
        self.arrived = 0
        self.rejected = 0
        self.unrouted = 0

    # ------------------------------------------------------------ 工具

    def push(self, t: float, kind: int, data) -> None:
        self.seq += 1
        heapq.heappush(self.ev, (t, self.seq, kind, data))

    def dt(self, t: float) -> datetime:
        return self.scn.clock.start + timedelta(seconds=t)

    def accrue(self, s: SimSite, t: float) -> None:
        """把 [last_t, t) 内的能耗按时间片累加（功率分段恒定）。"""
        t0 = s.last_t
        if t <= t0:
            return
        n_live = sum(n.state != "off" for n in s.nodes)
        while t0 < t:
            b = int(t0 // self.bin_s)
            if b >= len(self.bin_energy[s.cfg.id]):
                break
            t1 = min(t, (b + 1) * self.bin_s)
            self.bin_energy[s.cfg.id][b] += s.power * (t1 - t0)
            self.bin_nodes[s.cfg.id][b] += n_live * (t1 - t0)
            t0 = t1
        s.last_t = t

    # ------------------------------------------------------------ 事件处理

    def gen_arrivals(self) -> None:
        w = self.scn.workload
        lam_max = self.rps * (1 + abs(w.diurnal_amplitude))
        srcs = [s.id for s in self.scn.sources]
        shares = np.array([s.share for s in self.scn.sources], float)
        shares /= shares.sum()
        t = 0.0
        while True:
            t += self.rng.exponential(1.0 / lam_max)
            if t >= self.horizon:
                break
            lt = self.dt(t).astimezone(CST)
            lam = self.rps * diurnal_factor(lt.hour + lt.minute / 60, w.diurnal_amplitude, w.peak_hour)
            if self.rng.random() * lam_max <= lam:
                src = srcs[int(self.rng.choice(len(srcs), p=shares))]
                self.push(t, ARRIVE, (src, lognormal(self.rng, w.input_tokens), lognormal(self.rng, w.output_tokens)))

    def view(self, t: float, signals: dict[str, GridSignal]) -> ClusterView:
        sv = {sid: SiteView(snap=s.snapshot(t), signal=signals.get(sid), pending=s.inflight_wan) for sid, s in self.sites.items()}
        return ClusterView(now=self.dt(t), sites=sv, rtt_ms=self.rtt, slo=self.scn.slo, weights=self.weights)

    def on_arrive(self, t: float, data, signals) -> None:
        src, n_in, n_out = data
        self.src_window[src] += 1
        req = RequestInfo(source=src, input_tokens=n_in, output_tokens=n_out)
        self.arrived += 1
        sid = self.routing.choose(req, self.view(t, signals))
        if sid is None:
            self.unrouted += 1
            return
        s = self.sites[sid]
        s.inflight_wan += 1
        rtt = self.rtt[src][sid] / 1000.0
        self.push(t + rtt / 2, SITE_ARRIVE, (sid, src, n_in, n_out, t, rtt))

    def start(self, t: float, s: SimSite, n: SimNode, req) -> None:
        sid, src, n_in, n_out, t_arr, rtt = req
        n.busy += 1
        self.accrue(s, t)
        s.update(n)
        occ = n.busy / n.slots
        pre = prefill_s(s.prof, n_in, n.cap)
        tp = tpot_s(s.prof, occ, n.cap)
        t_first = t + pre + tp
        t_end = t + pre + n_out * tp
        ttft = t_first + rtt / 2 - t_arr
        e2e = t_end + rtt / 2 - t_arr
        self.rec_src.append(src)
        self.rec_site.append(sid)
        self.rec_ttft.append(ttft)
        self.rec_e2e.append(e2e)
        self.rec_tpot.append(tp)
        self.rec_ok.append(int(ttft <= self.scn.slo.ttft_s and tp <= self.scn.slo.tpot_s))
        self.push(t_end, FINISH, (sid, n.id, t_end - t))

    def on_site_arrive(self, t: float, req) -> None:
        sid = req[0]
        s = self.sites[sid]
        s.inflight_wan -= 1
        s.arrivals_window += 1
        b = int(t // self.bin_s)
        if b < len(self.bin_arrivals[sid]):
            self.bin_arrivals[sid][b] += 1
        n = s.free_node()
        if n is not None and not s.queue:
            self.start(t, s, n, req)
        elif len(s.queue) < s.queue_limit and any(x.state in ("on", "booting") for x in s.nodes):
            s.queue.append(req)
        else:
            self.rejected += 1

    def dispatch(self, t: float, s: SimSite) -> None:
        while s.queue:
            n = s.free_node()
            if n is None:
                return
            self.start(t, s, n, s.queue.popleft())

    def on_finish(self, t: float, data) -> None:
        sid, nid, svc = data
        s = self.sites[sid]
        n = next(x for x in s.nodes if x.id == nid)
        self.accrue(s, t)
        n.busy -= 1
        s.est_service_s = 0.98 * s.est_service_s + 0.02 * svc
        if n.state == "draining" and n.busy == 0:
            n.state = "off"
        s.update(n)
        self.dispatch(t, s)

    def on_boot(self, t: float, data) -> None:
        sid, nid = data
        s = self.sites[sid]
        n = next(x for x in s.nodes if x.id == nid)
        if n.state == "booting":
            self.accrue(s, t)
            n.state = "on"
            s.update(n)
            self.dispatch(t, s)

    def apply(self, t: float, s: SimSite, tgt: CapacityTarget) -> None:
        """与在线站点代理 SiteCore.apply 相同的规则。"""
        self.accrue(s, t)
        if tgt.cap_ratio is not None:
            r = min(1.0, max(s.prof.cap_min_ratio, tgt.cap_ratio))
            for n in s.nodes:
                n.cap = r
                s.update(n)
        if tgt.active_nodes is None:
            return
        want = max(0, min(tgt.active_nodes, len(s.nodes)))
        live = [n for n in s.nodes if n.state in ("on", "booting")]
        if want > len(live):
            need = want - len(live)
            for n in [n for n in s.nodes if n.state == "draining"][:need]:
                n.state = "on"
                need -= 1
            for n in [n for n in s.nodes if n.state == "off"][:need]:
                n.state = "booting"
                s.update(n)
                self.push(t + s.prof.boot_s, BOOT, (s.cfg.id, n.id))
        elif want < len(live):
            drop = len(live) - want
            for n in [n for n in live if n.state == "booting"][:drop]:
                n.state = "off"
                s.update(n)
                drop -= 1
            for n in sorted([n for n in live if n.state == "on"], key=lambda n: n.busy)[:drop]:
                n.state = "off" if n.busy == 0 else "draining"
                s.update(n)
        self.dispatch(t, s)

    def on_control(self, t: float, signals) -> None:
        for s in self.sites.values():
            s.arrival_rate = s.arrivals_window / self.ctl_s
            s.arrivals_window = 0
        demand = DemandSnapshot(rates={k: v / self.ctl_s for k, v in self.src_window.items()})
        self.src_window = {k: 0 for k in self.src_window}
        view = ControlView(now=self.dt(t), interval_s=self.ctl_s, sites={k: s.snapshot(t, True) for k, s in self.sites.items()},
                           signals=signals, demand=demand, rtt_ms=self.rtt, slo=self.scn.slo,
                           min_active={s.id: s.min_active for s in self.scn.sites})
        plan = self.capacity.decide(view)
        for sid, tgt in plan.targets.items():
            self.apply(t, self.sites[sid], tgt)
        if plan.weights is not None:
            self.weights = plan.weights
        self.decisions.append({"t": t, "targets": {k: v.model_dump() for k, v in plan.targets.items()}, "notes": plan.notes})
        if t + self.ctl_s < self.horizon:
            self.push(t + self.ctl_s, CONTROL, None)

    # ------------------------------------------------------------ 主循环

    def run(self) -> SimResult:
        self.gen_arrivals()
        self.push(0.0, CONTROL, None)
        sig_t, signals = -1.0, {}
        while self.ev:
            t, _, kind, data = heapq.heappop(self.ev)
            if t >= self.horizon:
                break
            if t - sig_t >= 60.0:  # 电网信号每仿真分钟刷新一次
                signals = self.grid.snapshot(self.dt(t))
                sig_t = t
            if kind == ARRIVE:
                self.on_arrive(t, data, signals)
            elif kind == SITE_ARRIVE:
                self.on_site_arrive(t, data)
            elif kind == FINISH:
                self.on_finish(t, data)
            elif kind == BOOT:
                self.on_boot(t, data)
            elif kind == CONTROL:
                self.on_control(t, signals)
        for s in self.sites.values():
            self.accrue(s, self.horizon)
        return self.result()

    def result(self) -> SimResult:
        nb = len(next(iter(self.bin_energy.values())))
        ts, tot = [], {"it_kwh": 0.0, "facility_kwh": 0.0, "cost_yuan": 0.0, "carbon_kg": 0.0}
        per_site = {sid: {"it_kwh": 0.0, "facility_kwh": 0.0, "cost_yuan": 0.0, "carbon_kg": 0.0, "requests": 0} for sid in self.sites}
        for b in range(nb):
            t_mid = (b + 0.5) * self.bin_s
            row = {"t_h": round(b * self.bin_s / 3600, 4), "time": self.dt(b * self.bin_s).isoformat()}
            for sid, s in self.sites.items():
                g = self.grid.signal(sid, self.dt(t_mid))
                it_kwh = self.bin_energy[sid][b] / 3.6e6
                fac = it_kwh * s.cfg.pue
                cost, co2 = fac * g.price_yuan_per_kwh, fac * g.carbon_kg_per_kwh
                for d in (tot, per_site[sid]):
                    d["it_kwh"] += it_kwh
                    d["facility_kwh"] += fac
                    d["cost_yuan"] += cost
                    d["carbon_kg"] += co2
                row[f"{sid}_it_kw"] = round(self.bin_energy[sid][b] / self.bin_s / 1000, 4)
                row[f"{sid}_nodes"] = round(self.bin_nodes[sid][b] / self.bin_s, 3)
                row[f"{sid}_rps"] = round(self.bin_arrivals[sid][b] / self.bin_s, 4)
                row[f"{sid}_price"] = g.price_yuan_per_kwh
                row[f"{sid}_carbon"] = g.carbon_kg_per_kwh
                row[f"{sid}_cap_kw"] = g.power_cap_kw
            ts.append(row)
        ttft, e2e, ok = np.array(self.rec_ttft), np.array(self.rec_e2e), np.array(self.rec_ok)
        for sid in self.rec_site:
            per_site[sid]["requests"] += 1
        n = len(self.rec_site)
        summary = {
            "routing": self.routing.name, "capacity": self.capacity.name, "hours": self.horizon / 3600, "rps": self.rps,
            "arrived": self.arrived, "served": n, "rejected": self.rejected, "unrouted": self.unrouted,
            "unfinished": self.arrived - n - self.rejected - self.unrouted,  # 仿真结束时仍在排队或在途
            "slo_attainment": float(ok.mean()) if n else 0.0,
            "ttft_p50_s": float(np.percentile(ttft, 50)) if n else None,
            "ttft_p99_s": float(np.percentile(ttft, 99)) if n else None,
            "e2e_p50_s": float(np.percentile(e2e, 50)) if n else None,
            **{k: round(v, 4) for k, v in tot.items()},
            "wh_per_request": round(tot["facility_kwh"] * 1000 / max(n, 1), 4),
            "sites": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in per_site.items()},
        }
        reqs = {"source": np.array(self.rec_src), "site": np.array(self.rec_site), "ttft": ttft, "e2e": e2e,
                "tpot": np.array(self.rec_tpot), "ok": ok}
        return SimResult(summary, ts, reqs, self.decisions)
