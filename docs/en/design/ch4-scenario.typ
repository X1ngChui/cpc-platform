#import "../../template/cpc-doc.typ": *

= Scenario and models <ch-scenario>

A scenario file (YAML) describes the system under study, independently of how it runs and is deployed.
Loading rejects unknown fields and checks references; relative paths resolve against the directory of
the scenario file. A scenario has the following parts.

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("Part", "Content"),
    [`clock`], [start of the signal clock (with UTC offset) and its speed-up in live experiments.],
    [`sources[]`], [source regions: identifier, name, latitude and longitude, share of requests `share`.],
    [`node_profiles`], [server types: power and performance model parameters (@sec-server-model).],
    [`sites[]`], [sites: identifier, name, grid region `region`, latitude and longitude, PUE, server pools `pools`, power measurement `measurement`, minimum online servers `min_active`, grid-connection limit `grid_cap_kw`, local policies `scheduler`.],
    [`network`], [round-trip latency model between source regions and sites; whether the gateway injects it in live experiments.],
    [`grid`], [datasets and region maps of price, carbon intensity and extra signals; demand-response events.],
    [`workload`], [workload dataset, extension fields passed to policies, time window and scaling, random seed.],
    [`slo`], [limits of TTFT and TPOT.],
    [`scheduler`], [global routing and capacity policies and their parameters; runtime parameters such as control period, queue limits, retries and state refresh.],
  ),
  caption: [Parts of a scenario],
)

== Sites and server pools

A site consists of one or more server pools, each referring to a server type:

```yaml
sites:
  - id: nm
    name: Inner Mongolia - Horinger
    region: inner_mongolia          # region key of the grid-signal datasets
    lat: 40.38
    lon: 111.82
    pue: 1.20
    pools:
      - {id: h100, profile: h100x8, count: 2}                  # emulated pool
      - {id: b200, profile: b200x4, count: 2}
      - id: lab                                                 # real pool
        profile: local
        backend: openai
        upstreams:
          - {id: lab-0, url: "http://10.0.0.8:8000", gpu_indices: [0],
             container: vllm-0, max_inflight: 32}
    measurement:                     # required with real pools
      sensors: [{type: nvml}]
      host: excluded                 # server / cpu_dram / excluded
    scheduler:                       # local policies (default: least_loaded + ordered)
      dispatch: {policy: efficient_first}
      power: {policy: efficient}
```

An emulated pool (`backend: emulated`) gives the number of servers `count`; their slots are `slots` of the
server type. In a real pool (`backend: openai`) every entry of `upstreams` is a real server: the address
of an OpenAI-compatible inference instance, the accelerators it uses (`gpu_indices` or the device names
`devices` of the sensor layer, one of the two; CPU-only instances give neither, their power is the host
part and `measurement.host` must not be `excluded`), an optional container name (for power on/off), the
concurrency limit `max_inflight` and the path of the health check. The sensors of the site's
`measurement` measure the power of real servers (@ch-energy); the parameters of their server type serve
the capacity and power estimates of global policies in live experiments and emulate the server in simulation.

== Server model <sec-server-model>

A server type describes power and speed with two tables over occupancy $u = "requests in service" \/
"slots"$ in $[0, 1]$; values between points are interpolated linearly and values outside the table take
the end points.

```yaml
node_profiles:
  h100x8:
    description: 8xH100, Llama-3.1-405B-Instruct-FP8
    slots: 128                                   # concurrency slots
    power_w: [[0, 1360], [0.0624, 4060.4], [0.2493, 4544.3],
              [0.4989, 4962.1], [0.9973, 5406.1]]
    tpot_s:  [[0.0624, 0.03406], [0.2493, 0.04039], [0.4989, 0.04689], [0.9973, 0.06527]]
    prefill_tokens_per_s: 5000                   # prefill throughput
    boot_s: 180                                  # time from power-on to service
```

*Power.* `power_w` gives the server IT power $P(u)$; its first point is $u = 0$, the power online without
requests, $P_"idle"$; $P_"max"$ is the largest value. Booting servers draw $P_"idle"$, servers that are off
draw `off_w` (default 0). With a power-cap ratio $c$ the limit is $P_"idle" + c dot (P_"max" - P_"idle")$
and server power is $min(P(u), "limit")$. The power of LLM inference grows concavely with concurrency,
with a clear jump from idle to a few requests @patel2024characterizing; tables use measured points
directly without assuming a functional form.

*Speed.* Prefill time $T_"pre" = L_"in" \/ (R_"pre" dot s(c))$, time per output token
$T_"pot" = T(u) \/ s(c)$ with $T$ the table `tpot_s` and $s(c) = c^beta$ the relative speed under a power
cap ($c$ clamped to $["cap_min_ratio", 1]$, $beta$ = `cap_perf_exponent`). The service time is
$T_"pre" + L_"out" dot T_"pot"$; TTFT is the queueing time plus $T_"pre"$, one $T_"pot"$ and the round-trip
time. $u$ is the occupancy when the request starts (including the request).

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ([Field (`node_profiles.<name>`)], [Meaning]),
    [`description`], [description (optional): hardware, model and data source],
    [`slots`], [concurrency slots (required)],
    [`power_w`], [power table \[\[occupancy, server IT power W\], ...\] (required); the first occupancy is 0, occupancies strictly increase],
    [`tpot_s`], [time-per-output-token table \[\[occupancy, s\], ...\] (required), occupancies strictly increase],
    [`prefill_tokens_per_s`], [prefill throughput $R_"pre"$ (required)],
    [`boot_s`], [time from power-on to service (required)],
    [`off_w`], [power while off (default 0). Switching a real server off usually stops the inference instance while the machine keeps running; measure it],
    [`cap_min_ratio`, `cap_perf_exponent`], [lowest power-cap ratio (default 0.5; for real GPUs the ratio of the device's minimum and maximum power limits, e.g. 100/165 for the A30); exponent $beta$ of the speed over the cap ratio (default 0.4; measure it)],
  ),
  caption: [Fields of server types],
)

=== Generation from measurements <sec-calibration>

`cpc profile` generates a server type from the measurements of one (device, device count, model) in a
power-profile dataset: each measurement, at occupancy $u = "concurrency" \/ "slots"$, becomes a point of
`power_w` (the measured power plus `--host-w`) and of `tpot_s`; the first point of `power_w` is the online
idle power `--idle-w`. Measurements cover the serving periods only: online idle power, host power,
prefill throughput and boot time are given separately, and required items that are missing are noted in
the output. When `cpc measure` measures a real server it also records the slots, the online idle power,
the boot time and the power while off, and `cpc profile` uses these values directly. Tables may also be
computed from any formula at a few points.

`h100x8` of the default scenario comes from six measurements of 8xH100 running
Llama-3.1-405B-Instruct-FP8 in ML.ENERGY Benchmark v3, `b200x4` from 4xB200 running the same model; their
online idle and host power are illustrative.

== Network latency

The round-trip time from a source region to a site is $"RTT" = "base_ms" + 2 dot d dot "route_factor" \/
"km_per_ms"$ with $d$ the great-circle distance, `route_factor` the detour factor of routes and
`km_per_ms` the propagation speed in fibre (about 200 km/ms). In live experiments with `emulate_wan` the
gateway injects half the RTT before forwarding and half after the response; set it to false when the
services really are in different regions. `cpc check` prints the RTT matrix.

== Grid signals and demand response

```yaml
grid:
  price:  {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: price}
  carbon: {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: carbon_intensity}
  extras:                                   # extra signals, passed unchanged to policies (GridSignal.extra)
    - {name: renewable_share, dataset: "${CPC_DATA_DIR}/my-grid", signal: renewable_share}
  dr_events:
    - {site: gd, cap_kw: 8, start: "2026-07-15T15:00:00+08:00",
       end: "2026-07-15T17:00:00+08:00"}
```

Every signal source gives a dataset, a signal and an optional site-to-region map `regions` (default: the
site's `region`). The grid-signal model (`cpc.grid.signals.GridModel`) gives the `GridSignal` of a site at
signal time $t$: price and its period label, carbon intensity, currency, power limit (the smaller of the
grid-connection limit and active demand-response limits), PUE, carbon price and extra signals. The
effective price is price + carbon price × carbon intensity (per unit of facility energy); the carbon
price `scheduler.carbon_price_per_kg` is in the currency of the price. When price or carbon intensity is
unavailable, the site has no signal; an unavailable extra signal is `None`.

A demand-response event means "the facility power of a site must not exceed `cap_kw` during a period";
events can be listed in the scenario or injected while an experiment runs.

*Signal clock.* In simulation, signal time = `clock.start` + simulated time. In live experiments, signal
time = `clock.start` + elapsed real time × `clock.speedup`; the grid-signal service is the authority of the
clock, and the other services synchronise from `/v1/clock` at startup. The speed-up only changes how fast
signals change: service times, boot times and energy integration remain in real time.

== Workload and transformation

```yaml
workload:
  dataset: "${CPC_DATA_DIR}/azure-llm-2024-conv"
  attributes: [session_id]   # extension fields passed to policies (declared in the dataset)
  start_s: 230400            # window start (original trace seconds)
  duration_s: 86400          # window length
  time_scale: 1.0            # time compression: t' = (t - start_s) / time_scale
  rate_scale: 1.0            # arrival-rate factor
  seed: 1                    # random seed of scaling and source assignment
```

A window outside the data is an error. With `rate_scale` ≤ 1 every request is kept independently with
that probability; above 1, ⌈`rate_scale`⌉ thinned copies are superimposed, each keeping a request with
probability `rate_scale` / ⌈`rate_scale`⌉. Requests are assigned to source regions at random by
`sources[].share`; the simulator and the replay of `cpc live` use the same seed, so both see the same
request sequence and source assignment.
