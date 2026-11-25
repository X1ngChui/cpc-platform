#import "../../template/cpc-doc.typ": *

= Architecture <ch-arch>

== Layers

The platform has five layers (@fig-arch):

- *Data*: datasets declare their structure and provenance in descriptors; the platform validates them
  against their kind specification and loads them as columnar tables (@ch-data);
- *Scenario*: one YAML file describes the system under study: source regions, sites and server pools,
  server types, network latency, grid signals and demand response, the workload and its transformation,
  objectives and policies (@ch-scenario);
- *Shared parts*: server model, data-centre runtime (@ch-runtime), global and local policies
  (@ch-policy), grid-signal model and accounting. They have no clock of their own, and simulation and live
  experiments call the same implementation;
- *Operation*: simulations are driven by the discrete-event simulator; live experiments by service
  processes (grid-signal service, gateway, controller, site agents) on real time, with `cpc live`
  replaying the workload and sampling;
- *Results*: both modes write result packages of the same format; live experiments also produce
  Prometheus metrics, request logs and decision logs (@ch-workflow).

#figure(image("../../figures/en/architecture.pdf", width: 100%), caption: [Layers of the platform]) <fig-arch>

== Shared and mode-specific parts

#figure(
  tbl(
    columns: (13em, 1fr, 1fr),
    header: ("Part", "Simulation", "Live experiment"),
    [Scenario, datasets, server models], [shared], [shared],
    [Global policies (routing, capacity) and local policies (dispatch, power management)], [shared (same interfaces, same implementations)], [shared],
    [Data-centre runtime (server states, queueing and dispatch, capacity instructions, energy metering)], [shared (`cpc.datacenter`)], [shared],
    [Grid-signal model, metric definitions and accounting rules], [shared], [shared],
    [Clock and events], [simulated clock, event queue], [real time (`asyncio`); the signal clock may be accelerated],
    [Request execution], [prefill and decode times from the performance model], [emulated servers wait as the model says; real servers forward to inference instances],
    [Power], [server model], [server model for emulated servers; sensors for real servers],
    [Actions], [state changes directly; boot completion is a timed event], [executor hooks (containers on/off, power limits) and health checks],
    [Communication], [in-process calls], [HTTP: OpenAI-compatible inference, state and admin interfaces],
    [Results], [result package (`cpc sim`, `cpc run`)], [result package (`cpc live`); plus Prometheus metrics, request and decision logs],
  ),
  caption: [Shared and mode-specific parts],
) <tbl-shared>

In live experiments, real and emulated servers can coexist in one site and one scenario; in simulation a
real pool is emulated with the model of its server type, one server per inference instance. The same
scenario therefore serves both for comparing policies in simulation and for experiments with real servers.

== Two-level scheduling

Scheduling has a global and a local level (@fig-two-level). Their contract consists of state summaries
and instructions:

- *Global scheduling (across data centres)*: the routing policy chooses a site for every request; the
  capacity policy periodically sets an instruction per site (servers to keep online, power-cap ratio) and
  may set routing weights from source regions to sites. Global policies read only the state summaries of
  the sites (online, booting and draining servers and slots, requests in service and queued, arrival rate
  and mean service time, IT and facility power, mean server-model parameters and pool summaries) and the
  grid signals;
- *Local scheduling (inside a data centre)*: the dispatch policy chooses a server for every request
  reaching the site (filter the servers that can accept it → score → select) and orders the queue; the
  power policy turns the number of online servers into concrete servers to switch. Local policies may use
  request extension attributes (session identifier, priority, prefix-block hashes, ...) and differences
  between pools (such as power per slot).

Studies of scheduling across data centres may keep the default local policies and treat each data centre
as a whole; studies inside a data centre may fix the global policies. The split follows cloud simulators
that separate allocation inside a data centre from user-facing brokers @calheiros2011cloudsim, global
schedulers of LLM serving systems that decide on load summaries of instances @sun2024llumnix, and the
filter and score extension points of the Kubernetes scheduling framework @k8s-scheduling-framework.

#figure(image("../../figures/en/two-level.pdf", width: 100%), caption: [Two-level scheduling: global and local policies]) <fig-two-level>

== Data paths

*Requests.* The workload replay of `cpc live` (or a user) sends OpenAI-compatible requests to the gateway;
the header `x-cpc-source` names the source region and `x-cpc-attr-<field>` headers carry extension
attributes. The gateway builds the cluster view (site summaries, grid signals, RTT from sources to sites,
routing weights), asks the routing policy for a site, waits half the RTT and forwards the request. The
site agent submits it to the data-centre runtime, the dispatch policy chooses a server, and the server
executes it (emulated servers by the model, real servers through their inference instance). The
response returns through the gateway after the other half of the RTT.

*State.* The gateway polls `/state` of every site every `scheduler.state_refresh_s`. Between two polls it
counts the requests it has sent that the summaries do not show yet, so that a burst does not flood one site.

*Control.* In every control period the controller collects the site summaries, the grid signals and the
gateway's demand statistics (arrival rate per source region), calls the capacity policy when its inputs
are complete, sends instructions to the sites and may send routing weights to the gateway.

*Observation.* Prometheus scrapes `/metrics` of every service; recording rules aggregate the power
metrics into per-site energy, cost and carbon rates (@ch-energy).

The simulator implements the same paths in process: routing builds a cluster view of the same structure,
control periods call the same capacity policy, and requests reaching a site are submitted to the same
data-centre runtime.

== Failure handling and fault isolation <sec-failure>

The platform distinguishes two classes of failures:

- *configuration and environment errors* are checked at startup; any failed check reports the reason and
  exits with a non-zero status (status 2 for the command line);
- *local faults at run time* (sensor reading failed, site unreachable, grid signal missing, ...) are
  confined to the data or component they affect, reported as explicit state, never take down the service
  as a whole and are never replaced by another source, a model estimate or a default.

There are no fallback chains that try several sources in turn: datasets, power sources, sensor
interfaces, service addresses and policy inputs are all set explicitly by configuration. When hardware
offers several interfaces, one is probed at startup and used throughout; the result appears in the state.

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("Component", "Startup checks (the process exits on failure)"),
    [Configuration], [unknown fields in scenario and endpoints files; references to unknown sites, source regions or server types; pools inconsistent with their backend (emulated pools need a count, real pools inference instances and accelerators); sites with real servers but no power measurement, or a measurement without real servers.],
    [Datasets], [descriptor structure and kind specification (core fields, types, units); data files present; data satisfy the field constraints (non-empty, ranges, enums, uniqueness); workloads ordered by arrival; grid-signal periods non-overlapping with declared units; the workload window within the data.],
    [Grid-signal service], [units of the price and carbon signals; every site region covered; data covering the start of the scenario clock.],
    [Site agent], [every power sensor constructed and read successfully; every declared accelerator has a reading and device names are unique; the readings needed by `measurement.host` exist; with container names, the Docker Engine is reachable and the containers exist; with power capping, the sensors of the accelerators support it.],
    [Gateway, controller, `cpc live`], [the signal clock is synchronised from the grid-signal service in time (30 s by default for gateway and controller); `cpc live` also waits until all services are ready.],
    [Service addresses], [except for `cpc up`, commands that reach other services need an endpoints file (`-e` or `CPC_ENDPOINTS`) or the explicit local addresses (`--local`).],
  ),
  caption: [Startup checks],
)

#figure(
  tbl(
    columns: (9em, 1fr, 1fr),
    header: ("Fault at run time", "Affected", "Explicit state"),
    [sensor reading failed or field missing], [the readings of that sensor are missing; server power depending on them is `None` with the missing parts in `power_missing`; other sensors and request handling are unaffected], [`components.sensors` in `/state`; `cpc_sensor_up`, `cpc_sensor_errors_total`; power metrics NaN],
    [site state failed or stale], [the gateway removes the site from the routing view; the controller excludes it from the period], [`upstreams` state; `cpc_upstream_up`; `excluded` in the decision record],
    [forwarding failed], [retryable failures (queue full, connection failed) exclude the site and decide again as `scheduler.retry` says; failures after delivery are not retried], [`cpc_gateway_attempts_total{site,outcome}`; `attempts` in the request log],
    [grid signal missing], [`signal` of the site view is `None`; signal-dependent routing overflows with the reason; capacity policies requiring signals skip the period; simulation accounts cost and carbon only for bins with signals], [`cpc_gateway_route_decisions_total{reason}`; `cpc_controller_cycles_total{result="skipped"}`; `signal_coverage` in the summary],
    [power or power-cap action failed], [the server keeps its state; a server failing to boot returns to `off`], [`last_error` of the server; `cpc_node_actuation_errors_total`],
    [intervals with missing power], [not counted in energy, counted as missing time], [`energy_coverage`; `cpc_node_energy_missing_seconds_total`],
  ),
  caption: [Isolation and reporting of faults at run time],
)

Every service provides `/healthz` (liveness: process and event loop respond) and `/readyz` (readiness:
startup checks passed and the service can handle requests). Logs can be structured
(`CPC_LOG_FORMAT=json`); state changes (e.g. `upstream_down`, `sensor_up`) and skipped control periods
are written as records with an `event` field.

== Deployment

The service processes of a live experiment can run on one host or be distributed (@fig-deploy). An
endpoints file gives the service addresses; scenarios do not depend on the deployment.

#figure(image("../../figures/en/deployment.pdf", width: 100%), caption: [Deployment of a live experiment]) <fig-deploy>

#figure(
  tbl(
    columns: (9em, 1fr, 1fr),
    header: ("Deployment", "Use", "Entry"),
    [one process], [development and demonstrations], [`cpc up`],
    [one process per service], [checking process isolation and fault handling], [`cpc grid`, `cpc site`, `cpc gateway`, `cpc controller`],
    [cloud servers], [real servers: one control stack (with Prometheus, Grafana and emulated sites) and one site stack per GPU or CPU server (Docker Compose)], [`deploy/cloud/`],
  ),
  caption: [Deployments],
)

The services target Linux (x86_64, aarch64) only and are published as one multi-architecture container image.

== Code structure

```text
src/cpc/
├── datasets/     descriptors, kind specifications (profiles), table reading
│                 and validation (table), loading of workloads, grid signals and
│                 power profiles (workload, grid, power), writing (build)
├── config.py     scenario and endpoint configuration
├── models.py     server power and performance model, WAN latency model, signal clock
├── datacenter.py data-centre runtime (shared by simulation and live experiments)
├── sched/        global policies (routing, capacity) and local policies (local)
├── registry.py   plugin registry (entry points)
├── schemas.py    state summaries, instructions and demand statistics
├── sim/          discrete-event simulator
├── results.py    result packages (RunResult) and experiment descriptions
├── live.py       live experiments: workload replay, sampling, result packages
├── measure.py    power-profile measurement of real servers (cpc measure)
├── launch.py     all services in one process (cpc up, cpc live)
├── grid/ site/ gateway/ controller/   services
├── sensors/      power sensors and energy integration
├── health.py     health of dependencies and its metrics
└── cli.py        command line cpc
examples/datasets/    conversion examples for public raw data
examples/experiments/ experiment descriptions
configs/              scenarios
deploy/               cloud deployment (Docker Compose) and monitoring configuration
```
