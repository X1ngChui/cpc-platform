"""站点核心：节点池状态机、并发槽与排队、容量指令执行、能耗积分。

一个站点 = 若干节点；一个节点 = 一台推理服务器（仿真后端）或一个推理实例（真实后端，如一个 vLLM 进程）。
节点状态：off（关机/停服）→ booting（启动中）→ on（服务中）→ draining（停止接新请求，等待在途请求完成）→ off。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from ..config import NodeProfile, SiteConfig, UpstreamConfig
from ..models import node_idle_w, node_peak_w, service_s
from ..schemas import CapacityTarget, NodeSnapshot, SiteSnapshot


class Overloaded(Exception):
    """排队已满（或没有可用节点），站点拒绝请求。"""


@dataclass
class Node:
    id: str
    slots: int
    gpus: int
    state: str = "on"
    busy: int = 0
    cap_ratio: float = 1.0
    boot_until: float = 0.0
    upstream: UpstreamConfig | None = None
    # 最近一次功率采样
    it_power_w: float = 0.0
    gpu_power_w: float = 0.0
    gpu_powers_w: list[float] = field(default_factory=list)
    gpu_utils: list[float] = field(default_factory=list)
    power_source: str = "model"
    # 累计能耗
    energy_j: float = 0.0
    gpu_energy_mj: list[float] = field(default_factory=list)

    @property
    def accepting(self) -> bool:
        return self.state == "on" and self.busy < self.slots

    def snapshot(self) -> NodeSnapshot:
        return NodeSnapshot(
            id=self.id,
            state=self.state,
            slots=self.slots,
            busy=self.busy,
            cap_ratio=self.cap_ratio,
            it_power_w=round(self.it_power_w, 2),
            gpu_power_w=round(self.gpu_power_w, 2),
            power_source=self.power_source,
        )


class PowerSource(Protocol):
    """功率来源：模型计算（仿真）或硬件采集（NVML 等）。sample() 就地更新各节点的功率字段。"""

    name: str

    def sample(self, nodes: list[Node]) -> None: ...


class NodeHooks(Protocol):
    """节点执行器钩子：真实后端在这里启停容器、设置功率上限、检查就绪。"""

    async def power_on(self, node: Node) -> None: ...

    async def power_off(self, node: Node) -> None: ...

    async def set_cap(self, node: Node, ratio: float) -> None: ...

    async def is_ready(self, node: Node) -> bool: ...


class NoopHooks:
    async def power_on(self, node: Node) -> None:
        return None

    async def power_off(self, node: Node) -> None:
        return None

    async def set_cap(self, node: Node, ratio: float) -> None:
        return None

    async def is_ready(self, node: Node) -> bool:
        return time.monotonic() >= node.boot_until


class SiteCore:
    def __init__(
        self,
        cfg: SiteConfig,
        profile: NodeProfile,
        power: PowerSource,
        hooks: NodeHooks | None = None,
        queue_limit_per_slot: float = 4.0,
        initial_active: int | None = None,
    ):
        self.cfg = cfg
        self.profile = profile
        self.power = power
        self.hooks: NodeHooks = hooks or NoopHooks()
        self.queue_limit_per_slot = queue_limit_per_slot
        if cfg.backend == "openai":
            self.nodes = [
                Node(id=u.id, slots=u.max_inflight, gpus=max(len(u.gpu_indices), 1), upstream=u) for u in cfg.upstreams
            ]
        else:
            self.nodes = [Node(id=f"{cfg.id}-n{i:02d}", slots=profile.slots, gpus=profile.gpus) for i in range(cfg.nodes)]
        for n in self.nodes:
            n.gpu_energy_mj = [0.0] * n.gpus
        n_on = len(self.nodes) if initial_active is None else max(0, min(initial_active, len(self.nodes)))
        # 真实后端的实例启动时先处于 booting，通过就绪检查（/health）后才接收请求
        first = "booting" if cfg.backend == "openai" else "on"
        for i, n in enumerate(self.nodes):
            n.state = first if i < n_on else "off"
        self._waiters: deque[asyncio.Future] = deque()
        self._last_t = time.monotonic()
        self._rate_t = self._last_t
        self._arrivals = 0
        self.arrival_rate = 0.0
        self.est_service_s = service_s(profile, 800, 200, 0.5)  # 初值：典型请求长度下的服务时间
        self.target = CapacityTarget(active_nodes=n_on, cap_ratio=1.0)
        self._pending_tasks: set[asyncio.Task] = set()
        self.power.sample(self.nodes)

    # ------------------------------------------------------------ 并发槽与排队

    @property
    def slots_on(self) -> int:
        return sum(n.slots for n in self.nodes if n.state == "on")

    @property
    def queue_limit(self) -> int:
        base = max(self.slots_on, self.profile.slots if self.cfg.backend == "emulated" else 1)
        return max(1, math.ceil(base * self.queue_limit_per_slot))

    def _free_node(self) -> Node | None:
        cands = [n for n in self.nodes if n.accepting]
        if not cands:
            return None
        return min(cands, key=lambda n: n.busy / n.slots)  # 站内最少负载

    async def acquire(self) -> Node:
        self._arrivals += 1
        node = self._free_node()
        if node is not None and not self._waiters:
            node.busy += 1
            return node
        if len(self._waiters) >= self.queue_limit or not any(n.state in ("on", "booting") for n in self.nodes):
            raise Overloaded()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            return await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release(fut.result(), None)
            else:
                try:
                    self._waiters.remove(fut)
                except ValueError:
                    pass
            raise

    def release(self, node: Node, service_s: float | None) -> None:
        node.busy = max(0, node.busy - 1)
        if service_s is not None:
            self.est_service_s = 0.95 * self.est_service_s + 0.05 * service_s
        if node.state == "draining" and node.busy == 0:
            self._set_off(node)
            self.resample()
        self._dispatch()

    def _dispatch(self) -> None:
        while self._waiters:
            node = self._free_node()
            if node is None:
                return
            fut = self._waiters.popleft()
            if fut.cancelled():
                continue
            node.busy += 1
            fut.set_result(node)

    # ------------------------------------------------------------ 容量执行

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _set_off(self, node: Node) -> None:
        node.state = "off"
        self._spawn(self.hooks.power_off(node))

    def apply(self, target: CapacityTarget) -> None:
        """执行容量指令。开机：off→booting（经 boot_s 或就绪检查后转 on）；关机：先排空再关。"""
        if target.cap_ratio is not None:
            ratio = min(1.0, max(self.profile.cap_min_ratio, float(target.cap_ratio)))
            self.target.cap_ratio = ratio
            for n in self.nodes:
                if n.cap_ratio != ratio:
                    n.cap_ratio = ratio
                    self._spawn(self.hooks.set_cap(n, ratio))
        if target.active_nodes is None:
            return
        want = max(0, min(int(target.active_nodes), len(self.nodes)))
        self.target.active_nodes = want
        live = [n for n in self.nodes if n.state in ("on", "booting")]
        if want > len(live):
            # 优先恢复正在排空的节点（无需重启），再开启关机节点
            need = want - len(live)
            for n in [n for n in self.nodes if n.state == "draining"][:need]:
                n.state = "on"
                need -= 1
            for n in [n for n in self.nodes if n.state == "off"][:need]:
                n.state = "booting"
                n.boot_until = time.monotonic() + self.profile.boot_s
                self._spawn(self.hooks.power_on(n))
        elif want < len(live):
            drop = len(live) - want
            # 先取消启动中的，再排空负载最轻的
            for n in [n for n in live if n.state == "booting"][:drop]:
                self._set_off(n)
                drop -= 1
            for n in sorted([n for n in live if n.state == "on"], key=lambda n: n.busy)[:drop]:
                if n.busy == 0:
                    self._set_off(n)
                else:
                    n.state = "draining"
        self._dispatch()
        self.resample()  # 立即刷新功率，使状态快照与指令一致

    def resample(self) -> None:
        """先把上次采样以来的能耗按旧功率积分（功率分段恒定），再重新采样功率。"""
        now = time.monotonic()
        dt = now - self._last_t
        self._last_t = now
        for n in self.nodes:
            n.energy_j += n.it_power_w * dt
            for i, p in enumerate(n.gpu_powers_w):
                if i < len(n.gpu_energy_mj):
                    n.gpu_energy_mj[i] += p * dt * 1000.0
        self.power.sample(self.nodes)

    async def tick(self) -> None:
        """周期调用：推进启动/排空状态、积分能耗并重新采样功率、更新到达率。"""
        for n in self.nodes:
            if n.state == "booting" and await self.hooks.is_ready(n):
                n.state = "on"
            if n.state == "draining" and n.busy == 0:
                self._set_off(n)
        self._dispatch()
        self.resample()
        now = time.monotonic()
        dt = now - self._rate_t
        if dt > 0:
            self.arrival_rate = 0.8 * self.arrival_rate + 0.2 * (self._arrivals / dt)
        self._rate_t = now
        self._arrivals = 0

    # ------------------------------------------------------------ 状态

    def snapshot(self) -> SiteSnapshot:
        it = sum(n.it_power_w for n in self.nodes)
        return SiteSnapshot(
            site=self.cfg.id,
            ts=time.time(),
            backend=self.cfg.backend,
            healthy=True,
            nodes_total=len(self.nodes),
            nodes_on=sum(n.state == "on" for n in self.nodes),
            nodes_booting=sum(n.state == "booting" for n in self.nodes),
            nodes_draining=sum(n.state == "draining" for n in self.nodes),
            slots_per_node=self.nodes[0].slots if self.nodes else 0,
            slots_on=self.slots_on,
            busy=sum(n.busy for n in self.nodes),
            queue_len=len(self._waiters),
            queue_limit=self.queue_limit,
            cap_ratio=self.target.cap_ratio if self.target.cap_ratio is not None else 1.0,
            it_power_w=round(it, 2),
            facility_power_w=round(it * self.cfg.pue, 2),
            pue=self.cfg.pue,
            energy_it_j=round(sum(n.energy_j for n in self.nodes), 1),
            est_service_s=round(self.est_service_s, 3),
            arrival_rate=round(self.arrival_rate, 3),
            node_idle_w=node_idle_w(self.profile),
            node_peak_w=node_peak_w(self.profile),
            nodes=[n.snapshot() for n in self.nodes],
        )


TickFn = Callable[[], Awaitable[None]]
