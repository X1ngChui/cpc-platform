#import "../../template/cpc-doc.typ": *

= Data specification <ch-data>

The platform takes three kinds of input data: inference request workloads (workload), grid signals
(grid_signal) and power profiles (power_profile). Each dataset is a directory with a descriptor and data
files. The platform specifies the core fields each kind needs for basic scheduling and carries everything
else in extension fields; it validates data against the descriptor and the kind specification before
loading and contains no parser for any particular raw dataset. Converting raw data into the platform
format is the user's task; `examples/datasets/` holds conversion examples for public datasets (@fig-data).

#figure(image("../../figures/en/data-workflow.pdf", width: 100%), caption: [Organisation, conversion and use of datasets]) <fig-data>

== Descriptors

Descriptors are a subset of Frictionless Data Package v2 @frictionless2024datapackage: package metadata
(name, version, licences, sources), data resources (path, format, bytes, hash) and table schemas (Table
Schema: field names, types, constraints, missing values). The standard allows custom properties on the
package, resources and fields; the platform's properties are the top-level `cpc` and the field property `unit`.

```yaml
name: azure-llm-2024-conv            # dataset name
version: dataset-llm-2024            # version (e.g. release tag or commit of the raw data)
title: Azure LLM Inference Trace 2024 (conversation, 1 week)
licenses: [{name: CC-BY-4.0}]
sources:
  - {title: Azure Public Dataset, path: https://github.com/Azure/AzurePublicDataset}
cpc:
  kind: workload                     # kind: workload / grid_signal / power_profile
  profile: 1                         # version of the kind specification
  origin: "2024-05-12T00:00:00+00:00"  # kind property: time origin of the workload
  citation: "..."
  conversion: {script: examples/datasets/azure_llm.py, zero_output: fail}
resources:
  - name: requests                   # resource name set by the kind
    type: table
    path: requests.parquet           # relative to the descriptor
    format: parquet                  # csv / parquet
    hash: "sha256:..."               # provenance (optional)
    schema:
      fields:
        - {name: arrival_s, type: number, unit: s,
           constraints: {required: true, minimum: 0}}
        - {name: input_tokens, type: integer, constraints: {required: true, minimum: 0}}
        - {name: output_tokens, type: integer, constraints: {required: true, minimum: 1}}
```

The platform reads the properties below; other properties are kept and not interpreted.

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("Property", "Description"),
    [`name`, `version`], [identify the dataset; results record the datasets used as `name@version`.],
    [`licenses`, `sources`, `title`, `description`], [licences and sources, for reference and the provenance of results.],
    [`cpc.kind`, `cpc.profile`], [kind and version of the kind specification (currently 1).],
    [`resources[].path`], [path relative to the directory of the descriptor.],
    [`resources[].format`], [`csv` or `parquet`.],
    [`resources[].hash`, `bytes`], [provenance in the "algorithm prefix" form (e.g. `sha256:<hex digest>`).],
    [`schema.fields[]`], [name, type (`string`, `number`, `integer`, `boolean`, `datetime`, `date`, `list`, ...), constraints (`required`, `unique`, `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `enum`), unit `unit`; `list` fields are separated by `delimiter` in CSV, with the element type `itemType`.],
    [`schema.missingValues`], [strings that mean missing in CSV (default: the empty string).],
  ),
  caption: [Descriptor properties read by the platform],
)

Scenarios give dataset paths relative to the scenario file or with environment variables, e.g.
`${CPC_DATA_DIR}/azure-llm-2024-conv`. `CPC_DATA_DIR` defaults to `~/.cache/cpc/datasets`; other
undefined variables make loading fail.

== Kind specifications

Each kind specifies one table resource and its fields: *core fields* are needed for basic scheduling,
must be declared in the descriptor with compatible types and units, and must not be empty;
*conventional extension fields* have agreed names and types that reference policies may use; all other
fields are *arbitrary extensions*, kept as they are and passed to policies when the scenario selects
them. `cpc data kinds` prints the fields of all kinds.

=== Workload (workload)

Resource `requests`, one inference request per row, ordered by `arrival_s`.

#figure(
  tbl(
    columns: (9.5em, 6.5em, 1fr),
    header: ("Field", "Type", "Description"),
    [`arrival_s` (core)], [number, s], [arrival time: seconds from the dataset origin, ≥ 0],
    [`input_tokens` (core)], [integer], [input tokens, ≥ 0],
    [`output_tokens` (core)], [integer], [output tokens, ≥ 1],
    [`request_id`], [string / integer], [request identifier],
    [`model`], [string], [requested model],
    [`session_id`], [string / integer], [session identifier (multi-turn conversations)],
    [`priority`], [integer], [priority, higher first],
    [`slo_class`], [string], [service class],
    [`tenant`], [string], [tenant or application],
    [`prefix_hashes`], [list], [prefix-block hashes, for KV-cache reuse analysis],
  ),
  caption: [Fields of workload datasets],
)

The descriptor property `cpc.origin` (a time with UTC offset) is the origin of `arrival_s`; it is optional
and omitted when the raw data have no absolute times. Scenarios select the extension fields passed to
policies with `workload.attributes`. Selected fields are request attributes (`RequestInfo.attrs`), visible
directly in simulation; in live experiments the replay of `cpc live` sends them as headers
`x-cpc-attr-<field>` (JSON values), which the gateway forwards to the site agents.

=== Grid signals (grid_signal)

Resource `signals` is a long table: each row gives the value of one signal for one region during one
period. A new kind of signal is a new value of the field `signal`, without schema changes. This matches
OpenADR 3.0, which expresses prices, greenhouse-gas and load-limit signals as interval payloads @openadr3.

#figure(
  tbl(
    columns: (9.5em, 6.5em, 1fr),
    header: ("Field", "Type", "Description"),
    [`region` (core)], [string], [region key (province, balancing area, ...), matched with `region` of sites],
    [`start`, `end` (core)], [datetime], [period \[`start`, `end`) with time zone],
    [`signal` (core)], [string], [signal, e.g. `price`, `carbon_intensity`],
    [`value` (core)], [number], [value; the unit is declared in `cpc.signals` of the descriptor],
    [`label`], [string], [period label, e.g. `peak`, `valley` of a time-of-use tariff],
    [`is_estimated`], [boolean], [whether the value is an estimate],
  ),
  caption: [Fields of grid-signal datasets],
)

The descriptor property `cpc.signals` declares every signal of the dataset and its unit, e.g.
`price: {unit: CNY/kWh}`, `carbon_intensity: {unit: kgCO2/kWh, basis: lifecycle}`. Prices must be in
`<currency>/kWh` and carbon intensity in `kgCO2/kWh`. Periods of the same region and signal must not
overlap; gaps between periods and times outside the covered range mean the signal is unavailable.

=== Power profiles (power_profile)

Resource `measurements`, one steady-state measurement of a device at a given concurrency per row, used to
calibrate the server model (@sec-calibration).

#figure(
  tbl(
    columns: (9.5em, 6.5em, 1fr),
    header: ("Field", "Type", "Description"),
    [`device` (core)], [string], [device model, e.g. `H100`],
    [`device_count` (core)], [integer], [devices of one inference instance, ≥ 1],
    [`model` (core)], [string], [model name],
    [`concurrency` (core)], [number], [mean concurrency (running sequences)],
    [`power_w` (core)], [number, W], [mean total power of all devices],
    [`itl_s` (core)], [number, s], [mean inter-token latency, > 0],
    [`max_concurrency`], [integer], [concurrency limit (e.g. `max_num_seqs` of vLLM)],
    [`throughput_tokens_per_s`, `ttft_s`, `energy_per_token_j`, `task`], [-], [output throughput, mean TTFT, energy per output token, workload task],
  ),
  caption: [Fields of power-profile datasets],
)

== Validation and loading

`cpc.datasets.descriptor.load_descriptor` reads a descriptor and checks its structure and kind;
`cpc.datasets.table.read_table` reads the columns needed with Apache Arrow, parses them with the declared
types and checks the constraints and the non-empty core fields. Every violation raises `DatasetError`; the
platform never corrects, drops or fills data. After loading, workloads are checked for arrival order and
grid signals for periods and units. `cpc data check <path>` validates a dataset completely and reports
row counts and statistics of its resources.

== Conversion examples

The scripts in `examples/datasets/` show how public data become datasets in the platform format. They are
not part of the platform; their choices (time zones, abnormal records, external parameters) are explicit
command-line options recorded in `cpc.conversion` of the descriptor.

#figure(
  tbl(
    columns: (11em, 7em, 1fr),
    header: ("Example", "Kind", "Raw data and notes"),
    [`azure_llm.py`], [workload], [Azure LLM Inference Trace 2023 (1 hour) and 2024 (1 week) @patel2024splitwise @azure2024trace. Timestamps are read as UTC and `cpc.origin` is the earliest; requests with 0 output tokens are an error or dropped, by explicit choice.],
    [`synthetic_workload.py`], [workload], [synthetic non-homogeneous Poisson arrivals, for tests and demonstrations only.],
    [`cn_grid/convert.py`], [grid_signal], [provincial commercial and industrial time-of-use tariff rules @gd2021tou @gz2023tou @nm2023tou @sh2022tou expanded into per-period prices (`label` is the period); the 2023 provincial average emission factors @mee2025factors are the constant carbon intensity over the converted period, an assumption recorded in the descriptor.],
    [`mlenergy.py`], [power_profile], [LLM run summaries of the ML.ENERGY Benchmark v3 @chung2025mlenergy @mlenergy2025v3.],
  ),
  caption: [Conversion examples],
)

New conversions can use `cpc.datasets.build.write_dataset`: given an Arrow table, field definitions and
descriptor properties, it writes the data file and the descriptor and validates them once against the
kind specification. Power profiles of your own servers are measured by `cpc measure`, which writes a
power_profile dataset directly (@sec-calibration).
