"""数据中心运行时：服务器状态机、站内排队与派发、容量指令执行、能耗计量。仿真与实验共用同一实现。

运行时与时钟无关：所有方法显式接收当前时间 now（秒，单调不减；仿真中为仿真秒，实验中为 time.monotonic()）。
驱动方（离散事件仿真器或站点代理）负责：
- 推进时间：在请求到达（submit）、完成（finish）、服务器启动完成（boot_complete）、容量指令（apply）等时刻调用相应方法；
- 执行请求：回调 on_start(job, server, now) 表示请求开始在服务器上服务。仿真器按性能模型安排完成事件，站点代理把请求交给执行器；
- 执行动作：回调 on_action(action) 给出开机（power_on）、关机（power_off）、功率封顶（set_cap）操作。
  仿真器在 server.boot_until 时刻调用 boot_complete；站点代理调用执行器钩子，并以就绪检查或 boot_until 判断启动完成；
- 测量功率：model_power 为真的服务器在每次状态变化时按服务器模型重算功率；实测服务器的功率由驱动方在 advance(now) 之后写入。

服务器状态：off → booting → on → draining → off。
- 开机：off → booting（发出 power_on），启动完成后 → on；启动失败回到 off；
- 关机：booting → off（发出 power_off）；on 且没有在服务的请求 → off（发出 power_off）；
  on 且有在服务的请求 → draining（不再接收请求），最后一个请求完成后 → off（发出 power_off）；
- 扩容时 draining → on（无需重启）。选择哪些服务器由电源管理策略（PowerPolicy）决定。

派发：请求到达且队列为空时，由派发策略（DispatchPolicy）选择服务器；没有可选服务器时进入队列。队列按派发策略给出的优先级排序，
同优先级先到先出；队列已满或没有在线、启动中的服务器时拒绝（Overloaded）。服务器空出并发槽、启动完成或容量变化后，从队首依次派发。

能耗：功率在相邻两次变化之间视为恒定，逐服务器积分；功率缺失（None）的区间不积分，计入缺失时长。
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import NodeProfile, SiteConfig, UpstreamConfig
from .models import idle_w, peak_w, power_w, service_s
from .schemas import CapacityTarget, NodeSnapshot, PoolSnapshot, SiteSnapshot
from .sched.local import DispatchPolicy, PowerPolicy
from .sched.routing import RequestInfo

STATES = ("off", "booting", "on", "draining")
SERVICE_EWMA = 0.05  # 平均服务时间估计的指数加权系数（每完成一个请求）


class Overloaded(Exception):
    """队列已满或没有在线、启动中的服务器，数据中心拒绝请求。"""


@dataclass(eq=False)
class Server:
    id: str
    pool: str
    profile: NodeProfile
    slots: int
    upstream: UpstreamConfig | None = None  # 真实服务器（OpenAI 兼容推理实例）；仿真服务器为 None
    model_power: bool = True  # True：功率由服务器模型给出；False：由驱动方写入实测值
    state: str = "off"
    busy: int = 0
    cap_ratio: float = 1.0
    boot_until: float = 0.0
    it_power_w: float | None = 0.0
    gpu_power_w: float | None = None  # 加速卡功率（只对实测服务器给出）
    power_missing: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)  # 实测的逐卡读数（驱动方写入，供指标导出）
    energy_j: float = 0.0  # 功率有效区间内的累计 IT 能耗
    measured_s: float = 0.0  # 功率有效的累计时长
    missing_s: float = 0.0  # 功率缺失的累计时长
    live_s: float = 0.0  # 非关机状态的累计时长
    last_error: str | None = None
    t: float = 0.0  # 能耗已积分到的时刻

    def __post_init__(self) -> None:
        self.peak_w = peak_w(self.profile)
        self.idle_w = idle_w(self.profile)

    @property
    def accepting(self) -> bool:
        return self.state == "on" and self.busy < self.slots

    @property
    def occupancy(self) -> float:
        return self.busy / self.slots if self.slots else 0.0

    def snapshot(self) -> NodeSnapshot:
        r = lambda x: None if x is None else round(x, 2)  # noqa: E731
        return NodeSnapshot.model_construct(
            id=self.id, pool=self.pool, state=self.state, slots=self.slots, busy=self.busy, cap_ratio=self.cap_ratio,
            it_power_w=r(self.it_power_w), gpu_power_w=r(self.gpu_power_w),
            power_source="model" if self.model_power else "sensors", power_missing=list(self.power_missing),
            last_error=self.last_error)


@dataclass(eq=False)
class Job:
    """数据中心内的一个请求。payload 由驱动方使用（仿真器的请求记录、站点代理的 Future）。"""

    info: RequestInfo
    payload: Any = None
    state: str = "new"  # new / queued / started / cancelled
    t_queued: float | None = None
    server: Server | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # power_on / power_off / set_cap
    server: Server
    ratio: float | None = None


@dataclass(frozen=True)
class Meter:
    """累计计量：IT 能耗（J）、功率有效 / 缺失时长与非关机时长（服务器·秒）。"""

    energy_j: float
    measured_s: float
    missing_s: float
    live_s: float


def _noop(*_a) -> None:
    return None


class Datacenter:
    def __init__(self, cfg: SiteConfig, profiles: dict[str, NodeProfile], *, dispatch: DispatchPolicy, power: PowerPolicy,
                 queue_limit_per_slot: float = 4.0, rate_window_s: float = 60.0, now: float = 0.0,
                 initial_active: int | None = None, emulate_all: bool = False,
                 on_start: Callable[[Job, Server, float], None] = _noop, on_action: Callable[[Action], None] = _noop):
        """emulate_all：把真实服务器池也按服务器模型仿真（离散事件仿真器使用）。"""
        self.cfg = cfg
        self.dispatch = dispatch
        self.power = power
        self.qlps = queue_limit_per_slot
        self.rate_window_s = rate_window_s
        self.on_start = on_start
        self.on_action = on_action
        self.servers: list[Server] = []
        for p in cfg.pools:
            prof = profiles[p.profile]
            if p.backend == "emulated":
                self.servers += [Server(f"{cfg.id}-{p.id}-{i:02d}", p.id, prof, prof.slots) for i in range(p.count)]
            elif emulate_all:
                self.servers += [Server(u.id, p.id, prof, u.max_inflight) for u in p.upstreams]
            else:
                self.servers += [Server(u.id, p.id, prof, u.max_inflight, upstream=u, model_power=False) for u in p.upstreams]
        self.by_id = {s.id: s for s in self.servers}
        n_on = len(self.servers) if initial_active is None else max(0, min(int(initial_active), len(self.servers)))
        for i, s in enumerate(self.servers):
            s.t = now
            # 真实服务器启动时先处于 booting，经就绪检查后才接收请求
            s.state = ("booting" if s.upstream is not None else "on") if i < n_on else "off"
            s.boot_until = now
            if s.model_power:
                self._repower(s)
            else:
                s.it_power_w = s.gpu_power_w = None
                s.power_missing = ["unsampled"]
        self.max_slots = max(s.slots for s in self.servers)
        self.cap_min_ratio = max(s.profile.cap_min_ratio for s in self.servers)
        self.power_scope = "node"
        kinds = {"emulated" if s.upstream is None else "openai" for s in self.servers}
        self.backend = kinds.pop() if len(kinds) == 1 else "mixed"
        self._queue: list[tuple[float, int, Job]] = []
        self._qn = 0
        self._seq = itertools.count()
        first = profiles[cfg.pools[0].profile]
        self.est_service_s = service_s(first, 800, 200, 0.5)  # 初值：典型请求长度下的服务时间
        self.arrival_rate = 0.0
        self._arrivals = 0
        self._rate_t = now
        self.target = CapacityTarget(active_nodes=n_on, cap_ratio=1.0)
        self.actuation_errors: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------ 计量

    @staticmethod
    def _integrate(s: Server, now: float) -> None:
        dt = now - s.t
        if dt > 0:
            if s.it_power_w is None:
                s.missing_s += dt
            else:
                s.energy_j += s.it_power_w * dt
                s.measured_s += dt
            if s.state != "off":
                s.live_s += dt
            s.t = now

    @staticmethod
    def _repower(s: Server) -> None:
        if s.model_power:
            s.it_power_w = power_w(s.profile, s.state, s.occupancy, s.cap_ratio)

    def advance(self, now: float) -> None:
        """把全部服务器的能耗积分到 now（实测服务器写入新读数之前调用）。"""
        for s in self.servers:
            self._integrate(s, now)

    def meter(self, now: float) -> Meter:
        self.advance(now)
        ss = self.servers
        return Meter(sum(s.energy_j for s in ss), sum(s.measured_s for s in ss), sum(s.missing_s for s in ss),
                     sum(s.live_s for s in ss))

    def update_rate(self, now: float) -> None:
        """到达率的指数加权估计（时间常数 rate_window_s，与调用间隔无关）。"""
        dt = now - self._rate_t
        if dt <= 0:
            return
        w = 1.0 - math.exp(-dt / self.rate_window_s)
        self.arrival_rate += w * (self._arrivals / dt - self.arrival_rate)
        self._arrivals = 0
        self._rate_t = now

    # ------------------------------------------------------------ 请求

    @property
    def slots_on(self) -> int:
        return sum(s.slots for s in self.servers if s.state == "on")

    @property
    def queue_len(self) -> int:
        return self._qn

    @property
    def queue_limit(self) -> int:
        return max(1, math.ceil(max(self.slots_on, self.max_slots) * self.qlps))

    def submit(self, job: Job, now: float) -> None:
        """请求到达。立即派发时调用 on_start；否则进入队列；拒绝时抛出 Overloaded。"""
        self._arrivals += 1
        if not self._qn:
            s = self.dispatch.select(job.info, self.servers)
            if s is not None:
                self._start(job, s, now)
                return
        if self._qn >= self.queue_limit or not any(s.state in ("on", "booting") for s in self.servers):
            raise Overloaded(f"站点 {self.cfg.id} 队列已满或没有可用服务器")
        job.state, job.t_queued = "queued", now
        heapq.heappush(self._queue, (-self.dispatch.priority(job.info), next(self._seq), job))
        self._qn += 1
        self._drain(now)

    def cancel(self, job: Job) -> None:
        """撤销排队中的请求（客户端断开等）；已开始服务的请求不受影响。"""
        if job.state == "queued":
            job.state = "cancelled"
            self._qn -= 1

    def _start(self, job: Job, s: Server, now: float) -> None:
        self._integrate(s, now)
        s.busy += 1
        self._repower(s)
        job.state, job.server = "started", s
        self.on_start(job, s, now)

    def _drain(self, now: float) -> None:
        q = self._queue
        while q:
            job = q[0][2]
            if job.state == "cancelled":
                heapq.heappop(q)
                continue
            s = self.dispatch.select(job.info, self.servers)
            if s is None:
                return
            heapq.heappop(q)
            self._qn -= 1
            self._start(job, s, now)

    def finish(self, s: Server, now: float, service_s: float | None = None) -> None:
        """请求在服务器 s 上完成（或失败）。service_s 为服务时长（用于平均服务时间估计）；未知时为 None。"""
        self._integrate(s, now)
        s.busy = max(0, s.busy - 1)
        if service_s is not None:
            self.est_service_s += SERVICE_EWMA * (service_s - self.est_service_s)
        if s.state == "draining" and s.busy == 0:
            s.state = "off"
            self.on_action(Action("power_off", s))
        self._repower(s)
        self._drain(now)

    # ------------------------------------------------------------ 服务器状态

    def boot_complete(self, s: Server, now: float) -> None:
        if s.state != "booting":
            return
        self._integrate(s, now)
        s.state = "on"
        self._repower(s)
        self._drain(now)

    def actuation_failed(self, s: Server, action: str, error: str, now: float) -> None:
        """执行器操作失败：记入服务器的 last_error 与失败计数；开机失败的服务器回到 off。"""
        s.last_error = f"{action}: {error}"
        key = (s.id, action)
        self.actuation_errors[key] = self.actuation_errors.get(key, 0) + 1
        if action == "power_on" and s.state == "booting":
            self._integrate(s, now)
            s.state = "off"
            self._repower(s)

    def server_failed(self, s: Server, error: str, now: float) -> None:
        """在线或排空中的服务器失效（如推理实例的就绪检查连续失败）：退出服务并记录原因。

        在线的服务器转为启动中并重新发出 power_on（由执行器重启推理实例，经就绪检查后恢复服务）；
        排空中的服务器本应关机，转为关机。正在服务的请求由执行器以失败结束。
        """
        if s.state not in ("on", "draining"):
            return
        self._integrate(s, now)
        s.last_error = f"health: {error}"
        key = (s.id, "health")
        self.actuation_errors[key] = self.actuation_errors.get(key, 0) + 1
        if s.state == "on":
            s.state = "booting"
            s.boot_until = now + s.profile.boot_s
            self.on_action(Action("power_on", s))
        else:
            s.state = "off"
            self.on_action(Action("power_off", s))
        self._repower(s)

    def _transition(self, s: Server, to: str, now: float) -> None:
        if to not in ("on", "off"):
            raise ValueError(f"电源管理策略 {self.power.name} 给出了无效的目标状态 {to}（服务器 {s.id}）")
        self._integrate(s, now)
        if to == "on":
            if s.state == "draining":
                s.state = "on"
            elif s.state == "off":
                s.state = "booting"
                s.boot_until = now + s.profile.boot_s
                self.on_action(Action("power_on", s))
        else:
            if s.state == "booting" or (s.state == "on" and s.busy == 0):
                s.state = "off"
                self.on_action(Action("power_off", s))
            elif s.state == "on":
                s.state = "draining"
        self._repower(s)

    def apply(self, target: CapacityTarget, now: float) -> None:
        """执行容量指令：功率封顶比例（各服务器按其下限截断）与目标在线服务器数（由电源管理策略落实到具体服务器）。"""
        if target.cap_ratio is not None:
            self.target.cap_ratio = min(1.0, max(self.cap_min_ratio, float(target.cap_ratio)))
            for s in self.servers:
                r = min(1.0, max(s.profile.cap_min_ratio, float(target.cap_ratio)))
                if s.cap_ratio != r:
                    self._integrate(s, now)
                    s.cap_ratio = r
                    self._repower(s)
                    self.on_action(Action("set_cap", s, r))
        if target.active_nodes is not None:
            want = max(0, min(int(target.active_nodes), len(self.servers)))
            self.target.active_nodes = want
            for sid, to in self.power.plan(want, self.servers).items():
                self._transition(self.by_id[sid], to, now)
        self._drain(now)

    # ------------------------------------------------------------ 状态摘要

    def summary(self, now: float, ts: float | None = None, detail: bool = True) -> SiteSnapshot:
        """数据中心状态摘要（全局策略的输入）。detail=False 时省略服务器列表、服务器池与能耗字段（仿真器逐请求路由时使用）。"""
        ss = self.servers
        cnt = {k: 0 for k in STATES}
        slots_on = busy = 0
        it: float | None = 0.0
        for s in ss:
            cnt[s.state] += 1
            busy += s.busy
            if s.state == "on":
                slots_on += s.slots
            if it is not None:
                it = None if s.it_power_w is None else it + s.it_power_w
        n = len(ss)
        out = dict(
            site=self.cfg.id, ts=now if ts is None else ts, backend=self.backend, nodes_total=n, nodes_on=cnt["on"],
            nodes_booting=cnt["booting"], nodes_draining=cnt["draining"], slots_per_node=sum(s.slots for s in ss) / n,
            slots_on=slots_on, busy=busy, queue_len=self._qn, queue_limit=self.queue_limit,
            cap_ratio=self.target.cap_ratio if self.target.cap_ratio is not None else 1.0, cap_min_ratio=self.cap_min_ratio,
            it_power_w=it, facility_power_w=None if it is None else it * self.cfg.pue, pue=self.cfg.pue,
            power_scope=self.power_scope, est_service_s=self.est_service_s, arrival_rate=self.arrival_rate,
            node_idle_w=sum(s.idle_w for s in ss) / n, node_peak_w=sum(s.peak_w for s in ss) / n,
            energy_it_j=0.0, energy_coverage=None, pools=[], nodes=[], components={})
        if detail:
            m = self.meter(now)
            out.update(
                it_power_w=None if it is None else round(it, 2),
                facility_power_w=None if it is None else round(it * self.cfg.pue, 2),
                est_service_s=round(self.est_service_s, 3), arrival_rate=round(self.arrival_rate, 4),
                energy_it_j=round(m.energy_j, 1),
                energy_coverage=None if m.measured_s + m.missing_s == 0 else round(m.measured_s / (m.measured_s + m.missing_s), 4),
                pools=self._pools(), nodes=[s.snapshot() for s in ss])
        return SiteSnapshot.model_construct(**out)

    def _pools(self) -> list[PoolSnapshot]:
        out = []
        for p in self.cfg.pools:
            ss = [s for s in self.servers if s.pool == p.id]
            out.append(PoolSnapshot.model_construct(
                id=p.id, profile=p.profile, backend="emulated" if ss[0].upstream is None else "openai", total=len(ss),
                on=sum(s.state == "on" for s in ss), booting=sum(s.state == "booting" for s in ss),
                draining=sum(s.state == "draining" for s in ss), slots_per_node=sum(s.slots for s in ss) / len(ss),
                node_idle_w=ss[0].idle_w, node_peak_w=ss[0].peak_w))
        return out
