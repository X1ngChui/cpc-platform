#import "../../template/cpc-doc.typ": *

= Observation and operation <g-ops>

Every service exposes Prometheus metrics at `/metrics`; the control stack of cloud deployments contains
Prometheus and a Grafana dashboard (@g-cloud). The design document, chapter "Energy measurement and
accounting", defines all metrics and recording rules.

== Frequently used metrics

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("Metric", "Meaning"),
    [`cpc_site_it_power_watts`, `cpc_site_facility_power_watts`], [IT and facility power of a site (W)],
    [`cpc_site_nodes_on`, `cpc_site_queue_length`], [online servers and queued requests of a site],
    [`cpc_node_it_power_watts`], [server IT power (labels `site`, `pool`, `node`, `source`: model for the server model, sensors for measurements)],
    [`cpc_node_energy_joules_total`], [accumulated IT energy of a server (J)],
    [`DCGM_FI_DEV_POWER_USAGE`], [GPU power of real servers (W), named as by NVIDIA dcgm-exporter],
    [`cpc_grid_price_per_kwh`, `cpc_grid_carbon_intensity_kg_per_kwh`], [current price and carbon intensity of every site],
    [`cpc_gateway_ttft_seconds`, `cpc_gateway_e2e_seconds`], [distributions (histograms) of TTFT and end-to-end latency],
    [`cpc_gateway_route_decisions_total`], [routing decisions (labels `policy`, `reason`)],
    [`cpc_sensor_up`, `cpc_upstream_up`], [whether a sensor or upstream service is healthy (1) or failed (0)],
  ),
  caption: [Frequently used metrics],
)

Example queries on the Prometheus page:

```text
site:facility_power_watts:sum                       facility power per site
sum(site:energy_cost_per_hour)                      global cost rate (per hour)
sum(site:carbon_kg_per_hour)                        global carbon rate (kg per hour)
increase(cpc_node_energy_joules_total[10m]) / 3.6e6 IT energy per server in the last 10 minutes (kWh)
histogram_quantile(0.99, sum by (le, site) (rate(cpc_gateway_ttft_seconds_bucket[5m])))
```

== Power sensors <g-sensors>

```bash
# all sensor types, their requirements and whether this host meets them
cpc sensors --list
cpc sensors --types nvml                            # read the given sensors three times
cpc sensors --types nvml,rapl --count 5 --interval 2
cpc sensors --types nvml --json                     # JSON output for scripts
```

The platform never chooses sensors by itself: the sensors read by commands and by site agents are always
given explicitly. When constructing a sensor fails (driver or library unavailable, required capability
missing), the command exits with status 2 and the reason; when a field fails during reading, it shows as
`-` and the error is listed.

There are four built-in sensors, each measuring one class of objects without overlap:

#figure(
  tbl(
    columns: (6em, 11em, 1fr),
    header: ("Type", "Measures", "Requirements and parameters"),
    [`nvml`], [NVIDIA GPUs], [NVIDIA driver and `nvidia-ml-py` (`pip install -e ".[nvml]"`); `gpus` selects GPU indices (default all); supports power capping],
    [`rapl`], [CPU packages and memory], [powercap of bare-metal Linux (most cloud VMs lack it); reading needs root; mounting in containers is in the FAQ below; `root` sets the powercap directory],
    [`ipmi`], [whole server], [local BMC (`/dev/ipmi0`) and `ipmitool`; map `/dev/ipmi0` into containers; some BMCs return sporadic glitches, so set `max_w`],
    [`command`], [any other device], [runs a command and extracts the power (W) with a regular expression: `cmd`, `regex`, `kind` (reading kind), `device`, `scale`],
  ),
  caption: [Built-in sensors],
)

All sensors accept the generic parameters `name` (distinguishing instances of one type) and `max_w`
(plausible upper bound of power readings; larger readings count as missing). Devices without a built-in
sensor (accelerators of other vendors, smart PDUs, ...) use `command` with the vendor's command-line tool,
or a sensor class of your own (subclass of `cpc.sensors.Sensor` implementing `read()`) registered in the
entry-point group `cpc.sensors` the same way as policies (@g-policy). In WSL2 only `nvml` is available.

== Sensors in a configuration file

Sensors with parameters are described in YAML:

```yaml
# sensors.yaml
sensors:
  - {type: nvml, gpus: [0]}
  - {type: rapl}
  - {type: ipmi, max_w: 2000}                 # readings above 2000 W count as missing
  # other devices: command-line tool + regular expression (check the output of your version)
  - {type: command, kind: npu, vendor: huawei,
     cmd: ["npu-smi", "info", "-t", "power", "-i", "0"],
     regex: "Power Dissipation\\(W\\)\\s*:\\s*([0-9.]+)"}
```

```bash
cpc sensors --spec sensors.yaml
```

The same list goes into `measurement.sensors` of a site in the scenario, for the site agent, together with
`measurement.host` deciding the host part of power (`server`: whole-server reading minus accelerators;
`cpu_dram`: sum of CPU and memory readings; `excluded`: not counted).

== Health and troubleshooting <g-health>

=== Health and readiness

Every service provides `/healthz` (liveness) and `/readyz` (readiness; 503 with the reason when not ready).
Faults stay within the readings or components they affect and are reported as follows:

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("Where", "Content"),
    [site `/state`], [`power_missing` (missing power components) and `last_error` (failed power or cap actions) of servers; `components.sensors` (health, last error and probed capabilities of every sensor); `energy_coverage` (share of time with valid power)],
    [gateway `/state`, `/readyz`], [`upstreams`: health of the grid-signal service and of every site; sites usable in the routing view],
    [controller `/state`], [`upstreams`; `result` of the last period (`applied` / `skipped`), the reason for skipping and the excluded sites],
    [metrics], [`cpc_sensor_up`, `cpc_sensor_errors_total`, `cpc_upstream_up{component,upstream}`, `cpc_upstream_errors_total`, `cpc_gateway_attempts_total{site,outcome}`, `cpc_gateway_route_decisions_total{policy,reason}`, `cpc_controller_cycles_total{result,reason}`, `cpc_controller_collect_errors_total{target}`, `cpc_node_actuation_errors_total`],
    [logs], [state changes (`sensor_down`, `upstream_up`, ...), skipped control periods and failed actions; with `CPC_LOG_FORMAT=json` one JSON object per line with an `event` field],
  ),
  caption: [Explicit reporting of faults],
)

=== Startup failures

Configuration and environment errors are reported at startup, and the command exits with status 2 (3 when
uvicorn reports the failed startup of a service). Frequent causes:

- unknown fields or broken references in the scenario; datasets violating the specification, missing files
  or a window outside the data;
- no endpoints: except for `cpc up`, commands reaching other services need `-e` / `CPC_ENDPOINTS` or `--local`;
- a site with real servers declares no `measurement`, or its sensors are unavailable or do not cover the
  accelerators of the servers;
- gateway, controller or `cpc live` cannot synchronise the signal clock from the grid-signal service in
  time (`--startup-timeout` of gateway and controller, 30 s by default). Docker Compose restarts these
  services by its restart policy, and they recover once the grid-signal service is ready.

== Commands and environment variables <g-ref>

=== Commands

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("Command", "Purpose"),
    [`cpc check`], [check a scenario and its data; print server types, sites, round-trip times, grid signals and workload (`-s`, `--date`)],
    [`cpc data`], [datasets: `kinds` (field definitions), `check` (validation and statistics), `show` (descriptor summary)],
    [`cpc measure`], [measure power and inter-token latency of a real server at several concurrency levels; write a power_profile dataset (`--site`, `--boot`)],
    [`cpc compare`], [compare two or more result directories (simulation or live) in total and per site; ratios for two directories],
    [`cpc profile`], [server type from a power-profile dataset (`--where`, `--slots`, `--idle-w`, `--host-w`)],
    [`cpc sim`], [one simulation (`--hours`, `--routing`, `--capacity` with parameters, workload transformation, `--out`)],
    [`cpc run`], [simulation variants of an experiment file (`--jobs`, `--out`)],
    [`cpc live`], [one live experiment with a result directory (`--duration`, `--speedup`; `-e` for deployed services, `--local` for services of `cpc up`)],
    [`cpc up`], [all services in one process until Ctrl+C (`--speedup`, `--routing`, `--capacity`, `--interval`, `--no-controller`)],
    [`cpc event`], [inject a demand-response event (`--site`, `--cap-kw`, `--duration-min`)],
    [`cpc grid` / `site` / `gateway` / `controller`], [start one service (`-e` endpoints file or `--local`; `--host`, `--port` override the listen address; `cpc site --control <grid-signal service>` fetches scenario and addresses from the control node)],
    [`cpc sensors`], [list or read power sensors (`--list`, `--types`, `--spec`)],
    [`python scripts/smoke_test.py`], [end-to-end smoke test (`--offline` with test data; `--external -e <endpoints>` checks a deployment)],
  ),
  caption: [Command reference],
)

=== Environment variables

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("Variable", "Purpose"),
    [`CPC_ENDPOINTS`], [endpoints file (same as `-e`)],
    [`CPC_DATA_DIR`], [dataset directory, default `~/.cache/cpc/datasets`],
    [`HF_TOKEN`], [Hugging Face token (conversion examples downloading gated raw data)],
    [`CPC_LOG_FORMAT`], [log format: `text` (default) or `json` (structured logs)],
    [`CPC_ADMIN_TOKEN`], [admin token; checked by services and sent automatically by the controller and the command line],
    [`CPC_LOG_LEVEL`], [log level, default INFO],
  ),
  caption: [Environment variables],
)

== Frequently asked questions <g-faq>

#let qa(q, a) = block(below: 1em, { set par(justify: false, first-line-indent: 0em); [#strong(q) \ #a] })

#qa[A port is in use.][`cpc up` uses 9000, 8080, 9300 and 9101 upwards by default. An endpoints file with
other ports, passed with `-e`, changes them; cloud deployments change the port mappings in
`docker-compose.yml`. `ss -ltnp | grep :8080` shows the process holding a port.]

#qa[Requests are slow or fail, and the machine uses an HTTP proxy.][Platform services ignore proxy
environment variables for internal calls, but tools such as `curl` still use the proxy: add
`--noproxy '*'` for local services. A proxy at `127.0.0.1` inherited by Docker containers is unreachable
inside them; use a proxy address reachable from the containers or none.]

#qa[Grafana of the control stack shows no data.][Check that all targets are UP on the Prometheus targets
page (`http://localhost:9090/targets`) and that `prometheus.yml` has the site addresses; the dashboard shows
the last 30 minutes by default, so run an experiment (`cpc live`) first.]

#qa[The SLO attainment is low.][Frequent causes: the capacity policy scales down too fast, servers boot
slowly (`boot_s`), or the workload exceeds the total capacity. `cpc check` gives the slots of each site,
to compare with mean arrival rate × mean service time; `overflow_*` in `route_reasons` and `rejected` in
simulation results indicate missing capacity.]

#qa[NVML is unavailable (`pynvml` import fails or `NVML Shared Library Not Found`).][Install
`pip install nvidia-ml-py`; Linux containers need the NVIDIA Container Toolkit and `--gpus all`; in WSL2
the host driver provides `libnvidia-ml.so` through `/usr/lib/wsl/lib`. Check the driver with `nvidia-smi` first.]

#qa[The power reading does not follow the load.][The BMC readings of some servers are never updated (DCMI
and SDR readings identical with and without load). Before connecting a real server, run
`cpc sensors --types ipmi,rapl --count 5 --interval 2` once idle and once under full load and check that
the readings follow the load; with a frozen BMC use RAPL only (`measurement.host: cpu_dram`). `cpc measure`
warns when all levels show almost the same power.]

#qa[Whole-server power occasionally shows huge readings (e.g. 16462 W).][Some BMCs return sporadic abnormal
DCMI readings. Give the sensor a plausible limit, e.g. `{type: ipmi, max_w: 2000}`: readings above it count
as missing, are not integrated, and show in the health state and `cpc_sensor_errors_total`; `energy_coverage`
of the result drops below 1 accordingly. `max_w` is a generic parameter of all sensors.]

#qa[RAPL in a container reports "no package / dram / psys domain".][Docker masks
`/sys/devices/virtual/powercap` in containers with an empty directory. Mount the host directory elsewhere
(`-v /sys/devices/virtual/powercap:/host/powercap:ro`) and configure `{type: rapl, root: /host/powercap}`.]

#qa[RAPL cannot be read.][`/sys/class/powercap/intel-rapl:*/energy_uj` is readable by root only on most
distributions, and most cloud VMs do not provide RAPL. Run as root on bare metal, or use `ipmi` for
whole-server power.]

#qa[vLLM fails to start or runs out of GPU memory.][Reduce `MAX_MODEL_LEN`, lower
`--gpu-memory-utilization` or choose a smaller model; see `docker compose logs vllm-0`. New GPU architectures
need recent vLLM versions.]

#qa[Models download slowly.][Download them on the host with `huggingface-cli download`, mount the cache and
set `HF_HUB_OFFLINE=1` in the container; or use `HF_ENDPOINT=https://hf-mirror.com` /
`VLLM_USE_MODELSCOPE=True`.]

#qa[Admin interfaces return 401.][The service has `CPC_ADMIN_TOKEN` set. Add
`-H "Authorization: Bearer <token>"`; the controller and `cpc event` read the variable themselves.]

#qa[Are simulation results always the same?][Datasets have fixed versions, and rate scaling and source
assignment use `workload.seed` of the scenario, so the same scenario, datasets and policies give the same
result; changing the seed evaluates the effect of randomness. `run.yaml` of the result records all inputs.]

#qa[A dataset is missing or violates the specification.][Prepare it as @g-data describes and check it
with `cpc data check <dir>`; the error names the field, constraint and value. `cpc check -s <scenario>`
checks all datasets of a scenario at once.]

#qa[Many requests fail or time out in a live experiment.][The arrival rate exceeds what the machine can
handle: lower it with `--rate-scale`. Inference instances that are not ready or a model name mismatch
(`--model` must match the model of the inference instance) are other causes. The distribution of `failed`
requests over `site` and `server` is in `requests.parquet`.]

#qa[The cost of a live experiment is 0, or `signal_coverage` is below 1.][Grid signals were unavailable
while sampling: check whether the signal time under `clock.start` and `--speedup` leaves the grid data
(`missing` of `http://localhost:9000/v1/signals` gives the reason).]
