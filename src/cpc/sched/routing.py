"""Global routing policies (plugins): choose a site for every request.

The gateway calls policy.choose(req, view, exclude) for each request and receives a RouteDecision:
the chosen site (None to reject) and the reason, which goes to the request log and to the metric
cpc_gateway_route_decisions_total{policy,reason}. The simulator calls the same interface, so a
policy validated in simulation runs unchanged in live experiments.

The view holds only sites with a usable state (the gateway removes sites whose state could not be
fetched or is stale). SiteView.signal is None when the grid signal is missing; policies that need
signals must leave such sites out rather than assume a value.

New policy: subclass RoutingPolicy, implement choose(), and expose it as an entry point in the group
cpc.routing (see cpc.registry). The built-in policies are reference implementations.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal, get_args

from ..config import SLOConfig
from ..grid.signals import GridSignal
from ..registry import Registry, unknown_params
from ..schemas import SiteSnapshot

INF = float("inf")


@dataclass(slots=True)
class RequestInfo:
    """Request information visible to policies. attrs holds the workload extension fields selected by
    workload.attributes (carried as x-cpc-attr-* headers in live experiments)."""

    source: str
    input_tokens: int = 0  # approximate in live experiments (see site.executors.approx_prompt_tokens)
    output_tokens: int | None = None  # requested output limit; None if not given
    id: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Routing decision: site None means reject. reason is a short identifier from a small set (a
    metric label); detail carries additional information."""

    site: str | None
    reason: str
    detail: dict = field(default_factory=dict)

    @classmethod
    def to(cls, site: str, reason: str, **detail) -> RouteDecision:
        return cls(site, reason, detail)

    @classmethod
    def reject(cls, reason: str, **detail) -> RouteDecision:
        return cls(None, reason, detail)


@dataclass(slots=True)
class SiteView:
    snap: SiteSnapshot
    signal: GridSignal | None = None  # None when the grid signal is missing
    pending: int = 0  # requests sent since the last state refresh and not yet in the snapshot

    @property
    def id(self) -> str:
        return self.snap.site

    @property
    def outstanding(self) -> int:
        return self.snap.busy + self.snap.queue_len + self.pending

    @property
    def load(self) -> float:
        """Load = (in service + queued + in flight) / online slots."""
        return self.outstanding / self.snap.slots_on if self.snap.slots_on else INF

    @property
    def est_wait_s(self) -> float:
        """Estimated queueing delay: requests beyond the online slots / service rate."""
        s = self.snap
        if s.slots_on == 0:
            return INF
        excess = self.outstanding - s.slots_on
        return 0.0 if excess < 0 else (excess + 1) * s.est_service_s / s.slots_on

    @property
    def available(self) -> bool:
        s = self.snap
        return s.slots_on > 0 and s.queue_len + self.pending < s.queue_limit


@dataclass(slots=True)
class ClusterView:
    now: datetime
    sites: dict[str, SiteView]
    rtt_ms: dict[str, dict[str, float]]
    slo: SLOConfig
    weights: dict[str, dict[str, float]] = field(default_factory=dict)  # from the capacity policy (source -> site)

    def rtt(self, source: str, site: str) -> float:
        return self.rtt_ms[source][site]

    def est_ttft_s(self, req: RequestInfo, sv: SiteView) -> float:
        """Estimated TTFT without prefill: round-trip time + queueing delay."""
        return self.rtt(req.source, sv.id) / 1000.0 + sv.est_wait_s


Overflow = Literal["least_loaded", "reject"]


def _overflow_mode(v: str) -> Overflow:
    if v not in get_args(Overflow):
        raise ValueError(f"overflow must be least_loaded or reject, got {v}")
    return v  # type: ignore[return-value]


class RoutingPolicy:
    name = "base"

    def __init__(self, **params):
        if params:
            raise unknown_params("routing policy", self, params)

    def choose(self, req: RequestInfo, view: ClusterView, exclude: Iterable[str] = ()) -> RouteDecision:
        raise NotImplementedError

    @staticmethod
    def candidates(view: ClusterView, exclude: Iterable[str] = ()) -> list[SiteView]:
        """Sites with online slots and room in the queue, not in exclude."""
        ex = set(exclude)
        return [sv for sid, sv in view.sites.items() if sid not in ex and sv.available]

    @staticmethod
    def least_loaded(cands: list[SiteView]) -> str | None:
        return min(cands, key=lambda sv: (sv.load, sv.id)).id if cands else None

    def overflow(self, mode: Overflow, cands: list[SiteView], cause: str) -> RouteDecision:
        """Explicit overflow rule when the preferred condition of a policy fails: take the least-loaded
        available site, or reject. The reason names the condition."""
        if mode == "reject":
            return RouteDecision.reject(cause)
        sid = self.least_loaded(cands)
        return RouteDecision.to(sid, f"overflow_{cause}") if sid else RouteDecision.reject("no_available_site")


ROUTING: Registry[RoutingPolicy] = Registry("routing")
register_routing = ROUTING.register


def make_routing(name: str, params: dict | None = None) -> RoutingPolicy:
    return ROUTING.get(name)(**(params or {}))


@register_routing("least_loaded")
class LeastLoaded(RoutingPolicy):
    """Lowest load (baseline)."""

    def choose(self, req, view, exclude=()):
        sid = self.least_loaded(self.candidates(view, exclude))
        return RouteDecision.to(sid, "least_loaded") if sid else RouteDecision.reject("no_available_site")


@register_routing("nearest")
class Nearest(RoutingPolicy):
    """Nearest first: in order of RTT, the first site whose estimated wait is at most max_wait_s;
    otherwise overflow (least_loaded: the least-loaded site; reject: reject)."""

    def __init__(self, max_wait_s: float = 0.5, overflow: str = "least_loaded", **params):
        super().__init__(**params)
        self.max_wait_s = max_wait_s
        self.mode = _overflow_mode(overflow)

    def choose(self, req, view, exclude=()):
        cands = sorted(self.candidates(view, exclude), key=lambda sv: view.rtt(req.source, sv.id))
        if not cands:
            return RouteDecision.reject("no_available_site")
        if sv := next((sv for sv in cands if sv.est_wait_s <= self.max_wait_s), None):
            return RouteDecision.to(sv.id, "nearest")
        return self.overflow(self.mode, cands, "wait_exceeded")


@register_routing("cost_aware")
class CostAware(RoutingPolicy):
    """Greedy energy-cost-aware routing: among sites with a grid signal, within the TTFT budget and
    below the load limit, the one with the lowest marginal cost per request.

    marginal cost = effective price (price + carbon price x carbon intensity) x PUE x marginal energy
                    per request + latency_weight x estimated TTFT
    marginal energy per request ~ (peak - idle server power) / slots x mean service time.
    Without a qualifying site the request overflows; the reason tells "no grid signal" (no_signal)
    from "budget or load exceeded" (over_budget).
    """

    def __init__(self, max_load: float = 0.9, ttft_budget: float = 0.5, latency_weight: float = 0.0,
                 overflow: str = "least_loaded", **params):
        super().__init__(**params)
        self.max_load = max_load
        self.ttft_budget = ttft_budget  # fraction of the TTFT objective
        self.latency_weight = latency_weight  # currency per second
        self.mode = _overflow_mode(overflow)

    @staticmethod
    def marginal_cost(sv: SiteView) -> float:
        s, g = sv.snap, sv.signal
        assert g is not None
        e_kwh = (s.node_peak_w - s.node_idle_w) / max(s.slots_per_node, 1) * s.est_service_s / 3.6e6
        return g.effective_price * s.pue * e_kwh

    def choose(self, req, view, exclude=()):
        if not (cands := self.candidates(view, exclude)):
            return RouteDecision.reject("no_available_site")
        if not (signed := [sv for sv in cands if sv.signal is not None]):
            return self.overflow(self.mode, cands, "no_signal")
        budget = self.ttft_budget * view.slo.ttft_s
        if not (ok := [sv for sv in signed if sv.load < self.max_load and view.est_ttft_s(req, sv) <= budget]):
            return self.overflow(self.mode, cands, "over_budget")
        best = min(ok, key=lambda sv: (self.marginal_cost(sv) + self.latency_weight * view.est_ttft_s(req, sv), sv.id))
        return RouteDecision.to(best.id, "min_cost", unsigned=len(cands) - len(signed))


@register_routing("weighted")
class Weighted(RoutingPolicy):
    """Random split by the weights of the capacity policy (share of each source's traffic per site).

    Overflow when the source has no positive weight on an available site (no_weights), or when the
    drawn site has a load of at least max_load (target_full).
    """

    def __init__(self, seed: int = 0, max_load: float = 1.0, overflow: str = "least_loaded", **params):
        super().__init__(**params)
        self.rng = random.Random(seed)
        self.max_load = max_load
        self.mode = _overflow_mode(overflow)

    def choose(self, req, view, exclude=()):
        if not (cands := {sv.id: sv for sv in self.candidates(view, exclude)}):
            return RouteDecision.reject("no_available_site")
        w = {k: v for k, v in sorted(view.weights.get(req.source, {}).items()) if k in cands and v > 0}
        if not w:
            return self.overflow(self.mode, list(cands.values()), "no_weights")
        sid = self.rng.choices(list(w), weights=list(w.values()))[0]
        if cands[sid].load >= self.max_load:
            return self.overflow(self.mode, list(cands.values()), "target_full")
        return RouteDecision.to(sid, "weighted")
