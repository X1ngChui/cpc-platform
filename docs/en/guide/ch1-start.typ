#import "../../template/cpc-doc.typ": *

= Overview <g-overview>

== What the platform is for

The platform studies how AI inference scheduling across data centres can respond to grid signals. A
typical question: data centres in several regions serve large-model inference to users; electricity
prices and the carbon intensity of electricity differ between regions and change over time, and
sometimes the grid asks a data centre to limit its consumption for a while. Which data centre should
each request go to, and how many servers should each data centre keep on, to reduce cost and emissions
while meeting response-time objectives?

The platform splits such questions into replaceable parts: data (request workloads, prices and carbon
intensities, power characteristics of servers), a description of the system under study (the scenario),
scheduling policies, and two ways of running them (simulation and live experiments). Researchers usually
prepare data, write a scenario, write or choose policies and compare the results.

== Basic concepts

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("Concept", "Meaning"),
    [site], [a data centre at a location, connected to a grid region, with its own PUE (ratio of total facility power to IT power).],
    [server], [the unit of a site that processes inference requests. The power and speed of an emulated server come from the tables of its server type; a real server is a running inference instance (such as vLLM) whose power sensors measure.],
    [server type], [power and speed characteristics of a server: tables of power and inter-token latency over occupancy, plus slots, prefill throughput and boot time.],
    [server pool], [servers of one type in a site. A site may have several pools (for example two server generations).],
    [slots, occupancy], [the number of requests a server can process at the same time is its slots; occupancy = requests in service / slots.],
    [source region], [where user requests come from. Requests travel from their source region to a site with a network round-trip time (RTT).],
    [workload], [a sequence of requests with arrival time, input tokens and output tokens, usually from published service logs (traces).],
    [grid signals], [prices, carbon intensity and the like per grid region over time; a demand-response event requires a site to keep its facility power below a limit for a while.],
    [global policies], [decide across data centres: the routing policy chooses a site for every request; the capacity policy periodically decides how many servers each site keeps on.],
    [local policies], [decide inside a data centre: the dispatch policy chooses a server for requests reaching the site; the power policy decides which servers to switch.],
    [simulation], [advances time event by event in one process; hours to a day of operation take seconds to minutes, for comparing policies.],
    [live experiment], [services run on real time and handle requests through real network interfaces; real inference instances and power sensors can be connected.],
    [result package], [the result directory of a run (simulation or live): per-request records, time series, summary metrics, capacity decisions and the run description.],
  ),
  caption: [Basic concepts],
)

== Workflow

1. *Prepare data*: bring raw data into the platform format (@g-data) or use the conversion scripts;
2. *Write a scenario*: describe sites, servers, grid signals, workload and policies in a scenario file and
   check it with `cpc check` (@g-scenario);
3. *Compare in simulation*: run `cpc sim` or `cpc run` and compare cost, carbon and service quality of
   policies (@g-sim);
4. *Validate live*: run the same scenario on real time with `cpc live`, optionally with real servers (@g-live);
5. *Write policies*: turn your scheduling algorithm into a policy and use it in simulation and live (@g-policy).

= Installation <g-setup>

== Requirements

The services run on Linux (x86_64 or aarch64). On Windows, use Ubuntu in WSL2 and keep the repository in
the Linux file system (e.g. `~/cpc-platform`), not under `/mnt/c` or other Windows drives, where file access
is slow and permissions misbehave. Python 3.10 or later (`python3 --version`) and git are required.

== Getting the code

```bash
git clone https://github.com/X1ngChui/cpc-platform.git
cd cpc-platform
```

All commands below run in the repository root `cpc-platform/`.

== Python environment and installation

The platform is installed into its own Python virtual environment, separate from other Python packages of
the system. The system Python of Debian / Ubuntu lacks the venv module, so install `python3-venv` first:

```bash
sudo apt install python3-venv  # Debian / Ubuntu; usually included elsewhere
python3 -m venv .venv          # create the virtual environment .venv in the repository
. .venv/bin/activate           # activate it; the prompt starts with (.venv)
pip install -e ".[dev]"        # install the platform and the test dependencies
```

In every new terminal, change into the repository and run `. .venv/bin/activate` first. The `cpc` command
is then available:

```bash
cpc --help                     # list all commands
cpc sim --help                 # options of one command
```

Optional dependencies: `pip install -e ".[nvml]"` (NVIDIA GPU power), `pip install -e ".[docker]"`
(starting and stopping inference containers through Docker), `pip install -e ".[docs]"` (building the
documentation). uv works as well (`uv venv --python 3.12 && uv pip install -e ".[dev]"`).

== Checking the installation

```bash
pytest                                    # unit tests; the last line reports all passed
python scripts/smoke_test.py --offline    # starts all services and checks them, about 1 minute
```

The smoke test starts every service as a separate process and checks readiness, request handling,
capacity decisions, energy metrics, demand response, and the handling of sensor and site faults. Every
check prints "[ok]" and the last line is "Smoke test passed". It uses test data shipped with the code,
so nothing is downloaded.

= Quick start <g-quick>

This chapter runs the whole workflow on a small scenario: prepare data, check the scenario, simulate,
compare policies, inspect results, run a live experiment. The scenario `examples/quickstart/scenario.yaml`
has 4 data-centre sites (Shanghai, Guangdong, Guizhou, Inner Mongolia) and 3 source regions; the workload
is about one hour of conversation requests from the Azure LLM Inference Trace 2023.

== Preparing data

```bash
python examples/datasets/azure_llm.py --variant 2023-conv --zero-output fail
python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01
```

The first command downloads a raw workload file of about 0.7 MB and converts it to the platform format;
the second expands the provincial time-of-use tariff rules and emission factors shipped with the code
into grid signals for 2026. Datasets are written to `~/.cache/cpc/datasets/` (another directory can be set
with the environment variable `CPC_DATA_DIR`); each dataset is a directory, e.g.:

```text
~/.cache/cpc/datasets/azure-llm-2023-conv/
├── datapackage.yaml      descriptor: name, version, sources, licence, fields
└── requests.parquet      data: one request per row
```

== Checking the scenario

```bash
cpc check -s examples/quickstart/scenario.yaml
```

`cpc check` reads the scenario and every dataset it refers to, checks configuration and data and prints a
summary (server types, sites, round-trip times, grid signals and workload). Its main parts:

```text
Server types
  name        slots   idle W   peak W  peak W/slot  boot s  description
  h100x8        128     1360     5406         42.2     180  8xH100, Llama-3.1-405B-Instruct-FP8 (...)
  b200x4        128     1160     4450         34.8     180  4xB200, same model (...)

Sites
  site   region             PUE  servers  slots  peak facility kW  pools; dispatch/power policy
  sh     shanghai          1.25        1    128               6.8  h100:h100x8x1; least_loaded/ordered
  ...
Round-trip times (ms, source -> site)
                sh      gd      gz      nm
  east         4.5    19.8    27.4    24.1   (share 0.4)
  ...
Workload azure-llm-2023-conv@7909210: 19366 requests, 0.97 h, 5.53 requests/s, ...
Scenario check passed
```

Any problem (misspelt fields, missing datasets, a workload window outside the data, ...) is reported with
its reason and a non-zero exit status.

== Running a simulation

```bash
cpc sim -s examples/quickstart/scenario.yaml --hours 0.9 --out results/qs-base
```

The simulation runs 0.9 hours with the policies of the scenario (nearest site `nearest` + servers switched
with the load `reactive`) and prints a summary:

```text
Simulation finished (0.67 s): routing nearest, capacity reactive, 0.9 h
  requests: arrived 18356, served 18356, rejected by sites 0, unrouted 0, unfinished at the end 0
  SLO attainment 99.99%; TTFT P50 0.199 s, P99 0.871 s
  facility energy 14.004 kWh (IT 11.449 kWh), cost 11.95 CNY, carbon 8.07 kg, 0.76 Wh per request
Result written to results/qs-base/
```

The SLO attainment is the share of requests whose time to first token and inter-token latency are both
within the limits of the scenario.

== Comparing policies

`examples/quickstart/experiment.yaml` lists three combinations of policies (variants):

```yaml
name: quickstart
scenario: scenario.yaml
hours: 0.9
bin_s: 60
variants:
  - {name: nearest-static,   routing: {policy: nearest},    capacity: {policy: static}}
  - {name: nearest-reactive, routing: {policy: nearest},    capacity: {policy: reactive}}
  - {name: cost-reactive,    routing: {policy: cost_aware}, capacity: {policy: reactive}}
```

```bash
cpc run examples/quickstart/experiment.yaml
```

The variants run in parallel and a comparison follows:

```text
  variant             served  rejected      SLO  TTFT P99  facility kWh  cost CNY  carbon kg
  nearest-static       18356         0   99.99%     0.865          17.9      15.7       10.0
  nearest-reactive     18356         0   99.99%     0.871          14.0      12.0        8.1
  cost-reactive        18356         0  100.00%     0.589           8.2       4.8        5.0
```

`static` keeps all servers on; `reactive` switches off servers the arrival rate does not need and saves
idle energy; `cost_aware` sends requests to sites with a lower effective price (price + carbon price ×
carbon intensity) and lowers cost and carbon further.

== Inspecting results

The result of each variant is a directory (result package), e.g. `results/quickstart/cost-reactive/`:

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("File", "Content"),
    [`summary.json`], [summary metrics: requests, SLO attainment, latency quantiles, energy, cost, carbon, with details per site],
    [`requests.parquet`], [one record per request: arrival, source, site, server, status, latencies, ...],
    [`timeseries.csv`], [one row per time bin: IT power, servers on, arrival rate, price and carbon intensity of each site],
    [`decisions.jsonl`], [decisions of the capacity policy per control period],
    [`run.yaml`], [run description: complete scenario, policies and parameters, platform version],
  ),
  caption: [Files of a result directory],
)

Reading results in Python:

```python
import cpc

run = cpc.read_run("results/quickstart/cost-reactive")
print(run["summary"]["facility_kwh"], run["summary"]["cost"])
df = run["requests"].to_pandas()                         # needs pandas
print(df[df.status == "served"].groupby("site").ttft_s.describe())
```

Plotting (IT power per site over time; needs pandas and matplotlib):

```python
import pandas as pd

ts = pd.read_csv("results/quickstart/cost-reactive/timeseries.csv")
ax = ts.plot(x="t_h", y=[c for c in ts.columns if c.endswith("_it_kw")], ylabel="IT kW")
ax.figure.savefig("it-power.png")
```

== Running a live experiment

A live experiment runs the same scenario on real time: the grid-signal service, the site agents, the
gateway and the controller start on this machine, and real HTTP requests are sent at the arrival times of
the workload. The servers of the sites are emulated: they are timed and their power computed from the
tables of the server type, so no GPU is needed.

```bash
cpc live -s examples/quickstart/scenario.yaml --duration 120 --rate-scale 0.2 \
    --out results/qs-live
```

`--duration 120` runs for 120 seconds; `--rate-scale 0.2` reduces the arrival rate to 20 % to spare the
machine. The command prints a summary of the same form as simulations and writes a result directory of
the same format, read and plotted the same way. @g-real describes how to connect real GPU servers.
