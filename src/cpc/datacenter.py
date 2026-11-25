"""Data-centre runtime: server state machine, local queueing and dispatch, capacity instructions,
energy metering. Simulation and live experiments share this implementation.

The runtime has no clock of its own: every method takes the current time now (seconds, monotonic;
simulated seconds in simulation, time.monotonic() in live experiments). The driver (discrete-event
simulator or site agent):
- advances time by calling submit (arrival), finish (completion), boot_complete (server ready) and
  apply (capacity instruction) at the corresponding moments;
- executes requests: the callback on_start(job, server, now) says that a request starts on a server;
  the simulator schedules its completion with the performance model, the site agent hands it to an
  executor;
- performs actions: the callback on_action(action) asks for power_on, power_off or set_cap. The
  simulator calls boot_complete at server.boot_until; the site agent calls its executor hooks and
  detects readiness with a health check or boot_until;
- measures power: servers with model_power recompute their power from the server model on every
  state change; the driver writes measured power of real servers after advance(now).

Server states: off -> booting -> on -> draining -> off.
- power on: off -> booting (power_on), then on when ready; back to off if booting fails;
- power off: booting -> off (power_off); on without requests -> off (power_off); on with requests ->
  draining (no new requests), then off after the last request (power_off);
- scale-up turns draining servers back on without a restart. The power policy chooses the servers.

Dispatch: an arriving request goes to the server chosen by the dispatch policy when the queue is
empty, and queues otherwise. The queue is ordered by the priority of the dispatch policy, first come
first served on ties; a full queue or a site without online or booting servers rejects the request
(Overloaded). Freed slots, completed boots and capacity changes dispatch from the head of the queue.

Energy: power is constant between two changes and integrated per server; intervals with missing
power (None) are not integrated but counted as missing time.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import NodeProfile, SiteConfig, UpstreamConfig
from .models import idle_w, peak_w, power_w, service_s
from .schemas import CapacityTarget, NodeSnapshot, PoolSnapshot, SiteSnapshot
from .sched.local import DispatchPolicy, PowerPolicy
from .sched.routing import RequestInfo

STATES = ("off", "booting", "on", "draining")
SERVICE_EWMA = 0.05  # weight of each completed request in the mean service-time estimate


class Overloaded(Exception):
    """The queue is full or no server is online or booting: the data centre rejects the request."""


def _round(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


@dataclass(eq=False)
class Server:
    id: str
    pool: str
    profile: NodeProfile
    slots: int
    upstream: UpstreamConfig | None = None  # real server (OpenAI-compatible instance); None when emulated
    model_power: bool = True  # True: power from the server model; False: measured, written by the driver
    state: str = "off"
    busy: int = 0
    cap_ratio: float = 1.0
    boot_until: float = 0.0
    it_power_w: float | None = 0.0
    gpu_power_w: float | None = None  # accelerator power (measured servers only)
    power_missing: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)  # measured per-device readings (written by the driver, for metrics)
    energy_j: float = 0.0  # IT energy accumulated over intervals with valid power
    measured_s: float = 0.0  # time with valid power
    missing_s: float = 0.0  # time with missing power
    live_s: float = 0.0  # time not off
    last_error: str | None = None
    t: float = 0.0  # time up to which energy is integrated

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
        return NodeSnapshot.model_construct(
            id=self.id, pool=self.pool, state=self.state, slots=self.slots, busy=self.busy, cap_ratio=self.cap_ratio,
            it_power_w=_round(self.it_power_w), gpu_power_w=_round(self.gpu_power_w),
            power_source="model" if self.model_power else "sensors", power_missing=list(self.power_missing),
            last_error=self.last_error)


@dataclass(eq=False)
class Job:
    """A request inside the data centre. payload belongs to the driver (simulator record, site-agent future)."""

    info: RequestInfo
    payload: Any = None
    state: str = "new"  # new / queued / started / cancelled
    t_queued: float | None = None
    server: Server | None = None


@dataclass(frozen=True, slots=True)
class Action:
    kind: str  # power_on / power_off / set_cap
    server: Server
    ratio: float | None = None


@dataclass(frozen=True, slots=True)
class Meter:
    """Accumulated metering: IT energy (J), time with valid / missing power and time not off (server-seconds)."""

    energy_j: float
    measured_s: float
    missing_s: float
    live_s: float

    @property
    def coverage(self) -> float | None:
        total = self.measured_s + self.missing_s
        return None if total == 0 else self.measured_s / total


def _noop(*_a) -> None:
    return None


class Datacenter:
    def __init__(self, cfg: SiteConfig, profiles: dict[str, NodeProfile], *, dispatch: DispatchPolicy, power: PowerPolicy,
                 queue_limit_per_slot: float = 4.0, rate_window_s: float = 60.0, now: float = 0.0,
                 initial_active: int | None = None, emulate_all: bool = False,
                 on_start: Callable[[Job, Server, float], None] = _noop, on_action: Callable[[Action], None] = _noop):
        """emulate_all: emulate real pools with their server model too (used by the simulator)."""
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
            else:
                self.servers += [Server(u.id, p.id, prof, u.max_inflight, upstream=None if emulate_all else u,
                                        model_power=emulate_all) for u in p.upstreams]
        self.by_id = {s.id: s for s in self.servers}
        n_on = len(self.servers) if initial_active is None else max(0, min(int(initial_active), len(self.servers)))
        for i, s in enumerate(self.servers):
            s.t = s.boot_until = now
            # a real server starts in booting and accepts requests once its health check passes
            s.state = ("booting" if s.upstream is not None else "on") if i < n_on else "off"
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
        self.est_service_s = service_s(profiles[cfg.pools[0].profile], 800, 200, 0.5)  # initial value: a typical request
        self.arrival_rate = 0.0
        self._arrivals = 0
        self._rate_t = now
        self.target = CapacityTarget(active_nodes=n_on, cap_ratio=1.0)
        self.actuation_errors: Counter[tuple[str, str]] = Counter()

    # ------------------------------------------------------------ metering

    @staticmethod
    def _integrate(s: Server, now: float) -> None:
        if (dt := now - s.t) <= 0:
            return
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
        """Integrate the energy of all servers up to now (call before writing new measured readings)."""
        for s in self.servers:
            self._integrate(s, now)

    def meter(self, now: float) -> Meter:
        self.advance(now)
        ss = self.servers
        return Meter(*(sum(getattr(s, k) for s in ss) for k in ("energy_j", "measured_s", "missing_s", "live_s")))

    def update_rate(self, now: float) -> None:
        """Exponentially weighted arrival rate (time constant rate_window_s, independent of the call interval)."""
        if (dt := now - self._rate_t) <= 0:
            return
        w = 1.0 - math.exp(-dt / self.rate_window_s)
        self.arrival_rate += w * (self._arrivals / dt - self.arrival_rate)
        self._arrivals, self._rate_t = 0, now

    # ------------------------------------------------------------ requests

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
        """A request arrives: on_start when dispatched at once, queued otherwise; Overloaded when rejected."""
        self._arrivals += 1
        if not self._qn and (s := self.dispatch.select(job.info, self.servers)) is not None:
            self._start(job, s, now)
            return
        if self._qn >= self.queue_limit or not any(s.state in ("on", "booting") for s in self.servers):
            raise Overloaded(f"site {self.cfg.id}: queue full or no server available")
        job.state, job.t_queued = "queued", now
        heapq.heappush(self._queue, (-self.dispatch.priority(job.info), next(self._seq), job))
        self._qn += 1
        self._drain(now)

    def cancel(self, job: Job) -> None:
        """Withdraw a queued request (client disconnected, ...); started requests are not affected."""
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
            if (job := q[0][2]).state == "cancelled":
                heapq.heappop(q)
                continue
            if (s := self.dispatch.select(job.info, self.servers)) is None:
                return
            heapq.heappop(q)
            self._qn -= 1
            self._start(job, s, now)

    def finish(self, s: Server, now: float, service_s: float | None = None) -> None:
        """A request on server s completed (or failed). service_s is its service time, None if unknown."""
        self._integrate(s, now)
        s.busy = max(0, s.busy - 1)
        if service_s is not None:
            self.est_service_s += SERVICE_EWMA * (service_s - self.est_service_s)
        if s.state == "draining" and s.busy == 0:
            s.state = "off"
            self.on_action(Action("power_off", s))
        self._repower(s)
        self._drain(now)

    # ------------------------------------------------------------ server state

    def _set_state(self, s: Server, state: str, now: float) -> None:
        self._integrate(s, now)
        s.state = state
        self._repower(s)

    def boot_complete(self, s: Server, now: float) -> None:
        if s.state == "booting":
            self._set_state(s, "on", now)
            self._drain(now)

    def actuation_failed(self, s: Server, action: str, error: str, now: float) -> None:
        """An executor action failed: recorded in last_error and the failure count; a server failing to boot returns to off."""
        s.last_error = f"{action}: {error}"
        self.actuation_errors[s.id, action] += 1
        if action == "power_on" and s.state == "booting":
            self._set_state(s, "off", now)

    def server_failed(self, s: Server, error: str, now: float) -> None:
        """An online or draining server failed (e.g. repeated failed health checks of its inference instance).

        An online server goes back to booting with a new power_on (the executor restarts the instance,
        which serves again after its health check passes); a draining server was going off and turns
        off. The executor fails the requests in service.
        """
        if s.state not in ("on", "draining"):
            return
        s.last_error = f"health: {error}"
        self.actuation_errors[s.id, "health"] += 1
        if s.state == "on":
            s.boot_until = now + s.profile.boot_s
            self._set_state(s, "booting", now)
            self.on_action(Action("power_on", s))
        else:
            self._set_state(s, "off", now)
            self.on_action(Action("power_off", s))

    def _transition(self, s: Server, to: str, now: float) -> None:
        match to, s.state:
            case "on", "draining":
                self._set_state(s, "on", now)
            case "on", "off":
                s.boot_until = now + s.profile.boot_s
                self._set_state(s, "booting", now)
                self.on_action(Action("power_on", s))
            case "off", "booting":
                self._set_state(s, "off", now)
                self.on_action(Action("power_off", s))
            case "off", "on":
                self._set_state(s, "off" if s.busy == 0 else "draining", now)
                if s.state == "off":
                    self.on_action(Action("power_off", s))
            case ("on" | "off"), _:
                pass
            case _:
                raise ValueError(f"power policy {self.power.name} returned an invalid target state {to} for server {s.id}")

    def apply(self, target: CapacityTarget, now: float) -> None:
        """Apply a capacity instruction: power-cap ratio (clamped to each server's lower bound) and the
        number of online servers (turned into concrete servers by the power policy)."""
        if target.cap_ratio is not None:
            self.target.cap_ratio = min(1.0, max(self.cap_min_ratio, float(target.cap_ratio)))
            for s in self.servers:
                if s.cap_ratio != (r := min(1.0, max(s.profile.cap_min_ratio, float(target.cap_ratio)))):
                    self._integrate(s, now)
                    s.cap_ratio = r
                    self._repower(s)
                    self.on_action(Action("set_cap", s, r))
        if target.active_nodes is not None:
            want = self.target.active_nodes = max(0, min(int(target.active_nodes), len(self.servers)))
            for sid, to in self.power.plan(want, self.servers).items():
                self._transition(self.by_id[sid], to, now)
        self._drain(now)

    # ------------------------------------------------------------ summary

    def summary(self, now: float, ts: float | None = None, detail: bool = True) -> SiteSnapshot:
        """Summary of the data centre (input of the global policies). detail=False leaves out servers,
        pools and energy (the simulator routes every request with it)."""
        ss, n = self.servers, len(self.servers)
        cnt = Counter(s.state for s in ss)
        powers = [s.it_power_w for s in ss]
        it = None if None in powers else sum(powers)
        out = dict(
            site=self.cfg.id, ts=now if ts is None else ts, backend=self.backend, nodes_total=n, nodes_on=cnt["on"],
            nodes_booting=cnt["booting"], nodes_draining=cnt["draining"], slots_per_node=sum(s.slots for s in ss) / n,
            slots_on=self.slots_on, busy=sum(s.busy for s in ss), queue_len=self._qn, queue_limit=self.queue_limit,
            cap_ratio=1.0 if self.target.cap_ratio is None else self.target.cap_ratio, cap_min_ratio=self.cap_min_ratio,
            it_power_w=it, facility_power_w=None if it is None else it * self.cfg.pue, pue=self.cfg.pue,
            power_scope=self.power_scope, est_service_s=self.est_service_s, arrival_rate=self.arrival_rate,
            node_idle_w=sum(s.idle_w for s in ss) / n, node_peak_w=sum(s.peak_w for s in ss) / n,
            energy_it_j=0.0, energy_coverage=None, pools=[], nodes=[], components={})
        if detail:
            m = self.meter(now)
            out.update(
                it_power_w=_round(it), facility_power_w=_round(None if it is None else it * self.cfg.pue),
                est_service_s=round(self.est_service_s, 3), arrival_rate=round(self.arrival_rate, 4),
                energy_it_j=round(m.energy_j, 1), energy_coverage=_round(m.coverage, 4),
                pools=self._pools(), nodes=[s.snapshot() for s in ss])
        return SiteSnapshot.model_construct(**out)

    def _pools(self) -> list[PoolSnapshot]:
        out = []
        for p in self.cfg.pools:
            ss = [s for s in self.servers if s.pool == p.id]
            cnt = Counter(s.state for s in ss)
            out.append(PoolSnapshot.model_construct(
                id=p.id, profile=p.profile, backend="emulated" if ss[0].upstream is None else "openai", total=len(ss),
                on=cnt["on"], booting=cnt["booting"], draining=cnt["draining"],
                slots_per_node=sum(s.slots for s in ss) / len(ss), node_idle_w=ss[0].idle_w, node_peak_w=ss[0].peak_w))
        return out
