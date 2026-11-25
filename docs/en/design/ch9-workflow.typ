#import "../../template/cpc-doc.typ": *

= Simulation and live experiments <ch-workflow>

== Discrete-event simulation

The simulator (`cpc.sim.des.Simulator`) drives the data-centre runtimes of all sites on a simulated clock
with these events:

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("Event", "Handling"),
    [request arrival], [streamed in trace order (not queued as events in advance). The cluster view is built and the routing policy called; a chosen site is reached after the one-way network delay (half the RTT).],
    [arrival at the site], [submitted to the site's data-centre runtime; a rejection counts as rejected.],
    [start of service], [callback of the runtime. Prefill time, time per output token and service time come from the performance model; TTFT and end-to-end latency (with the round-trip time) are recorded and the completion is scheduled.],
    [completion], [reported to the runtime, which frees the slot and dispatches from the queue.],
    [end of booting], [scheduled at `boot_until` when the runtime issues a power-on; stale when booting was cancelled and repeated.],
    [control period], [every `scheduler.sim_control_interval_s`: site arrival rates are updated; with complete inputs `decide()` is called, the instructions applied and the routing weights updated; otherwise the period is skipped and counted.],
  ),
  caption: [Simulation events],
)

Grid signals are refreshed every simulated minute. The simulated duration must not exceed the transformed
workload, or the run fails. All servers (including real pools of the scenario) are emulated with the server model.

== Live experiments

A live experiment consists of the following service processes, which synchronise the signal clock from
the grid-signal service:

- *grid-signal service* (`cpc grid`): loads the grid-signal datasets and serves signals and
  demand-response events;
- *site agents* (`cpc site`, one per site): drive the data-centre runtime on real time; emulated servers
  are timed by the model, requests to real servers are forwarded to inference instances, and their power
  is sampled from sensors; executor hooks start and stop containers, set power limits and check readiness;
- *gateway* (`cpc gateway`): polls site states and grid signals, calls the routing policy, forwards
  requests and counts the arrival rate per source region;
- *controller* (`cpc controller`): collects periodically, calls the capacity policy, sends instructions to
  the site agents and routing weights to the gateway.

`cpc live` replays the workload: it sends requests following the workload dataset and the transformation
of the scenario, with the source assignment of the simulation and extension attributes as headers.

*Running an experiment (`cpc live`).* An experiment starts the services (in this process, as with
`cpc up`, without an endpoints file; with one, it connects to deployed services), waits until all are
ready, replays the workload while sampling the site summaries, grid signals and controller decisions every
sampling period, and writes the result package (@sec-results). Request records come from the replay:
HTTP 200 is served, 503 rejected, other statuses, connection failures and incomplete streams failed. Site
energy is the difference of the accumulated site energy, so results are defined as in simulation and can
be compared directly.

`cpc up` starts all services in one process and keeps them running, for manual operation and
demonstrations; distributed deployments are described in the guide.

== Experiment descriptions

An experiment description (YAML) gives a scenario and variants overriding policies or workload parameters:

```yaml
name: policies
scenario: ../../configs/scenario.yaml     # relative to the experiment file
hours: 24                                 # simulated hours
bin_s: 300                                # time bin of the time series
variants:
  - name: nearest-static
    routing:  {policy: nearest, params: {max_wait_s: 0.5}}
    capacity: {policy: static}
  - name: weighted-gridaware
    routing:  {policy: weighted}
    capacity: {policy: grid_aware}
    workload: {rate_scale: 1.5}           # overrides a field of the scenario workload
```

`cpc run <experiment>` runs the variants in parallel processes; results go to `<out>/<variant>/` and the
comparison to `<out>/comparison.csv`. `cpc sim` runs one simulation, with command-line options overriding
policies and workload parameters of the scenario. In Python, `cpc.simulate()` runs a simulation and
`cpc.read_run()` reads a result directory. `cpc compare <dir> <dir> ...` compares result directories
(simulation or live) side by side, in total and per site, with ratios for two directories, for instance
to check the agreement of simulation and live experiment.

== Result packages <sec-results>

Every run writes a directory described by `datapackage.yaml` (`cpc.kind` is `run_result`):

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("File", "Content"),
    [`run.yaml`], [run description: platform version, creation time, variant, mode, duration, time bin, routing and capacity policies with parameters, the expanded scenario],
    [`summary.json`], [summary metrics (below)],
    [`requests.parquet`], [per request: `arrival_s`, `source`, `site`, `server`, `status` (served / rejected / unrouted / unfinished), `route_reason`, `input_tokens`, `output_tokens`, `queue_s`, `ttft_s`, `e2e_s`, `tpot_s`, `slo_ok`],
    [`timeseries.csv`], [per time bin and site: IT power (kW), servers not off (time average), arrival rate, price, carbon intensity, power limit],
    [`decisions.jsonl`], [capacity decisions per control period: time, instructions per site, notes],
  ),
  caption: [Result package],
)

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("Summary metric", "Definition"),
    [`arrived`, `served`, `rejected`, `unrouted`, `unfinished`], [requests arrived; served; rejected by sites; rejected by the routing policy; still queued or in flight at the end (conservation: the first is the sum of the others)],
    [`slo_attainment`], [share of served requests meeting both the TTFT and the TPOT objective],
    [`ttft_p50_s`, `ttft_p99_s`, `e2e_p50_s`], [quantiles of TTFT and end-to-end latency (with round-trip time)],
    [`it_kwh`, `facility_kwh`], [IT and facility energy],
    [`cost`, `currency`, `carbon_kg`], [cost (currency of the price dataset) and carbon, accounted for time bins with signals only],
    [`signal_coverage`], [share of facility energy with grid signals],
    [`wh_per_request`], [facility energy per served request],
    [`sites`], [energy, cost, carbon and served requests per site],
    [`route_reasons`, `skipped_cycles`], [counts of routing reasons; skipped control periods and why],
    [`workload`, `grid`], [workload dataset and transformation, grid-signal sources (`name@version:signal`)],
  ),
  caption: [Summary metrics],
)

The tables of a result package can be read and plotted directly with pandas, Polars or pyarrow.
Simulation statuses are served, rejected, unrouted (rejected by the routing policy) and unfinished (still
queued or in flight at the end); live statuses are served, rejected and failed.

== Reproducibility

A result is determined by the scenario (with dataset names and versions), the policies and their
parameters, duration and time bin, the random seeds (`workload.seed` for workload scaling and source
assignment; the `seed` parameter of `weighted` routing) and the platform version. `run.yaml` records all of
them; dataset descriptors record the raw-data sources and conversion parameters.
