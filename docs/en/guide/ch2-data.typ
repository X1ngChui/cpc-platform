#import "../../template/cpc-doc.typ": *

= Data <g-data>

The platform uses three kinds of data: request workloads (workload), grid signals (grid_signal) and power
profiles (power_profile). Each dataset is a directory with a descriptor `datapackage.yaml` and a data file
(CSV or Parquet). The descriptor gives name, version, sources and licence of the dataset and the name, type
and unit of every field of the data file; the platform reads and checks the data accordingly. The design
document, chapter "Data specification", defines the specification completely.

== Data directory

Scenarios refer to datasets by path, usually `${CPC_DATA_DIR}/<dataset>`. Without the environment
variable `CPC_DATA_DIR` the directory is `~/.cache/cpc/datasets`. To use another location (e.g. a larger
disk), set it in the shell:

```bash
export CPC_DATA_DIR=/data/cpc-datasets       # add to ~/.bashrc to keep it
```

Raw data and converted datasets both live in this directory and are not committed to the repository.
Conversion scripts put the raw files they download under `${CPC_DATA_DIR}/raw/`.

== Fields of the three kinds

Each kind specifies *core fields*, needed for basic scheduling and never empty. *Conventional extension
fields* have agreed names and meanings that reference policies may use; all other fields are *arbitrary
extensions*, kept as they are and selectable in the scenario as information for policies.
`cpc data kinds` prints all field definitions.

#figure(
  tbl(
    columns: (7.5em, 9em, 1fr),
    header: ("Kind", "Core fields", "Description"),
    [workload], [`arrival_s`, `input_tokens`, `output_tokens`], [one request per row: arrival time (seconds from the dataset origin, non-decreasing), input and output tokens. Conventional extensions: `request_id`, `model`, `session_id`, `priority`, `slo_class`, `tenant`, `prefix_hashes`.],
    [grid_signal], [`region`, `start`, `end`, `signal`, `value`], [each row is the value of one signal for one region during \[start, end). `signal` is `price`, `carbon_intensity` or a signal of your own; units are declared in `cpc.signals` of the descriptor.],
    [power_profile], [`device`, `device_count`, `model`, `concurrency`, `power_w`, `itl_s`], [each row is one steady-state measurement: device model and count, model, mean concurrency, mean total power of all devices (W), mean inter-token latency (s). Used to generate server types.],
  ),
  caption: [The three kinds of datasets],
)

== Converting public datasets

The scripts in `examples/datasets/` convert commonly used public data into the platform format. They
download the raw files by default; for files already downloaded, `--raw` gives the file.

#figure(
  tbl(
    columns: (15em, 1fr),
    header: ("Command (all in examples/datasets/)", "Dataset produced"),
    [`azure_llm.py --variant 2023-conv --zero-output fail`], [`azure-llm-2023-conv`: about one hour of requests of an Azure conversation service (also `2023-code`; `2024-conv` and `2024-code` cover one week, raw files of about 1 GB)],
    [`cn_grid/convert.py --start 2026-01-01 --end 2027-01-01`], [`cn-grid-2026`: time-of-use prices and carbon intensity of Guangdong, Guizhou, western Inner Mongolia and Shanghai],
    [`mlenergy.py`], [`mlenergy-benchmark-v3-llm`: power and inter-token latency of models on H100 and B200],
  ),
  caption: [Conversion examples],
)

Choices are explicit options. For example, the platform requires at least one output token:
`--zero-output fail` stops at requests with 0 output tokens, `--zero-output drop` removes them and records
their number in the descriptor. The dataset of `mlenergy.py` is gated: request access on its Hugging Face
page and put the token into the environment variable `HF_TOKEN`.

== Checking and inspecting datasets

```bash
cpc data check ${CPC_DATA_DIR}/azure-llm-2023-conv     # complete check with statistics
cpc data show  ${CPC_DATA_DIR}/azure-llm-2023-conv     # descriptor: sources, licence, fields
cpc data kinds                                         # field definitions of the three kinds
```

Output of `cpc data check`:

```text
azure-llm-2023-conv@7909210 (workload) is valid: requests 19366 rows
{
  "ref": "azure-llm-2023-conv@7909210",
  "requests": 19366,
  "duration_s": 3501.7,
  "mean_rps": 5.53,
  ...
}
```

For data that violate the specification, the output names dataset, field, constraint and value, e.g.
"dataset my-trace\@1, resource requests, field output_tokens: value 0 violates specification minimum 1".

== Preparing your own data

Your own data need two steps: write the data as a table, then write a descriptor. The example is a request
log in CSV.

*Step 1: the data file.* Column names are the field names of the kind; further columns become extension fields.

```text
my-trace/requests.csv
arrival_s,input_tokens,output_tokens,tenant
0.000,812,210,a
0.814,95,64,b
1.502,2048,512,a
...
```

*Step 2: the descriptor.* Create `datapackage.yaml` in the same directory:

```yaml
name: my-trace                     # dataset name (lower-case letters, digits, hyphens)
version: "2026.11"                 # version, any string
title: Inference request log of the business system
licenses: [{name: internal}]
sources: [{title: export of the business-system log, 2026-11-01}]
cpc:
  kind: workload                   # kind
  profile: 1                       # version of the kind specification, currently 1
  origin: "2026-11-01T00:00:00+08:00"   # time of arrival_s = 0 (optional)
resources:
  - name: requests                 # the resource of workloads is always requests
    path: requests.csv
    format: csv
    schema:
      fields:
        - {name: arrival_s, type: number, unit: s}
        - {name: input_tokens, type: integer}
        - {name: output_tokens, type: integer}
        - {name: tenant, type: string, description: application identifier}
```

Then check it with `cpc data check my-trace`. Alternatively, `cpc.datasets.build.write_dataset` writes
data file and descriptor in one step and checks them, as all scripts in `examples/datasets/` do:

```python
import pyarrow as pa
from cpc.datasets.build import write_dataset

table = pa.table({"arrival_s": [0.0, 0.814, 1.502], "input_tokens": [812, 95, 2048],
                  "output_tokens": [210, 64, 512], "tenant": ["a", "b", "a"]})
fields = [{"name": "arrival_s", "type": "number", "unit": "s"},
          {"name": "input_tokens", "type": "integer"},
          {"name": "output_tokens", "type": "integer"},
          {"name": "tenant", "type": "string"}]
write_dataset("my-trace", name="my-trace", version="2026.11", kind="workload",
              table=table, fields=fields, licenses=[{"name": "internal"}],
              cpc={"origin": "2026-11-01T00:00:00+08:00"})
```

The data file of grid signals is a long table with one region, period and signal per row, e.g. hourly
prices of two regions:

```text
region,start,end,signal,value
east,2026-07-15T00:00:00+08:00,2026-07-15T01:00:00+08:00,price,0.31
east,2026-07-15T01:00:00+08:00,2026-07-15T02:00:00+08:00,price,0.29
west,2026-07-15T00:00:00+08:00,2026-07-15T01:00:00+08:00,price,0.22
...
```

Its descriptor has `cpc.kind` `grid_signal`, the resource `signals`, `start` and `end` of type `datetime`,
and declares the units of the signals:

```yaml
cpc:
  kind: grid_signal
  profile: 1
  signals:
    price: {unit: CNY/kWh}              # price units must be <currency>/kWh
    carbon_intensity: {unit: kgCO2/kWh} # carbon intensity must be kgCO2/kWh
```

== Passing extension fields to policies

Extension fields of a workload can reach the scheduling policies as request attributes; list them in
`workload.attributes` of the scenario:

```yaml
workload:
  dataset: /data/my-trace
  attributes: [tenant]        # must be declared in the descriptor
```

Policies read `req.attrs["tenant"]` (@g-policy). In simulation the attributes travel with the requests;
in live experiments the replay of `cpc live` sends the header `x-cpc-attr-tenant`, which the gateway
forwards to the site agent. Other signals of a grid-signal dataset (renewable share, for instance) are
available to policies in `signal.extra` once declared in `grid.extras` of the scenario (@g-scenario).

== Generating server types <g-profile>

A server type describes a server with two tables: `power_w`, the server IT power over occupancy, and
`tpot_s`, the inter-token latency over occupancy, interpolated linearly between points. There are three
ways to obtain them.

*From measurements.* Every measurement of a power-profile dataset (power and inter-token latency at a
concurrency) is a point of the tables:

```bash
cpc profile ${CPC_DATA_DIR}/mlenergy-benchmark-v3-llm \
    --slots 128 --idle-w 1360 --host-w 800 \
    --where device=H100 device_count=8 model=meta-llama/Llama-3.1-405B-Instruct-FP8 \
            task=lm-arena-chat \
    --prefill-tokens-per-s 5000 --boot-s 180 --name h100x8
```

`--where` selects one group of measurements (same device, device count and model; with several tasks in
the group also `task`); `--slots` is the server's slots and occupancy = measured concurrency / slots. The
measurements contain device power while serving only, so the online idle power (`--idle-w`, the first point
of the table) and the host power (`--host-w`, added to every point) are given separately. The command
prints YAML to copy under `node_profiles` of the scenario:

```yaml
h100x8:
  description: 8xH100, meta-llama/Llama-3.1-405B-Instruct-FP8; ...
  slots: 128
  power_w:
  - [0.0, 1360.0]
  - [0.0624, 4060.4]
  ...
  tpot_s:
  - [0.0624, 0.03406]
  ...
  prefill_tokens_per_s: 5000.0
  boot_s: 180.0
```

`cpc data check <dataset>` lists the measurement groups of a power-profile dataset. Your own servers are
measured with `cpc measure` (@g-calibrate); the power_profile dataset it writes also records slots, online
idle power and boot time, which `cpc profile` uses directly.

*By hand.* With known power and inter-token latency at a few occupancies, write them down; at least idle
(occupancy 0) and one busy point are needed:

```yaml
my-server:
  slots: 32
  power_w: [[0, 180], [0.25, 260], [0.5, 320], [1, 400]]
  tpot_s: [[0, 0.03], [1, 0.05]]
  prefill_tokens_per_s: 4000
  boot_s: 90
```

*From a formula.* A formula for power is evaluated at a few occupancies and written into the table, e.g.:

```python
# power per GPU = idle + (max - idle) * occupancy**alpha
gpus, gpu_idle, gpu_max, alpha = 8, 60, 400, 0.7
host_idle, host_max = 800, 1500                         # host power linear in occupancy
us = [0, 0.25, 0.5, 0.75, 1]
def server_w(u):
    gpu = gpu_idle + (gpu_max - gpu_idle) * u**alpha
    host = host_idle + (host_max - host_idle) * u
    return round(gpus * gpu + host, 1)

table = [[u, server_w(u)] for u in us]
# [[0, 1280.0], [0.25, 2485.7], [0.5, 3304.4], [0.75, 4028.9], [1, 4700.0]]
print(table)
```
