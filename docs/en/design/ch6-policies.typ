#import "../../template/cpc-doc.typ": *

= Scheduling policies <ch-policy>

The platform defines four policy interfaces: global routing and capacity policies, local dispatch and
power policies. Each is called the same way in simulation and live experiments; policies read the view
they receive and return a decision without modifying platform state. The built-in policies are reference
implementations that show the interfaces and data paths; they are not recommended algorithms. Parameters
are given in the scenario (`{policy: name, params: {...}}`); unknown parameters fail at construction.

== Routing policies (global)

For every request the gateway calls `choose(req, view, exclude)` and receives a `RouteDecision`: the site
(`None` to reject), the reason (a short identifier from a small set, used as a metric label) and details.
`exclude` holds the sites that already failed for this request. The simulator calls the same interface.

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("Input", "Content"),
    [`RequestInfo`], [source region `source`; input tokens `input_tokens` (approximate in live experiments); output limit `output_tokens`; identifier `id`; extension attributes `attrs`],
    [`ClusterView`], [signal time `now`; site views `sites`; RTT matrix; SLO; routing weights `weights` from the capacity policy; methods `rtt(source, site)`, `est_ttft_s(req, site_view)`],
    [`SiteView`], [state summary `snap`; grid signal `signal` (`None` when missing); requests sent but not yet in the summary `pending`; derived `load` ((in service + queued + in flight) / online slots), `est_wait_s` (estimated queueing delay), `available` (online slots and room in the queue)],
  ),
  caption: [Inputs of routing policies],
)

The view holds only sites with a usable state. When the preferred condition of a policy fails, an
explicit overflow rule applies (parameter `overflow`): `least_loaded` takes the least-loaded available site
with the reason `overflow_<condition>`; `reject` rejects the request with the condition as reason.

#figure(
  tbl(
    columns: (7em, 13em, 1fr),
    header: ("Name", "Parameters (default)", "Rule"),
    [`least_loaded`], [-], [the available site with the lowest load (baseline).],
    [`nearest`], [`max_wait_s` (0.5), `overflow`], [in order of RTT, the first site whose estimated wait is at most `max_wait_s`; otherwise overflow (`wait_exceeded`).],
    [`cost_aware`], [`max_load` (0.9), `ttft_budget` (0.5), `latency_weight` (0), `overflow`], [among sites with a grid signal, load below `max_load` and estimated TTFT within `ttft_budget` × SLO, the one minimising "effective price × PUE × marginal energy per request + `latency_weight` × estimated TTFT". Marginal energy per request = (full-load − idle IT power) / slots × mean service time. Overflow without signals (`no_signal`) or when budget or load fail (`over_budget`).],
    [`weighted`], [`seed` (0), `max_load` (1.0), `overflow`], [random split by the weights of the capacity policy (share of a source's traffic per site); overflow when the source has no positive weight on an available site (`no_weights`) or the drawn site has a load of at least `max_load` (`target_full`).],
  ),
  caption: [Reference routing policies],
)

== Capacity policies (global)

In every control period the controller calls `decide(view)` and receives a `CapacityPlan`: an instruction
per site (online servers, power-cap ratio), optional routing weights (sent to the gateway, used by
`weighted` routing) and notes (written to the decision log).

*Complete inputs.* A policy declares the inputs it needs in the class attribute `requires`: `signals`
(grid signals of all sites) and `demand` (gateway demand statistics). The controller and the simulator call
`decide()` only when these inputs are complete and otherwise skip the period and record why; sites without
a usable state are absent from `view.sites`, with the reason in `view.excluded`. Policies can therefore
index the signals by site without handling gaps.

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ([`ControlView` field], [Content]),
    [`now`, `interval_s`], [signal time and control period],
    [`sites`], [state summaries of the sites usable in this period],
    [`signals`], [grid signals of the sites (covering all of `sites` when required)],
    [`demand`], [arrival rate per source region (not `None` when required)],
    [`rtt_ms`, `slo`, `min_active`, `excluded`], [RTT matrix, SLO, minimum online servers per site, excluded sites and reasons],
  ),
  caption: [Inputs of capacity policies],
)

#figure(
  tbl(
    columns: (7em, 13em, 1fr),
    header: ("Name", "Parameters (default)", "Rule"),
    [`static`], [-], [all servers online without a cap (baseline). Needs neither signals nor demand.],
    [`reactive`], [`target_util` (0.6), `scale_down_step` (1)], [Little's law estimate of the concurrency needed: ceil(max(arrival rate × mean service time, in service + queued) / (slots per server × target utilisation)); scale-up immediately, scale-down by at most `scale_down_step` servers per period; respects the facility power limit (needs signals), and a limit below `min_active` wins and is recorded.],
    [`grid_aware`], [`target_util` (0.6), `rtt_budget_ms` (60), `headroom` (1.1), `scale_down_step` (1), `demand_source` (`gateway`)], [estimates the slots needed from the global arrival rate and allocates servers to sites in order of "effective price × PUE" (bounded by server counts and power limits, keeping `min_active` per site); the routing weights of a source are proportional to the capacity allocated within its RTT budget; when the power limit cannot hold `min_active` servers at full load, the largest cap ratio that fits is used, and if that still fails, as many servers as fit are kept and the fact is recorded.],
  ),
  caption: [Reference capacity policies],
)

`cpc.sched.capacity` provides two helpers: `max_nodes_under_cap(snap, signal, cap_ratio)` gives the most
servers that fit under the facility power limit (conservatively at full load), and
`cap_ratio_for(snap, signal, nodes, min_ratio)` the largest cap ratio keeping a number of servers at full
load within the limit.

== Local policies <sec-local-policy>

=== Dispatch policies

Dispatch policies choose a server for requests reaching the site, following the filter and score
extension points of the Kubernetes scheduling framework @k8s-scheduling-framework:

- `filter(req, server)`: whether the server can accept the request (default: on with a free slot);
- `score(req, server)`: a score, the lowest wins;
- `select(req, servers)`: by default the lowest score after filtering; may be replaced as a whole;
- `priority(req)`: queue priority, higher first (default 0: first come, first served).

The server view `ServerView` provides identifier, pool, state, requests in service, slots, cap ratio,
whether it accepts requests, and the full-load and idle IT power of the server model (`peak_w`, `idle_w`).

#figure(
  tbl(
    columns: (8em, 9em, 1fr),
    header: ("Name", "Parameters (default)", "Rule"),
    [`least_loaded`], [-], [the server with the lowest occupancy (load balancing inside the site, default).],
    [`efficient_first`], [-], [lowest full-load power per slot first, balanced by occupancy among equal servers (heterogeneous pools).],
    [`affinity`], [`key` (`session_id`)], [preferred server by the hash of the request attribute `key`, so that requests of one session land on one server and can reuse its KV cache; by occupancy when the attribute is missing.],
  ),
  caption: [Reference dispatch policies],
)

=== Power policies

`plan(want, servers)` turns the number of online servers `want` into concrete servers and returns the
servers that change state with their target (`on` or `off`); the runtime follows the state machine
(@ch-runtime).

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("Name", "Rule"),
    [`ordered`], [scale-up resumes draining servers first, then starts servers that are off in configuration order; scale-down cancels booting servers first, then stops the online servers with the fewest requests (default).],
    [`efficient`], [keeps servers with low full-load power per slot: scale-up starts efficient servers first, scale-down stops inefficient ones first (heterogeneous pools).],
  ),
  caption: [Reference power policies],
)

== Writing policies

A policy is a Python class: subclass the base class, implement the decision method, and take the
scenario's `params` as constructor arguments. Example: a routing policy choosing the available site with
the lowest carbon intensity.

```python
from cpc.sched import RouteDecision, RoutingPolicy


class Greenest(RoutingPolicy):
    """Among available sites with a grid signal and load below max_load, the one with the lowest
    carbon intensity; otherwise overflow to the least-loaded site."""

    def __init__(self, max_load: float = 0.9, **params):
        super().__init__(**params)       # unknown parameters fail here
        self.max_load = max_load

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)
        if not cands:
            return RouteDecision.reject("no_available_site")
        ok = [sv for sv in cands if sv.signal is not None and sv.load < self.max_load]
        if not ok:
            return self.overflow("least_loaded", cands, "no_candidate")
        best = min(ok, key=lambda sv: (sv.signal.carbon_intensity, sv.id))
        return RouteDecision.to(best.id, "min_carbon")
```

Local policies are written the same way: a dispatch policy that pins requests of a `tenant` attribute to a
pool only overrides `filter`; one that queues by the request attribute `priority` only overrides `priority`
(returning `float(req.attrs.get("priority", 0))`).

*Registration.* Built-in policies register with a decorator (`ROUTING.register("name")`, ...). External
policies are entry points of a Python package in the groups `cpc.routing`, `cpc.capacity`, `cpc.dispatch`
and `cpc.power` (power sensors: `cpc.sensors`):

```toml
# pyproject.toml of the policy package
[project.entry-points."cpc.routing"]
greenest = "my_policies.routing:Greenest"
```

Once the package is installed, scenarios refer to it as `scheduler.routing: {policy: greenest}`; a name
that clashes with a built-in or another package fails at loading.
