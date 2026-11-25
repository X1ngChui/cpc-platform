#import "../../template/cpc-doc.typ": *

= Writing scheduling policies <g-policy>

A scheduling policy is a Python class. When a decision is due, the platform calls a method of the policy
with the current state (a view) and the policy returns its decision. The same class works in simulation
and live experiments. There are four kinds of policies:

#figure(
  tbl(
    columns: (7em, 8em, 1fr),
    header: ("Kind", "Called", "Task"),
    [routing], [for every request at the gateway], [choose a site for the request or reject it (`RoutingPolicy.choose`)],
    [capacity], [every decision period], [servers to keep on and power-cap ratio per site, optionally routing weights (`CapacityPolicy.decide`)],
    [dispatch], [for every request at a site], [choose a server inside the site; order the queue (`DispatchPolicy`)],
    [power], [when a site receives a new number of servers], [decide which servers to switch (`PowerPolicy.plan`)],
  ),
  caption: [Kinds of policies],
)

== Built-in policies

#figure(
  tbl(
    columns: (8em, 11em, 1fr),
    header: ("Name", "Parameters (default)", "Rule"),
    [*routing*], [], [],
    [`least_loaded`], [-], [the site with the lowest load],
    [`nearest`], [`max_wait_s` (0.5), `overflow`], [in order of round-trip time, the first site whose estimated wait is at most `max_wait_s`],
    [`cost_aware`], [`max_load` (0.9), `ttft_budget` (0.5), `latency_weight` (0), `overflow`], [within the latency budget, the site with the lowest "effective price × PUE × energy per request"],
    [`weighted`], [`seed` (0), `max_load` (1.0), `overflow`], [random split by the weights from the capacity policy],
    [*capacity*], [], [],
    [`static`], [-], [all servers always on],
    [`reactive`], [`target_util` (0.6), `scale_down_step` (1)], [servers needed from arrival rate and mean service time, keeping a target utilisation],
    [`grid_aware`], [`target_util` (0.6), `rtt_budget_ms` (60), `headroom` (1.1), `scale_down_step` (1)], [allocates servers in order of effective price and gives routing weights (with `weighted` routing)],
    [*dispatch*], [], [],
    [`least_loaded`], [-], [the server with the lowest occupancy (default)],
    [`efficient_first`], [-], [lowest full-load power per slot first],
    [`affinity`], [`key` (`session_id`)], [requests with the same attribute value (e.g. session) on the same server],
    [*power*], [], [],
    [`ordered`], [-], [power on in configuration order; power off the servers with the fewest requests first (default)],
    [`efficient`], [-], [keep servers with low full-load power per slot],
  ),
  caption: [Built-in policies],
)

The `overflow` parameter of routing policies decides what happens when the preferred condition fails:
`least_loaded` (default, the least-loaded available site) or `reject` (reject the request). The built-in
policies show the interfaces and data paths and serve as baselines for comparison.

== What policies see

*Request (`RequestInfo`).* `req.source` (source region), `req.input_tokens`, `req.output_tokens`, `req.id`,
`req.attrs` (extension fields selected by `workload.attributes`, e.g. `req.attrs["session_id"]`).

*Cluster view (`ClusterView`, routing).* `view.now` (signal time), `view.sites` (site id → site view, sites
with a usable state only), `view.rtt(source, site)` (round-trip time, ms), `view.slo`, `view.weights`
(routing weights from the capacity policy), `view.est_ttft_s(req, site_view)` (estimated TTFT without prefill).

*Site view (`SiteView`).* `sv.id`; `sv.signal` (grid signal, `None` when missing); `sv.load` ((in service +
queued + in flight) / online slots); `sv.est_wait_s` (estimated queueing time); `sv.available` (online
servers and room in the queue); `sv.snap` (state summary of the site).

*State summary (`sv.snap`, and `view.sites[site]` for capacity policies).* Frequently used: `nodes_total`,
`nodes_on`, `nodes_booting` (servers); `slots_on`, `busy`, `queue_len` (online slots, requests in service,
queued requests); `arrival_rate`, `est_service_s` (estimates of arrival rate and mean service time);
`it_power_w`, `facility_power_w`, `pue`; `node_idle_w`, `node_peak_w`, `slots_per_node` (idle and full-load
power and slots of servers, means for sites with several server types); `pools` (per-pool details).

*Grid signal (`GridSignal`).* `price`, `price_label` (period, e.g. `peak`), `carbon_intensity`, `currency`,
`power_cap_kw` (facility power limit, `None` without one), `dr_event`, `pue`, `carbon_price`, `extra`
(extra signals), `effective_price` (price + carbon price × carbon intensity).

*View of capacity policies (`ControlView`).* `view.now`, `view.interval_s`, `view.sites`, `view.signals`
(site → grid signal), `view.demand.rates` (source region → arrival rate), `view.rtt_ms`, `view.min_active`,
`view.excluded` (sites without a usable state in this period and why).

*Server view (`ServerView`, local policies).* `s.id`, `s.pool`, `s.state` (`off`, `booting`, `on`,
`draining`), `s.busy`, `s.slots`, `s.cap_ratio`, `s.accepting` (whether it accepts new requests),
`s.peak_w`, `s.idle_w`.

== A first policy: the site with the lowest carbon intensity

The steps below turn a routing policy into a separate Python package used in simulation and live experiments.

*Step 1: create the package.* In a directory outside the repository:

```text
my-policies/
├── pyproject.toml
└── my_policies/
    ├── __init__.py          (empty)
    └── routing.py
```

`my_policies/routing.py`:

```python
from cpc.sched import RouteDecision, RoutingPolicy


class Greenest(RoutingPolicy):
    """Among available sites with a grid signal and load below max_load, the lowest carbon intensity."""

    def __init__(self, max_load: float = 0.9, **params):
        super().__init__(**params)          # misspelt parameters in the scenario fail here
        self.max_load = max_load

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)     # available sites not yet tried for this request
        if not cands:
            return RouteDecision.reject("no_available_site")
        ok = [sv for sv in cands if sv.signal is not None and sv.load < self.max_load]
        if not ok:                                 # no qualifying site: the least-loaded one
            return self.overflow("least_loaded", cands, "no_candidate")
        best = min(ok, key=lambda sv: (sv.signal.carbon_intensity, sv.id))
        return RouteDecision.to(best.id, "min_carbon")
```

`RouteDecision.to(site, reason)` chooses a site, `RouteDecision.reject(reason)` rejects. The reason is a
short identifier that appears in the `route_reasons` counts of results and in metrics, showing what the
policy decided in which situation.

*Step 2: registration.* `pyproject.toml`:

```toml
[project]
name = "my-policies"
version = "0.1.0"
dependencies = ["cpc-platform"]

[project.entry-points."cpc.routing"]
greenest = "my_policies.routing:Greenest"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Each line under `[project.entry-points."cpc.routing"]` maps a name to a policy class. Capacity, dispatch and
power policies use the groups `cpc.capacity`, `cpc.dispatch` and `cpc.power`.

*Step 3: install and use.* In the virtual environment of the platform:

```bash
pip install -e ./my-policies
cpc sim -s examples/quickstart/scenario.yaml --hours 0.9 --routing greenest \
    --out results/greenest
```

or set `scheduler.routing: {policy: greenest, params: {max_load: 0.8}}` in the scenario; variants of
experiment files use it the same way. Live experiments use the same policy: `cpc live -s ... --routing
greenest` or `cpc gateway --policy greenest`. Code changes need no reinstallation (`-e` installs in
editable mode).

== Capacity policies

Every decision period a capacity policy returns a `CapacityPlan`: per site a
`CapacityTarget(active_nodes=servers on, cap_ratio=power-cap ratio)`, optional routing weights `weights`
(source region → site → weight) and notes `notes` for the decision record. The class attribute `requires`
declares the inputs needed, and the platform calls the policy only with these inputs complete (otherwise it
skips the period and records why):

```python
import math

from cpc.schemas import CapacityTarget
from cpc.sched import CapacityPlan, CapacityPolicy


class ByPrice(CapacityPolicy):
    """Sites with a price above threshold keep min_active servers; the others follow the load."""

    # grid signals needed; the other input is "demand" (arrival rate per source region)
    requires = frozenset({"signals"})

    def __init__(self, threshold: float = 1.0, target_util: float = 0.6, **params):
        super().__init__(**params)
        self.threshold, self.target_util = threshold, target_util

    def decide(self, view):
        targets = {}
        for sid, s in view.sites.items():
            busy = s.arrival_rate * s.est_service_s             # estimated concurrent requests
            need = math.ceil(busy / (s.slots_per_node * self.target_util))
            if view.signals[sid].price > self.threshold:
                need = 0
            n = max(need, view.min_active.get(sid, 1))
            targets[sid] = CapacityTarget(active_nodes=n)
        return CapacityPlan(targets)
```

Register it in the group `cpc.capacity`. The number of servers is clamped between 0 and the servers of the
site; the power policy of the site decides which ones to switch.

== Local policies

By default a dispatch policy filters the servers that can accept the request with `filter(req, server)`,
scores them with `score(req, server)` and takes the lowest score; without a candidate the request queues,
and `priority(req)` orders the queue (higher first; default 0, first come first served). Usually only
`score` needs overriding:

```python
from cpc.sched import DispatchPolicy


class TenantPools(DispatchPolicy):
    """Requests of tenant a use only pool fast; other requests use all servers by occupancy."""

    def filter(self, req, s):
        return s.accepting and (req.attrs.get("tenant") != "a" or s.pool == "fast")

    def score(self, req, s):
        return s.busy / s.slots
```

Register it in the group `cpc.dispatch` and refer to it in `scheduler.dispatch` of a site (the scenario needs
`workload.attributes: [tenant]`). Queueing by request priority overrides `priority`, e.g.
`return float(req.attrs.get("priority", 0))` (with `workload.attributes: [priority]`). Power policies
implement `plan(want, servers)` and return the servers that change state with their target, e.g.
`{"nm-h100-01": "off"}`; they register in the group `cpc.power`.

== Hints for writing policies

- use the time of the view `view.now`, never the system clock, so that the policy behaves the same in
  simulation and live experiments;
- do not modify objects of the view; keep state (such as the last decision) in attributes of the policy;
- grid signals may be missing (`sv.signal is None`); routing policies should handle this explicitly with a reason;
- routing policies run for every request and should avoid expensive computation;
- debug in short simulations first, compare with baselines through `cpc run`, then validate live.
