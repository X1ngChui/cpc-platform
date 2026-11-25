#import "../../template/cpc-doc.typ": *

= Simulation <g-sim>

A simulation advances time event by event in one process: requests arrive, reach a site after the network
delay, queue, are processed on a server and complete; the capacity policy switches servers every decision
period. Simulation and live experiments share the server models, the rules inside data centres and the
scheduling policies, so policy differences found in simulation can be reproduced live.

== One simulation: cpc sim

```bash
cpc sim -s configs/scenario.yaml --hours 24 --out results/base
```

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("Option", "Meaning"),
    [`-s`, `--scenario`], [scenario file, default `configs/scenario.yaml`],
    [`--hours`], [simulated hours (default 24), at most the length of the workload],
    [`--routing`, `--routing-params`], [routing policy and parameters instead of the scenario's (parameters as JSON, e.g. `'{"max_wait_s": 1}'`); `--routing-params` alone updates those parameters of the scenario's routing policy],
    [`--capacity`, `--capacity-params`], [capacity policy and parameters, same rules],
    [`--start-s`, `--duration-s`, `--time-scale`, `--rate-scale`], [workload window, time compression and rate scaling instead of the scenario's],
    [`--bin-s`], [duration of a time-series row (s, default 300)],
    [`--out`], [result directory, default `results/sim-<routing>__<capacity>`],
  ),
  caption: [Options of cpc sim],
)

For example, 1.5 times the arrival rate with cost-aware routing:

```bash
cpc sim -s configs/scenario.yaml --routing cost_aware --rate-scale 1.5 \
    --out results/cost-1.5x
```

== Comparing policies: cpc run

List the variants to compare in an experiment file:

```yaml
name: policies                         # experiment name; default output results/<name>
scenario: ../../configs/scenario.yaml  # scenario, relative to this file
hours: 24                              # simulated hours
bin_s: 300                             # duration of a time-series row (s, default 300)
variants:
  - name: nearest-static               # variant name, also the result subdirectory
    routing:  {policy: nearest, params: {max_wait_s: 0.5}}
    capacity: {policy: static}
  - name: weighted-gridaware
    routing:  {policy: weighted}
    capacity: {policy: grid_aware}
  - name: cost-heavy-load
    routing:  {policy: cost_aware}
    workload: {rate_scale: 1.5}        # overrides fields of the scenario workload (dataset included)
```

Variants without `routing` or `capacity` take the scenario's. Run:

```bash
cpc run examples/experiments/policies.yaml --jobs 4
```

Variants run in parallel processes (`--jobs` limits how many at once; each process loads the workload, so
reduce it for very large workloads); a comparison is printed and written to
`results/<experiment>/comparison.csv`.

== Result directories

Every run writes a result directory with these files (`cpc live` writes the same format).

*summary.json*: summary metrics.

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("Field", "Meaning"),
    [`arrived`], [requests arrived],
    [`served`, `rejected`, `unrouted`, `unfinished`], [requests served; rejected by a site (queue full); rejected by the routing policy; not finished when the simulation ends (live experiments also have `failed`: connection failures, incomplete streams and other errors)],
    [`slo_attainment`], [share of served requests meeting both the TTFT and the inter-token objective],
    [`ttft_p50_s`, `ttft_p99_s`, `e2e_p50_s`], [median and 99th percentile of TTFT, median end-to-end latency (s, network round trip included)],
    [`it_kwh`, `facility_kwh`], [IT and facility energy (kWh)],
    [`cost`, `currency`, `carbon_kg`], [cost (currency in `currency`) and carbon (kg)],
    [`wh_per_request`], [facility energy per served request (Wh)],
    [`signal_coverage`], [share of facility energy with grid signals; below 1 the part without signals is not in cost and carbon],
    [`sites`], [energy, cost, carbon and served requests per site],
    [`route_reasons`], [counts of routing reasons, e.g. `nearest`, `overflow_wait_exceeded` (the nearest site queued too long and the request went elsewhere)],
    [`skipped_cycles`], [capacity decision periods skipped for incomplete inputs (missing grid signals, ...)],
    [`workload`, `grid`], [workload used (name \@ version, window, scaling) and grid-signal datasets],
  ),
  caption: [Summary metrics],
)

*requests.parquet*: one row per request with `arrival_s` (arrival, s), `source`, `site`, `server`,
`status`, `route_reason`, `input_tokens`, `output_tokens`, `queue_s` (queueing in the site), `ttft_s`,
`e2e_s`, `tpot_s`, `slo_ok`.

*timeseries.csv*: one row per time bin. `t_h` is its start (hours) and `time` the signal time; for every site
`<site>` there are `<site>_it_kw` (mean IT power), `<site>_nodes` (servers on), `<site>_rps` (arrival rate),
`<site>_price`, `<site>_carbon` and `<site>_cap_kw` (power limit, empty without one).

*decisions.jsonl*: one line per period of the capacity policy: time, instruction per site (`active_nodes`:
servers on, `cap_ratio`: power-cap ratio) and notes.

*run.yaml*: mode, policies and parameters, duration, platform version and the expanded scenario.

== Analysing results in Python

```python
import cpc

run = cpc.read_run("results/policies/weighted-gridaware")
s = run["summary"]
print(s["facility_kwh"], s["cost"], s["currency"], s["slo_attainment"])

req = run["requests"].to_pandas()        # pyarrow table to pandas (pandas needed)
served = req[req.status == "served"]
print(served.groupby("site").ttft_s.quantile(0.99))      # TTFT P99 per site
print(served.groupby("source").slo_ok.mean())            # SLO attainment per source region

ts = run["timeseries"]                   # one dict per time bin; numbers are floats, empty cells None
print(max(row["gz_it_kw"] or 0 for row in ts))           # largest IT power of site gz (kW)
```

Simulations can also run from Python, e.g. to sweep parameters:

```python
import cpc

for scale in (0.5, 1.0, 1.5):
    res = cpc.simulate("configs/scenario.yaml", routing="cost_aware", hours=6,
                       rate_scale=scale)
    print(scale, res.summary["facility_kwh"], res.summary["slo_attainment"])
```

Keyword arguments of `cpc.simulate`: `routing`, `routing_params`, `capacity`, `capacity_params` (as on the
command line), `hours`, `bin_s`, `out` (also writes the result directory); all others override fields of
the scenario `workload`.

== Plotting

The tables of a result package plot directly with common tools. The example below draws IT power and
servers on per site over time and compares the cost of the variants (pandas and matplotlib needed:
`pip install pandas matplotlib`):

```python
import matplotlib.pyplot as plt
import pandas as pd

ts = pd.read_csv("results/policies/cost-reactive/timeseries.csv")
fig, (a, b) = plt.subplots(2, 1, sharex=True, figsize=(8, 6))
ts.plot(x="t_h", y=[c for c in ts.columns if c.endswith("_it_kw")], ax=a, ylabel="IT kW")
ts.plot(x="t_h", y=[c for c in ts.columns if c.endswith("_nodes")], ax=b, ylabel="servers")
fig.savefig("run.png")

cmp = pd.read_csv("results/policies/comparison.csv")
cmp.plot.bar(x="variant", y="cost").figure.savefig("cost.png")
```

== Comparing two result directories: cpc compare

`cpc compare` shows two or more result directories (simulation or live) side by side, in total and per
site: served and failed requests, SLO attainment, TTFT, IT energy, facility energy, cost and carbon; with
two directories the last column is the ratio of the second to the first.

```bash
cpc compare results/quickstart/nearest-reactive results/quickstart/cost-reactive
```

It compares two sets of policies, or the live experiment and the simulation of one scenario (@g-calibrate).

== Reproducibility

The same scenario (dataset names and versions included), policies and parameters, duration and random seed
give the same result. `workload.seed` decides rate scaling and source assignment; changing it evaluates the
effect of randomness. `run.yaml` records everything needed to reproduce a run.
