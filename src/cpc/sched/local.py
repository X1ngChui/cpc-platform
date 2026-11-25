"""Local policies (inside a data centre): dispatch and power management.

- DispatchPolicy chooses a server for a request that reaches the site. Like the Kubernetes
  scheduling framework it filters the servers that can take the request and scores them; the lowest
  score wins, and without a candidate the request queues. priority() orders the queue (higher first,
  first come first served on ties).
- PowerPolicy turns the number of online servers set by the capacity policy into a target state
  (on / off) per server; the site runtime performs the transitions (off -> booting -> on,
  on -> draining -> off).

Policies read ServerView (server state) and RequestInfo (with the extension attributes attrs) and
never modify them; simulation and live experiments share the implementations.
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Protocol

from ..registry import Registry, unknown_params
from .routing import RequestInfo


class ServerView(Protocol):
    """Server state visible to local policies."""

    id: str
    pool: str
    state: str  # off / booting / on / draining
    busy: int
    slots: int
    cap_ratio: float

    @property
    def accepting(self) -> bool: ...

    @property
    def peak_w(self) -> float: ...  # full-load IT power (profile parameter)

    @property
    def idle_w(self) -> float: ...  # online idle IT power (profile parameter)


def occupancy(s: ServerView) -> float:
    return s.busy / s.slots


def efficiency(s: ServerView) -> float:
    """Full-load power per concurrency slot (lower is more efficient)."""
    return s.peak_w / s.slots


class DispatchPolicy:
    name = "base"

    def __init__(self, **params):
        if params:
            raise unknown_params("dispatch policy", self, params)

    def filter(self, req: RequestInfo, s: ServerView) -> bool:
        return s.accepting

    def score(self, req: RequestInfo, s: ServerView) -> float:
        raise NotImplementedError

    def select(self, req: RequestInfo, servers: Iterable[ServerView]) -> ServerView | None:
        return min((s for s in servers if self.filter(req, s)), key=lambda s: self.score(req, s), default=None)

    def priority(self, req: RequestInfo) -> float:
        return 0.0


class PowerPolicy:
    name = "base"

    def __init__(self, **params):
        if params:
            raise unknown_params("power policy", self, params)

    def plan(self, want: int, servers: list[ServerView]) -> dict[str, str]:
        """Servers whose state should change, with the target state ("on" / "off"); want counts online and booting servers."""
        raise NotImplementedError


DISPATCH: Registry[DispatchPolicy] = Registry("dispatch")
POWER: Registry[PowerPolicy] = Registry("power")


def make_dispatch(name: str, params: dict | None = None) -> DispatchPolicy:
    return DISPATCH.get(name)(**(params or {}))


def make_power(name: str, params: dict | None = None) -> PowerPolicy:
    return POWER.get(name)(**(params or {}))


@DISPATCH.register("least_loaded")
class LeastLoaded(DispatchPolicy):
    """Lowest occupancy (requests in service / slots): load balancing."""

    def score(self, req, s):
        return occupancy(s)


@DISPATCH.register("efficient_first")
class EfficientFirst(DispatchPolicy):
    """Lowest full-load power per slot first, balanced by occupancy among equal servers (heterogeneous pools)."""

    def score(self, req, s):
        return efficiency(s) + 1e-6 * occupancy(s)


@DISPATCH.register("affinity")
class Affinity(DispatchPolicy):
    """Preferred server by the hash of the request attribute key (e.g. session_id); the least-occupied
    server when the preferred one cannot accept or the attribute is missing.

    Shows the use of workload extension fields: requests of one session land on one server, which can
    reuse its KV cache.
    """

    def __init__(self, key: str = "session_id", **params):
        super().__init__(**params)
        self.key = key

    def select(self, req, servers):
        if not (cands := sorted((s for s in servers if self.filter(req, s)), key=lambda s: s.id)):
            return None
        if (v := req.attrs.get(self.key)) is None:
            return min(cands, key=occupancy)
        return cands[int.from_bytes(hashlib.blake2b(str(v).encode(), digest_size=8).digest(), "big") % len(cands)]


def _live(servers: list[ServerView]) -> list[ServerView]:
    return [s for s in servers if s.state in ("on", "booting")]


@POWER.register("ordered")
class Ordered(PowerPolicy):
    """Scale-up resumes draining servers first, then starts servers that are off in configuration order;
    scale-down cancels booting servers first, then stops the online servers with the fewest requests."""

    def plan(self, want, servers):
        live = _live(servers)
        if want > len(live):
            order = [s for s in servers if s.state == "draining"] + [s for s in servers if s.state == "off"]
            return dict.fromkeys((s.id for s in order[:want - len(live)]), "on")
        order = [s for s in live if s.state == "booting"] + sorted((s for s in live if s.state == "on"), key=lambda s: s.busy)
        return dict.fromkeys((s.id for s in order[:max(0, len(live) - want)]), "off")


@POWER.register("efficient")
class Efficient(PowerPolicy):
    """Keeps the servers with the lowest full-load power per slot: scale-up starts efficient servers
    first, scale-down stops inefficient ones first (heterogeneous pools)."""

    def plan(self, want, servers):
        live = _live(servers)
        if want > len(live):
            idle = sorted((s for s in servers if s.state in ("draining", "off")), key=lambda s: (s.state != "draining", efficiency(s)))
            return dict.fromkeys((s.id for s in idle[:want - len(live)]), "on")
        order = sorted(live, key=lambda s: (s.state != "booting", -efficiency(s), s.busy))
        return dict.fromkeys((s.id for s in order[:max(0, len(live) - want)]), "off")
