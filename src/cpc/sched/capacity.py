"""Global capacity policies (plugins): periodic instructions to every site.

In every control period the controller calls policy.decide(view) and receives, per site, the number
of servers to keep online and a power-cap ratio, plus optional routing weights that the gateway
applies with the weighted routing policy.

Inputs: a policy declares what it needs in the class attribute requires (signals: grid signals of
all sites; demand: gateway demand statistics). The controller calls decide() only when these inputs
are complete and otherwise skips the period and records why. Sites whose state is unavailable are
absent from view.sites (the reason is in view.excluded), so policies may index view.signals by site
without handling gaps.

The site's power policy turns the number of online servers into concrete servers (see sched.local).
Pools may be heterogeneous; slots_per_node, node_idle_w and node_peak_w in the snapshot are means,
which the reference implementations here use.

New policy: subclass CapacityPolicy, implement decide(), declare requires, and expose it as an entry
point in the group cpc.capacity. The built-in policies are reference implementations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal

from ..config import ConfigError, SLOConfig
from ..grid.signals import GridSignal
from ..registry import Registry, unknown_params
from ..schemas import CapacityTarget, DemandSnapshot, SiteSnapshot

INPUTS = frozenset({"signals", "demand"})


@dataclass(slots=True)
class ControlView:
    now: datetime
    interval_s: float
    sites: dict[str, SiteSnapshot]  # sites with a usable state in this period
    signals: dict[str, GridSignal]  # covers every site in sites when the policy requires signals
    demand: DemandSnapshot | None  # not None when the policy requires demand
    rtt_ms: dict[str, dict[str, float]]
    slo: SLOConfig
    min_active: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)  # sites left out in this period, with the reason


@dataclass(slots=True)
class CapacityPlan:
    targets: dict[str, CapacityTarget]
    weights: dict[str, dict[str, float]] | None = None  # routing weights source -> site; None: unchanged
    notes: dict = field(default_factory=dict)


class CapacityPolicy:
    name = "base"
    requires: ClassVar[frozenset[str]] = INPUTS

    def __init__(self, **params):
        if params:
            raise unknown_params("capacity policy", self, params)

    def decide(self, view: ControlView) -> CapacityPlan:
        raise NotImplementedError


CAPACITY: Registry[CapacityPolicy] = Registry("capacity")
register_capacity = CAPACITY.register


def make_capacity(name: str, params: dict | None = None) -> CapacityPolicy:
    cls = CAPACITY.get(name)
    if unknown := set(cls.requires) - INPUTS:
        raise ConfigError(f"capacity policy {name} requires unknown inputs {sorted(unknown)}; available: {sorted(INPUTS)}")
    return cls(**(params or {}))


def node_cap_peak_w(s: SiteSnapshot, cap_ratio: float) -> float:
    """Full-load IT power of one server at cap ratio cap_ratio (linear in the profile parameters)."""
    return s.node_idle_w + (s.node_peak_w - s.node_idle_w) * cap_ratio


def max_nodes_under_cap(s: SiteSnapshot, sig: GridSignal, cap_ratio: float = 1.0) -> int:
    """Most servers that fit under the facility power limit at full load (conservative); all servers without a limit."""
    if sig.power_cap_kw is None:
        return s.nodes_total
    if (peak := node_cap_peak_w(s, cap_ratio)) <= 0:
        raise ValueError(f"site {s.site}: invalid full-load server power {peak}")
    return max(0, min(s.nodes_total, int(sig.power_cap_kw * 1000.0 / s.pue // peak)))


def cap_ratio_for(s: SiteSnapshot, sig: GridSignal, nodes: int, min_ratio: float) -> float | None:
    """Largest cap ratio keeping nodes servers at full load under the facility limit; None if even min_ratio fails."""
    if sig.power_cap_kw is None or nodes <= 0 or max_nodes_under_cap(s, sig) >= nodes:
        return 1.0
    if (dyn := s.node_peak_w - s.node_idle_w) <= 0:
        return None
    r = (sig.power_cap_kw * 1000.0 / s.pue / nodes - s.node_idle_w) / dyn
    return None if r < min_ratio else min(1.0, math.floor(r * 1000) / 1000)


@register_capacity("static")
class Static(CapacityPolicy):
    """All servers online without a cap (baseline). Needs neither grid signals nor demand."""

    requires = frozenset()

    def decide(self, view):
        return CapacityPlan({sid: CapacityTarget(active_nodes=s.nodes_total, cap_ratio=1.0) for sid, s in view.sites.items()})


@register_capacity("reactive")
class Reactive(CapacityPolicy):
    """Threshold autoscaling (like the Kubernetes HPA): Little's law estimate kept at a target utilisation.

    servers needed = ceil(max(arrival rate x mean service time, in service + queued) / (slots per server x target_util));
    scale-up is immediate, scale-down removes at most scale_down_step servers per period.
    The facility power limit from the grid is respected (needs grid signals); a limit below
    min_active wins and is recorded in notes.
    """

    requires = frozenset({"signals"})

    def __init__(self, target_util: float = 0.6, scale_down_step: int = 1, **params):
        super().__init__(**params)
        self.target_util = target_util
        self.step = scale_down_step

    def needed(self, s: SiteSnapshot) -> int:
        offered = max(s.arrival_rate * s.est_service_s, float(s.busy + s.queue_len))
        return math.ceil(offered / (max(s.slots_per_node, 1) * self.target_util))

    def decide(self, view):
        targets, capped = {}, {}
        for sid, s in view.sites.items():
            cur, want = s.nodes_on + s.nodes_booting, self.needed(s)
            if want < cur:
                want = max(want, cur - self.step)
            lo, hi = view.min_active.get(sid, 1), max_nodes_under_cap(s, view.signals[sid])
            if hi < lo:
                capped[sid] = {"min_active": lo, "max_under_cap": hi}
            targets[sid] = CapacityTarget(active_nodes=max(min(want, hi), min(lo, hi)), cap_ratio=1.0)
        return CapacityPlan(targets, notes={"cap_below_min_active": capped} if capped else {})


@register_capacity("grid_aware")
class GridAware(CapacityPolicy):
    """Grid-signal-aware capacity allocation (pairs with weighted routing).

    1. Little's law gives the slots needed globally from the arrival rate (demand_source=gateway:
       sum of the per-source rates seen by the gateway; sites: sum of the site rates);
    2. servers are allocated to sites in order of effective price x PUE (converted with the slots per
       server of each site; bounded by server counts and power limits), keeping min_active per site;
    3. the routing weights of each source are proportional to the capacity allocated within its RTT budget;
    4. when the power limit cannot hold min_active servers at full load, the largest cap ratio that
       fits is used (at least cap_min_ratio of the snapshot); if that still fails, as many servers as
       fit are kept and notes records it.
    A source with no allocated site within its budget spreads over all allocated sites; notes.rtt_budget_exceeded lists it.
    """

    requires = frozenset({"signals", "demand"})

    def __init__(self, target_util: float = 0.6, rtt_budget_ms: float = 60.0, headroom: float = 1.1,
                 scale_down_step: int = 1, demand_source: Literal["gateway", "sites"] = "gateway", **params):
        super().__init__(**params)
        if demand_source not in ("gateway", "sites"):
            raise ValueError(f"demand_source must be gateway or sites, got {demand_source}")
        self.target_util = target_util
        self.rtt_budget_ms = rtt_budget_ms
        self.headroom = headroom
        self.step = scale_down_step
        self.demand_source = demand_source

    def decide(self, view):
        sites, sig = view.sites, view.signals
        if not sites:
            return CapacityPlan({})
        rates = view.demand.rates.values() if self.demand_source == "gateway" else (s.arrival_rate for s in sites.values())
        svc = sum(s.est_service_s for s in sites.values()) / len(sites)
        offered = max(sum(rates) * svc, float(sum(s.busy + s.queue_len for s in sites.values())))  # Little's law
        need_slots = offered * self.headroom / self.target_util  # slots per server may differ between sites
        per = {sid: max(s.slots_per_node, 1) for sid, s in sites.items()}

        alloc, caps, limit, infeasible = {}, {}, {}, {}
        for sid, s in sites.items():
            lo = view.min_active.get(sid, 1)
            if (ratio := cap_ratio_for(s, sig[sid], lo, s.cap_min_ratio)) is None:
                ratio = s.cap_min_ratio
                infeasible[sid] = {"min_active": lo, "max_under_cap": max_nodes_under_cap(s, sig[sid], ratio)}
            caps[sid], limit[sid] = ratio, max_nodes_under_cap(s, sig[sid], ratio)
            alloc[sid] = min(lo, limit[sid])
        remaining = max(0.0, need_slots - sum(alloc[sid] * per[sid] for sid in sites))
        for sid in sorted(sites, key=lambda sid: (sig[sid].effective_price * sites[sid].pue, sid)):
            add = min(math.ceil(remaining / per[sid]), limit[sid] - alloc[sid])
            alloc[sid] += add
            remaining = max(0.0, remaining - add * per[sid])
        for sid, s in sites.items():  # scale down at most step servers per period (the power limit comes first)
            if alloc[sid] < (cur := s.nodes_on + s.nodes_booting) - self.step:
                alloc[sid] = min(cur - self.step, limit[sid])

        weights, over_budget = {}, []
        for src, row in view.rtt_ms.items():
            ok = {sid: alloc[sid] for sid in sites if row[sid] <= self.rtt_budget_ms and alloc[sid] > 0}
            if not ok:
                over_budget.append(src)
                ok = {sid: n for sid, n in alloc.items() if n > 0}
            tot = sum(ok.values())
            weights[src] = {sid: round(v / tot, 4) for sid, v in ok.items()} if tot else {}
        targets = {sid: CapacityTarget(active_nodes=alloc[sid], cap_ratio=caps[sid]) for sid in sites}
        notes = {"need_slots": round(need_slots, 2), "unplaced_slots": round(remaining, 2), "demand_source": self.demand_source}
        if infeasible:
            notes["cap_infeasible"] = infeasible
        if over_budget:
            notes["rtt_budget_exceeded"] = over_budget
        return CapacityPlan(targets, weights, notes)
