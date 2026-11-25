# Changelog

Versions follow semantic versioning.

## v0.4.0 (2026-11-26)

### Changed

- All code comments, docstrings, log messages and command-line output are English. The documentation exists
  in Chinese and English (`docs/zh/`, `docs/en/`, PDFs `docs/pdf/<name>-<lang>.pdf`), sharing one template;
  the figures are black and white in both languages. `README.md` is English, `README.zh-CN.md` Chinese.
- The code was simplified with dataclasses (slots), `match` statements, `collections.Counter`, `bisect` and
  assignment expressions; behaviour is unchanged (the quick-start results are identical).

### Removed

- Built-in policies without a distinct purpose: routing `round_robin`; dispatch `packing` and `priority`
  (the `priority()` hook of dispatch policies stays). All extension points remain: plugin registry and entry
  points, request attributes, extra grid signals, gateway demand statistics and routing weights.
- `cpc loadgen` (the replay is part of `cpc live`; `cpc live --local` connects to services of `cpc up`),
  `cpc report` and the `analysis` extra (result tables are read and plotted with common tools), and the
  generic exporter `cpc exporter` (the site agent exports the same sensor metrics).
- Kubernetes/kind and local Docker Compose deployments, `deploy/cloud/netem.sh`, the scenario option
  `network.overrides`.
- Dataset converters for BurstGPT, Mooncake, EIA-930 and constant signals, and the US scenario.
- Power sensors reduced to four that measure one class of objects each without overlap: `nvml` (NVIDIA
  GPUs), `rapl` (CPU and memory), `ipmi` (whole server through the local BMC) and the generic `command`.
  Removed `nvidia-smi` (duplicate of `nvml`), `amdsmi`, `hwmon`, `ascend-dcmi`, `redfish`, `prometheus`,
  `http-json` and the remote-BMC mode of `ipmi`; other devices use `command` or a sensor registered in the
  entry-point group `cpc.sensors`. The cloud site stack no longer includes dcgm-exporter.

### Added

- Site agents fetch their configuration from the control node: the grid-signal service serves
  `GET /v1/config` (scenario text and service addresses, admin token), and `cpc site --control <address>
  --site <id>` replaces `-s` and `-e`. Scenario and endpoints are maintained on the control node only.
- `cpc measure`: keeps fixed concurrency levels on a real server of a site and measures power,
  inter-token latency, TTFT and throughput, optionally power while off and boot time, writing a
  power_profile dataset; `cpc profile` takes slots, idle power, power while off and boot time from such
  datasets. It warns when all levels show almost the same power (a frozen BMC reading).
- `cpc compare`: compares result directories (simulation or live) side by side in total and per site, with
  ratios for two directories; Python interface `cpc.results.compare_runs()`.
- `cpc live --local`: connects to services already running at the local addresses.
- Override file `deploy/cloud/site-stack/docker-compose.powercap.yml` for hardware power capping of GPU site
  stacks (`--enable-power-cap` with `SYS_ADMIN`); NVML warns when a requested limit is below the device minimum.
  Verified on NVIDIA A30.
- CPU site stack `deploy/cloud/site-stack/docker-compose.cpu.yml` (llama.cpp; power from RAPL and the local BMC).
- Generic sensor parameter `max_w`: readings above it count as missing (not integrated, never replaced),
  removing sporadic BMC glitches.
- CPU-only real servers: upstreams may declare no accelerators; their power is the host part
  (`measurement.host` `server` or `cpu_dram`).
- Scenario parameter `scheduler.stream_idle_timeout_s` (default 120 s): streams stalling longer are aborted
  and count as failed.
- `logs/` ships with the repository.

### Fixed

- The site agent always failed to start with `--enable-power-cap`: the capability check queried the device
  map before the sensors had started.
- The control-stack compose file was invalid YAML (since v0.2.0); a test now parses every YAML file.
- RAPL inside containers: Docker masks `/sys/devices/virtual/powercap`; the sensor's `root` accepts the
  nested layout mounted elsewhere (e.g. `/host/powercap`). The image includes `ipmitool`.
- Actions on one server run in order; a power-on issued during a power-off could leave the container exited.
- Failed streams counted as served: the site agent now waits for the first upstream chunk and returns
  upstream errors with their status; gateway and `cpc live` check stream completeness; failed requests no
  longer enter the mean service time.
- `PUT /admin/policy` of gateway and controller returns 400 with an explanation for misspelt policies or
  parameters (500 in v0.3.0).
- Online real servers whose inference instance failed kept receiving requests; the site agent now checks them
  every 5 s and restarts the instance after 2 failed checks. Real servers not ready within the boot deadline
  (4 × `boot_s`, at least 300 s) fail to boot instead of staying in booting.
- Replayed prompts use short common words, so the input length estimated by the site agent equals the trace
  (it was 1.35 times longer).
- Workloads shorter than the experiment, and unknown sensor parameters, are reported as one-line errors.
- `cpc.read_run()` returns numeric time-series cells as float and empty cells as None.

## v0.3.0 (2026-11-25)

### Simplified

- Server types are tables only: `power_w` (occupancy → server IT power, first point online idle) and `tpot_s`
  (occupancy → inter-token latency), interpolated linearly; the parametric model fields were removed. Power
  capping limits power to idle + cap ratio × (full load − idle).
- Removed the Kubernetes executor of the controller, `cpc data fetch` and URL resources, `cpc compare`,
  `cpc signals`, `cpc rtt`, `/v1/forecast` of the grid-signal service, routing policy `random`, the response
  header `x-cpc-energy-est-j` and the emulated accelerator metrics of emulated servers.

### Added

- `cpc check` (check a scenario and its data), `cpc live` (live experiment writing the result format of
  simulations) and `cpc profile` (server type from a power-profile dataset, replacing `cpc data calibrate`).
- Python interface `cpc.simulate()` and `cpc.read_run()`; `cpc data` actions `kinds`, `check`, `show`.
- Quick-start scenario `examples/quickstart/`; the guide rewritten along the workflow.

### Error messages

- Common usage errors are one-line messages with exit status 2 instead of tracebacks: unknown policies (with
  close names and all choices), unaccepted parameters, malformed JSON options, simulations longer than the
  workload, YAML syntax errors, numeric options out of range.
- Field errors of scenarios, experiment files and endpoints files list field path and reason; unknown
  policies and parameters raise `ConfigError`.
- `cpc run` checks the policies, parameters and workload fields of all variants before starting subprocesses.
- `--routing-params` / `--capacity-params` alone update the parameters of the scenario's policy.
- `clock.speedup` must be greater than 0.

## v0.2.0 (2026-11-25)

### Data

- Datasets are described by descriptors: `datapackage.yaml` (a subset of Frictionless Data Package v2 with
  the property `cpc`) and CSV / Parquet files. The kinds `workload`, `grid_signal` and `power_profile` specify
  core and conventional fields; other fields are arbitrary extensions; data are validated and loaded with
  Apache Arrow.
- No raw-data parsers or bundled data in the platform; conversion examples moved to `examples/datasets/`.
- Scenarios refer to datasets by path (with `${CPC_DATA_DIR}`); grid signals name dataset and signal and may
  declare extra signals (`grid.extras`); workloads may pass extension fields as request attributes
  (`workload.attributes`, `x-cpc-attr-*` headers in live experiments).

### Models and operation

- Data-centre runtime `cpc.datacenter` (server states, local queueing and dispatch, capacity instructions,
  energy metering), clock-independent and driven by both the simulator and the site agent.
- Sites consist of server pools, possibly heterogeneous; emulated and real servers may share a site; power
  measurement is declared per site (`measurement`); `region` replaces `province`.
- Arrival rates use exponential weighting independent of the update interval (`scheduler.rate_window_s`).
- The simulator streams arrivals in trace order; results are result packages; experiment files and `cpc run`.

### Scheduling

- Global (routing, capacity) and local (dispatch, power) policies; dispatch policies `least_loaded`,
  `packing`, `efficient_first`, `affinity`, `priority` and power policies `ordered`, `efficient`.
- Policies and power sensors register as entry points (`cpc.routing`, `cpc.capacity`, `cpc.dispatch`,
  `cpc.power`, `cpc.sensors`).

### Failure handling

- Configuration and environment errors at startup; faults at run time isolated and reported through health
  checks, state fields and metrics; missing readings and signals are marked missing. Gateway retries and the
  controller's input checks.
- Services target Linux (x86_64, aarch64) only.

### Interfaces and deployment

- Currency-independent grid-signal fields and metrics (`price`, `price_label`, `carbon_intensity`,
  `currency`); pool summaries in state summaries; the grid-signal service mounts the dataset directory in all
  deployments; smoke test `--offline`.

### Documentation

- Design document and guide rewritten; figures as draw.io sources exported to vector graphics.

## v0.1.0

- First version: grid-signal service, site agent, routing gateway, capacity controller, discrete-event
  simulator, power sensors for several kinds of hardware and a generic power exporter; local, Docker Compose,
  Kubernetes (kind) and cloud deployments.
