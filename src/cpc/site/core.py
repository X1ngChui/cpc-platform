"""Site-agent driver: runs the data-centre runtime (cpc.datacenter.Datacenter) on real time (time.monotonic).

The state machine and the dispatch and power policies are shared with the simulator; this module only
adds what live experiments need:
- requests: acquire() submits a request to the runtime and waits for its dispatch (a future);
  release() reports its completion after the executor finishes;
- actions: emulated servers finish booting at boot_until; real servers are started, stopped and
  capped through executor hooks and are ready when their health check passes. Actions on one server
  run in order (a power-on waits until the preceding power-off has finished). A failing hook is
  recorded in last_error and the failure count, and a server failing to boot returns to off; a real
  server that does not pass its health check within boot_timeout_s() has failed to boot as well.
  Online real servers are checked every HEALTH_PROBE_S seconds; after HEALTH_FAILURES consecutive
  failures they leave service (runtime server_failed: back to booting and powered on again, so the
  executor restarts the inference instance) and serve again once healthy;
- measurement: the power of real servers is sampled from the sensors (site/power.py); emulated
  servers take theirs from the server model.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Awaitable, Protocol

from ..config import Scenario
from ..datacenter import Action, Datacenter, Job, Overloaded, Server
from ..health import describe
from ..sched.local import make_dispatch, make_power
from ..sched.routing import RequestInfo
from ..schemas import CapacityTarget, SiteSnapshot

HEALTH_PROBE_S = 5.0  # health-check period of online real servers
HEALTH_FAILURES = 2  # consecutive failures after which a server has failed

log = logging.getLogger("cpc.site")

__all__ = ["NodeHooks", "Overloaded", "SiteAgent", "boot_timeout_s"]


def boot_timeout_s(boot_s: float) -> float:
    """Boot deadline of a real server: 4 x the boot time of its type, at least 300 s."""
    return max(4.0 * boot_s, 300.0)


class NodeHooks(Protocol):
    """Executor hooks of real servers: start and stop inference instances, set power limits, check readiness."""

    async def power_on(self, server: Server) -> None: ...

    async def power_off(self, server: Server) -> None: ...

    async def set_cap(self, server: Server, ratio: float) -> None: ...

    async def is_ready(self, server: Server) -> bool: ...


class SiteAgent:
    def __init__(self, scn: Scenario, site_id: str, hooks: NodeHooks | None = None, initial_active: int | None = None):
        self.cfg = cfg = scn.site(site_id)
        if cfg.real and hooks is None:
            raise ValueError(f"site {cfg.id} has real servers and needs executor hooks")
        self.hooks = hooks
        sched = cfg.scheduler
        self.dc = Datacenter(
            cfg, scn.node_profiles, dispatch=make_dispatch(sched.dispatch.policy, sched.dispatch.params),
            power=make_power(sched.power.policy, sched.power.params),
            queue_limit_per_slot=scn.scheduler.queue_limit_per_slot, rate_window_s=scn.scheduler.rate_window_s,
            now=time.monotonic(), initial_active=initial_active, on_start=self._on_start, on_action=self._on_action)
        self.meter = None  # power source of real servers (site.power.SensorPower), set by the service
        self._tasks: set[asyncio.Task] = set()
        self._locks: dict[str, asyncio.Lock] = {}  # one lock per server: actions run in the order issued
        self._boot_t0: dict[str, float] = {}  # time a real server entered booting
        self._fails: Counter[str] = Counter()  # consecutive failed health checks of online real servers
        self._next_probe = 0.0

    @property
    def servers(self) -> list[Server]:
        return self.dc.servers

    # ------------------------------------------------------------ requests

    def _on_start(self, job: Job, server: Server, now: float) -> None:
        fut: asyncio.Future = job.payload
        if fut.done():  # the waiter was cancelled: return the slot (after the current dispatch)
            asyncio.get_running_loop().call_soon(self.release, server, None)
        else:
            fut.set_result(server)

    async def acquire(self, info: RequestInfo) -> Server:
        """Submit a request and wait until it is dispatched; Overloaded when the data centre rejects it."""
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

    # ------------------------------------------------------------ actions

    def _actuation_failed(self, server: Server, action: str, err: str) -> None:
        self.dc.actuation_failed(server, action, err, time.monotonic())
        log.error("site %s server %s: %s failed: %s", self.cfg.id, server.id, action, err,
                  extra={"event": "node_actuation_failed", "site": self.cfg.id, "node": server.id, "action": action})

    def _spawn(self, coro: Awaitable, server: Server, action: str) -> None:
        lock = self._locks.setdefault(server.id, asyncio.Lock())

        async def serial():
            async with lock:  # asyncio.Lock grants in waiting order
                await coro

        task = asyncio.ensure_future(serial())
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and (e := t.exception()) is not None:
                self._actuation_failed(server, action, describe(e))

        task.add_done_callback(done)

    def _on_action(self, a: Action) -> None:
        if a.server.upstream is None or self.hooks is None:
            return  # emulated server: state and power come from the runtime and the server model
        match a.kind:
            case "power_on":
                self._spawn(self.hooks.power_on(a.server), a.server, a.kind)
            case "power_off":
                self._spawn(self.hooks.power_off(a.server), a.server, a.kind)
            case "set_cap":
                self._spawn(self.hooks.set_cap(a.server, a.ratio), a.server, a.kind)

    def apply(self, target: CapacityTarget) -> None:
        self.dc.apply(target, time.monotonic())

    # ------------------------------------------------------------ periodic work

    async def tick(self) -> None:
        """Advance booting servers, sample measured power, update the arrival rate."""
        for s in self.dc.servers:
            if s.state != "booting":
                self._boot_t0.pop(s.id, None)
            elif s.upstream is None:
                if time.monotonic() >= s.boot_until:
                    self.dc.boot_complete(s, time.monotonic())
            elif await self.hooks.is_ready(s):
                self.dc.boot_complete(s, time.monotonic())
            elif time.monotonic() - self._boot_t0.setdefault(s.id, time.monotonic()) > (limit := boot_timeout_s(s.profile.boot_s)):
                self._boot_t0.pop(s.id, None)
                self._actuation_failed(s, "power_on", f"boot timeout: no successful health check within {limit:g} s")
        if self.hooks is not None and time.monotonic() >= self._next_probe:
            self._next_probe = time.monotonic() + HEALTH_PROBE_S
            await self._probe_online()
        now = time.monotonic()
        self.dc.advance(now)
        if self.meter is not None:
            self.meter.sample([s for s in self.dc.servers if not s.model_power])
        self.dc.update_rate(now)

    async def _probe_online(self) -> None:
        """Check online real servers concurrently; after HEALTH_FAILURES consecutive failures a server leaves service."""
        if not (online := [s for s in self.dc.servers if s.upstream is not None and s.state == "on"]):
            return
        results = await asyncio.gather(*(self.hooks.is_ready(s) for s in online), return_exceptions=True)
        for s, ok in zip(online, results):
            if ok is True:
                self._fails.pop(s.id, None)
                continue
            self._fails[s.id] += 1
            if (n := self._fails[s.id]) >= HEALTH_FAILURES and s.state == "on":
                del self._fails[s.id]
                err = f"{n} consecutive failed health checks ({s.upstream.url}{s.upstream.ready_path})"
                self.dc.server_failed(s, err, time.monotonic())
                log.error("site %s server %s failed: %s; powering on again", self.cfg.id, s.id, err,
                          extra={"event": "node_failed", "site": self.cfg.id, "node": s.id})

    def snapshot(self) -> SiteSnapshot:
        snap = self.dc.summary(time.monotonic(), ts=time.time())
        snap.components = self.meter.status() if self.meter is not None else {}
        return snap
