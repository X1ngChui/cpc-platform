"""站点代理的运行时驱动：以真实时间（time.monotonic）驱动数据中心运行时（cpc.datacenter.Datacenter）。

与离散事件仿真器共用状态机、派发与电源管理策略；本模块只负责实验特有的部分：
- 请求：acquire() 把请求提交给运行时并等待派发（Future），release() 在执行器完成后通知运行时；
- 动作：仿真服务器的开机在 boot_until 时刻完成；真实服务器通过执行器钩子启停、设置功率上限，并以就绪检查判断启动完成。
  钩子失败时记入服务器的 last_error 与失败计数，开机失败的服务器回到 off；
- 测量：真实服务器的功率由传感器周期性采样后写入（site/power.py）；仿真服务器的功率由服务器模型给出。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Protocol

from ..config import Scenario
from ..datacenter import Action, Datacenter, Job, Overloaded, Server
from ..health import describe
from ..sched.local import make_dispatch, make_power
from ..sched.routing import RequestInfo
from ..schemas import CapacityTarget, SiteSnapshot

log = logging.getLogger("cpc.site")

__all__ = ["NodeHooks", "Overloaded", "SiteAgent"]


class NodeHooks(Protocol):
    """真实服务器的执行器钩子：启停推理实例、设置功率上限、就绪检查。"""

    async def power_on(self, server: Server) -> None: ...

    async def power_off(self, server: Server) -> None: ...

    async def set_cap(self, server: Server, ratio: float) -> None: ...

    async def is_ready(self, server: Server) -> bool: ...


class SiteAgent:
    def __init__(self, scn: Scenario, site_id: str, hooks: NodeHooks | None = None, initial_active: int | None = None):
        cfg = scn.site(site_id)
        self.cfg = cfg
        self.hooks = hooks
        self.dc = Datacenter(
            cfg, scn.node_profiles, dispatch=make_dispatch(cfg.scheduler.dispatch.policy, cfg.scheduler.dispatch.params),
            power=make_power(cfg.scheduler.power.policy, cfg.scheduler.power.params),
            queue_limit_per_slot=scn.scheduler.queue_limit_per_slot, rate_window_s=scn.scheduler.rate_window_s,
            now=time.monotonic(), initial_active=initial_active, on_start=self._on_start, on_action=self._on_action)
        if cfg.real and hooks is None:
            raise ValueError(f"站点 {cfg.id} 有真实服务器，需要执行器钩子")
        self.meter = None  # 真实服务器的功率来源（site.power.SensorPower），由 service 设置
        self._tasks: set[asyncio.Task] = set()

    @property
    def servers(self) -> list[Server]:
        return self.dc.servers

    # ------------------------------------------------------------ 请求

    def _on_start(self, job: Job, server: Server, now: float) -> None:
        fut: asyncio.Future = job.payload
        if fut.done():  # 等待方已取消：立即归还并发槽（推迟到当前派发结束之后）
            asyncio.get_running_loop().call_soon(self.release, server, None)
        else:
            fut.set_result(server)

    async def acquire(self, info: RequestInfo) -> Server:
        """提交请求并等待派发；数据中心拒绝时抛出 Overloaded。"""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        job = Job(info, fut)
        self.dc.submit(job, time.monotonic())
        try:
            return await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release(fut.result(), None)
            else:
                self.dc.cancel(job)
            raise

    def release(self, server: Server, service_s: float | None) -> None:
        self.dc.finish(server, time.monotonic(), service_s)

    # ------------------------------------------------------------ 动作

    def _spawn(self, coro: Awaitable, server: Server, action: str) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if t.cancelled() or t.exception() is None:
                return
            err = describe(t.exception())
            self.dc.actuation_failed(server, action, err, time.monotonic())
            log.error("站点 %s 服务器 %s 的 %s 操作失败：%s", self.cfg.id, server.id, action, err,
                      extra={"event": "node_actuation_failed", "site": self.cfg.id, "node": server.id, "action": action})

        task.add_done_callback(done)

    def _on_action(self, a: Action) -> None:
        if a.server.upstream is None or self.hooks is None:
            return  # 仿真服务器：状态与功率由运行时和服务器模型给出
        if a.kind == "power_on":
            self._spawn(self.hooks.power_on(a.server), a.server, "power_on")
        elif a.kind == "power_off":
            self._spawn(self.hooks.power_off(a.server), a.server, "power_off")
        elif a.kind == "set_cap":
            self._spawn(self.hooks.set_cap(a.server, a.ratio), a.server, "set_cap")

    def apply(self, target: CapacityTarget) -> None:
        self.dc.apply(target, time.monotonic())

    # ------------------------------------------------------------ 周期任务

    async def tick(self) -> None:
        """推进启动中的服务器、采样实测功率、更新到达率。"""
        for s in self.dc.servers:
            if s.state != "booting":
                continue
            if s.upstream is None:
                if time.monotonic() >= s.boot_until:
                    self.dc.boot_complete(s, time.monotonic())
            elif await self.hooks.is_ready(s):
                self.dc.boot_complete(s, time.monotonic())
        now = time.monotonic()
        self.dc.advance(now)
        if self.meter is not None:
            self.meter.sample([s for s in self.dc.servers if not s.model_power])
        self.dc.update_rate(now)

    def snapshot(self) -> SiteSnapshot:
        snap = self.dc.summary(time.monotonic(), ts=time.time())
        snap.components = self.meter.status() if self.meter is not None else {}
        return snap
