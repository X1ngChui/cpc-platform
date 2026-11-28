# cpc-platform: energy-aware AI inference scheduling across data centres

[中文](README.zh-CN.md)

A platform for research on the coordination of computing and the power grid: AI inference requests are
scheduled across data centres with knowledge of electricity prices, carbon intensity and power limits.
Workloads, grid signals and power profiles enter through one dataset specification; scenarios describe data
centres, server pools and the grid; scheduling is organised in global (across data centres) and local (inside a
data centre) policy interfaces; and the same server models, data-centre runtime and policies serve both
discrete-event simulation and live experiments with real inference instances.

- **Dataset specification**: a dataset is a descriptor (subset of Frictionless Data Package v2 plus the `cpc`
  kind specification) with data files (CSV / Parquet). Every kind specifies core fields; anything else is an
  extension field that can be passed to policies. The platform validates and loads data only; conversion
  examples for raw public data are in `examples/datasets/` (Azure LLM Inference Trace 2023 / 2024, ML.ENERGY
  Benchmark, Chinese provincial time-of-use tariffs and emission factors).
- **Shared simulation and live experiments**: scenarios, server models (power and inter-token latency as
  tables over occupancy), the data-centre runtime (server states, queueing and dispatch, capacity
  instructions, energy metering) and policies are one implementation for both modes; emulated and real
  servers may share a site in live experiments.
- **Two-level scheduling**: global routing and capacity policies depend only on state summaries of the data
  centres; local dispatch and power policies work inside a data centre and may use request attributes and
  heterogeneous server pools. Policies register as entry points, so new ones do not modify the platform.
- **Energy measurement**: NVIDIA GPUs (NVML), CPU and memory (RAPL), whole servers (local BMC, IPMI DCMI);
  other devices through a command adapter or sensors registered as entry points. Metrics use the Prometheus
  format; GPU metrics carry the dcgm-exporter names.
- **Explicit failures**: configuration and environment errors stop at startup; faults at run time stay
  isolated and are reported through health checks, state fields and metrics; missing data are never
  replaced by other values.
- **Result packages**: simulations and live experiments write the same result package (per-request records,
  time series, summary, decisions, run description) recording scenario, policies, datasets and version.

Server components target Linux (x86_64, aarch64); development uses Linux or WSL2 (Ubuntu).

## Documentation

| Document | English | Chinese | Content |
|---|---|---|---|
| Design document | [design-en.pdf](docs/pdf/design-en.pdf) | [design-zh.pdf](docs/pdf/design-zh.pdf) | architecture, dataset specification, scenarios and models, data-centre runtime, scheduling policies, energy measurement and accounting, service interfaces, simulation and live experiments, extension |
| User guide | [guide-en.pdf](docs/pdf/guide-en.pdf) | [guide-zh.pdf](docs/pdf/guide-zh.pdf) | installation, data preparation, scenarios, simulation, live experiments (local, cloud, real servers), writing policies, observation and operation, development |

## Quick start

```bash
git clone https://github.com/X1ngChui/cpc-platform.git && cd cpc-platform
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest                                    # unit tests
python scripts/smoke_test.py --offline    # end-to-end smoke test (test scenario, no downloads)

# data of the quick-start scenario (written to ${CPC_DATA_DIR:-~/.cache/cpc/datasets}, about 1 MB)
python examples/datasets/azure_llm.py --variant 2023-conv --zero-output fail
python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01

S=examples/quickstart/scenario.yaml
cpc check -s $S                                     # check scenario and data, print a summary
cpc sim -s $S --hours 0.9 --out results/qs          # simulation, written as a result directory
cpc run examples/quickstart/experiment.yaml         # compare several policy combinations
cpc live -s $S --duration 120 --rate-scale 0.2      # live experiment, same result format as simulation
cpc compare results/quickstart/nearest-reactive results/quickstart/cost-reactive   # compare two results
```

In Python: `cpc.simulate("examples/quickstart/scenario.yaml", routing="cost_aware", hours=0.9)` and
`cpc.read_run(<result dir>)`. The default scenario `configs/scenario.yaml` uses one week of the Azure 2024
workload (`azure_llm.py --variant 2024-conv`, about 1 GB of raw files). Cloud deployment and real servers are
described in the user guide.

## Images

`ghcr.io/x1ngchui/cpc-platform` (built by GitHub Actions) and `xingchui/cpc-platform` (Docker Hub), both
amd64 / arm64. The entry point is `cpc`:

```bash
docker run --rm --gpus all ghcr.io/x1ngchui/cpc-platform:0.4.2 sensors --types nvml
```

## Layout

```text
src/cpc/              datasets · datacenter (data-centre runtime) · sched (policies) · sim · results
                      grid / site / gateway / controller / live (services and live experiments)
                      sensors (power sensors) · measure · cli
examples/datasets/    conversion examples for raw public data
examples/experiments/ experiment files
examples/quickstart/  quick-start scenario and experiment file
configs/              scenarios and endpoints
deploy/               cloud (site and control stacks) · prometheus recording rules · grafana dashboard
scripts/              end-to-end smoke test
tests/                unit tests and test data
docs/                 Typst sources (zh, en), draw.io figure sources and PDFs
```

## Notes

Parameters of server types that do not come from measurements (online idle power, host power, prefill
throughput, boot time) and the shoulder-period prices of the time-of-use tariffs are illustrative; with real
servers, generate server types from measurements (`cpc measure`, `cpc profile`). The built-in policies show
the interfaces and data paths and serve as baselines; they are not recommended research methods.
