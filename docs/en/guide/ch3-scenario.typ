#import "../../template/cpc-doc.typ": *

= Writing scenarios <g-scenario>

A scenario file (YAML) describes everything a run involves: where users come from, which data centres and
servers exist, which datasets provide grid signals and workload, the service objectives, and the
scheduling policies. The same scenario serves simulation and live experiments. Scenarios are usually
copied from `examples/quickstart/scenario.yaml` or `configs/scenario.yaml` and adapted, checking every
change with `cpc check -s <scenario>`.

Relative paths resolve against the directory of the scenario file; paths may use environment variables
such as `${CPC_DATA_DIR}`. A misspelt field fails loading with the field named, instead of being ignored.

== Structure

```yaml
name: my-scenario          # scenario name
clock: {...}               # signal clock: where it starts
sources: [...]             # source regions of users
node_profiles: {...}       # server types
sites: [...]               # data-centre sites
network: {...}             # network latency (optional)
grid: {...}                # grid signals and demand response
workload: {...}            # workload
slo: {...}                 # service objectives (optional)
scheduler: {...}           # global policies and runtime parameters (optional)
```

== clock: signal clock

```yaml
clock:
  start: "2026-07-15T11:00:00+08:00"   # start, with UTC offset
  speedup: 1                           # speed-up in live experiments (no effect in simulation)
```

`start` decides the date and time from which grid signals are taken (summer or not, sharp-peak hours or
not, ...) and must lie within the grid data. In simulation second $t$ is signal time `start` + $t$. In live
experiments signal time = `start` + elapsed real time × `speedup`; `speedup: 60` makes one real minute one
signal hour, to see price changes in a short time.

== sources: source regions

```yaml
sources:
  - {id: east,  name: Shanghai,  lat: 31.23, lon: 121.47, share: 0.40}
  - {id: south, name: Guangzhou, lat: 23.13, lon: 113.26, share: 0.35}
```

`lat` and `lon` give the distance and round-trip time to each site; `share` is the share of requests. Each
request of the workload is assigned to a source region at random in proportion to the shares (they need
not sum to 1).

== node_profiles: server types

```yaml
node_profiles:
  h100x8:
    description: 8xH100, Llama-3.1-405B-Instruct-FP8
    slots: 128
    power_w: [[0, 1360], [0.0624, 4060.4], [0.2493, 4544.3],
              [0.4989, 4962.1], [0.9973, 5406.1]]
    tpot_s: [[0.0624, 0.03406], [0.2493, 0.04039], [0.4989, 0.04689], [0.9973, 0.06527]]
    prefill_tokens_per_s: 5000
    boot_s: 180
```

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("Field", "Meaning"),
    [`slots`], [concurrency slots: requests a server processes at the same time at most (the largest batch of the inference engine, e.g. `max_num_seqs` of vLLM)],
    [`power_w`], [power table: list of \[occupancy, server IT power (W)\]. The first occupancy is 0, the power when on without requests; occupancies increase],
    [`tpot_s`], [inter-token latency table: \[occupancy, s\]. The more requests in a batch, the longer each needs per token],
    [`prefill_tokens_per_s`], [prefill throughput: speed of processing input tokens, deciding the computation before the first token],
    [`boot_s`], [time from power-on until serving (model loading included)],
    [`off_w`], [power while off, default 0. Switching a real server "off" usually stops only the inference instance while the machine keeps running; then enter the measured value (`cpc measure --boot`)],
    [`cap_min_ratio`, `cap_perf_exponent`], [power capping: lowest cap ratio (default 0.5); with cap ratio c the speed becomes c to this exponent (default 0.4)],
    [`description`], [description (optional)],
  ),
  caption: [Fields of server types],
)

@g-profile shows how to generate the tables. With power-cap ratio c, server power stays below
"idle + c × (full load − idle)".

== sites: data-centre sites

```yaml
sites:
  - id: nm                         # site identifier (command line, metrics, results)
    name: Inner Mongolia - Horinger
    region: inner_mongolia         # grid region: region name in the grid-signal datasets
    lat: 40.38
    lon: 111.82
    pue: 1.20                      # facility power = IT power × PUE
    pools:                         # server pools
      - {id: h100, profile: h100x8, count: 2}
      - {id: b200, profile: b200x4, count: 2}
    min_active: 1                  # servers the capacity policy keeps online at least (default 1)
    grid_cap_kw: 30                # grid-connection limit (kW, optional)
    scheduler:                     # local policies (optional)
      dispatch: {policy: efficient_first}
      power: {policy: efficient}
```

*Server pools.* Each pool refers to a server type: emulated pools give the number `count`; real pools
(`backend: openai`) list their inference instances (@g-real). A site may have several pools.

*Local policies.* `dispatch` chooses a server for requests reaching the site, default `least_loaded`
(lowest occupancy); `power` decides which servers to switch, default `ordered` (configuration order). Sites
with several server types can prefer servers with low power per slot through `efficient_first` and
`efficient`. @g-policy lists all policies.

== network: network latency

```yaml
network:
  base_ms: 4                # fixed latency
  route_factor: 1.5         # detour factor of routes
  km_per_ms: 200            # propagation speed in fibre (km/ms)
  emulate_wan: true         # the gateway injects the latency in live experiments
  default_source: east      # region of requests without a source (live experiments)
```

Round-trip time = `base_ms` + 2 × distance × `route_factor` / `km_per_ms`. The whole `network` part may be
omitted, giving the defaults above (with no `default_source`). `cpc check` prints the round-trip times from
every source region to every site.

== grid: grid signals and demand response

```yaml
grid:
  price:  {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: price}
  carbon: {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: carbon_intensity}
  extras:                           # extra signals (optional), read by policies from signal.extra
    - {name: renewable, dataset: "${CPC_DATA_DIR}/my-grid", signal: renewable_share}
  dr_events:                        # demand-response events (optional)
    - {site: gd, cap_kw: 5, start: "2026-07-15T11:20:00+08:00",
       end: "2026-07-15T11:50:00+08:00"}
```

`price` and `carbon` each name a dataset and a signal in it, and the `region` of every site must be covered
by the dataset. When the region names of sites and dataset differ, map them with `regions`:
`price: {dataset: ..., signal: price, regions: {sh: east}}`. A demand-response event keeps the facility
power of a site below `cap_kw` during a period; `cpc event` injects events while an experiment runs. The
carbon price (`scheduler.carbon_price_per_kg`) uses the currency of the price.

== workload

```yaml
workload:
  dataset: "${CPC_DATA_DIR}/azure-llm-2024-conv"
  start_s: 230400        # start at second 230400 of the raw trace (default 0)
  duration_s: 86400      # take 86400 s (default: to the end)
  time_scale: 1          # time compression: 2 halves the trace time
  rate_scale: 1          # arrival-rate factor: 0.5 keeps half the requests at random, 2 doubles them
  attributes: []         # extension fields passed to policies
  seed: 1                # random seed (rate scaling and source assignment)
```

Second 0 of the workload is `clock.start`. A window outside the data fails loading with the covered range.
The options `--start-s`, `--duration-s`, `--time-scale` and `--rate-scale` of `cpc sim`, `cpc live` and
others override these values temporarily.

== slo and scheduler

```yaml
slo:
  ttft_s: 2.0                     # limit of the time to first token (s)
  tpot_s: 0.15                    # limit of the inter-token latency (s)

scheduler:
  routing:  {policy: nearest, params: {max_wait_s: 0.5}}     # global routing policy
  capacity: {policy: reactive, params: {target_util: 0.6}}   # global capacity policy
  carbon_price_per_kg: 0.1        # carbon price (currency of the price per kgCO2)
  sim_control_interval_s: 300     # decision period of the capacity policy in simulation (s)
  control_interval_s: 15          # decision period of the capacity policy in live experiments (s)
  # site queue limit = max(online slots, slots of one server) × this factor
  queue_limit_per_slot: 4
```

The other runtime parameters of `scheduler` (retries, state refresh, idle timeout of streams
`stream_idle_timeout_s`, ...) rarely need changes; the design document explains them in the chapters
"Scenario and models" and "Service interfaces".

== Common errors

#figure(
  tbl(
    columns: (14em, 1fr),
    header: ("Error message (excerpt)", "Cause and remedy"),
    [`unknown field`], [misspelt field or wrong level; the message gives the field path and the closest correct field (`did you mean ...`)],
    [`required field is missing`], [the field has no default and must be given; appears together with the previous error for misspelt names],
    [`unknown routing policy` (or capacity, dispatch, power policy)], [misspelt policy or policy package not installed; the message lists all policies],
    [`does not accept`], [misspelt policy parameter; the message lists all parameters of the policy],
    [`dataset ... does not exist`, `is missing`], [the dataset is not prepared or the path is wrong; check that `CPC_DATA_DIR` is the one used for preparation],
    [`does not cover the site regions`], [the `region` of a site does not exist in the grid-signal dataset; correct `region` or map it with `regions`],
    [`does not include the clock start`], [`clock.start` is outside the grid data; convert a longer period or move the start],
    [`window ... is outside`], [`workload.start_s` and `duration_s` exceed the workload data],
    [`less than the simulated`], [`cpc sim --hours` exceeds the transformed workload; shorten the run or widen the window],
    [`the first point must be at occupancy 0`], [the power table must start at idle],
  ),
  caption: [Common scenario errors],
)
