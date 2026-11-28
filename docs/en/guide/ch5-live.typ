#import "../../template/cpc-doc.typ": *

= Live experiments <g-live>

A live experiment runs a scenario on real time: the services communicate over HTTP, and `cpc live` sends
OpenAI-compatible inference requests at the arrival times of the trace. Live experiments check how
policies behave in a real system and can connect real inference instances and power sensors.

== Components

#figure(
  tbl(
    columns: (9em, 6em, 1fr),
    header: ("Service", "Local port", "Role"),
    [grid-signal service], [9000], [current price, carbon intensity and power limit of every site; demand-response events; authority of the signal clock],
    [site agent], [from 9101], [one per site: manages the servers of the site (on/off, queueing, dispatch), measures power, serves inference],
    [gateway], [8080], [entry of user requests: chooses a site per request (global routing policy) and forwards it, emulating the wide-area latency],
    [controller], [9300], [reads site states and grid signals periodically, calls the capacity policy, sends servers to keep on and power-cap ratios to the sites],
  ),
  caption: [Services of a live experiment],
)

Locally the services use the ports above; distributed deployments list the service addresses in an
endpoints file (@g-endpoints).

== One live experiment: cpc live

```bash
cpc live -s configs/scenario.yaml --duration 600 --rate-scale 0.1 --speedup 60 \
    --out results/live-1
```

`cpc live` starts all services in its own process, waits until they are ready, replays 600 seconds of the
workload while reading site states and grid signals every 10 seconds (`--sample-s`), then stops the services
and writes the result directory. Results have the format of simulations (@g-sim), plus a subdirectory
`logs/` with the request log of the gateway and the decision log of the controller.

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("Option", "Meaning"),
    [`--duration`], [duration (s, required)],
    [`--speedup`], [speed-up of the signal clock; 60 lets a 10-minute experiment cover 10 signal hours],
    [`--routing`, `--capacity`], [global policies instead of the scenario's (services started here only)],
    [`--interval`], [decision period of the capacity policy (s)],
    [`--initial-active`], [servers on at the start in every site (default: all)],
    [`--sample-s`], [sampling period (s, default 10), the duration of a time-series row],
    [`--rate-scale` etc.], [workload transformation, as for `cpc sim`],
    [`--model`], [model name of the requests; with real inference instances it must match the model they serve (default `cpc`)],
    [`--no-stream`], [non-streaming requests (no TTFT)],
    [`-e`, `--endpoints`], [connect to deployed services (see below) instead of starting them here],
    [`--local`], [connect to services running at the local ports (`cpc up`)],
  ),
  caption: [Options of cpc live],
)

Request statuses: HTTP 200 is served, 503 (site queue full or rejected by the routing policy) rejected,
other errors, connection failures and incomplete streams failed. Energy is the increase of the accumulated
energy of each site per sampling interval; cost and carbon use the price and carbon intensity at the start
of each interval.

*Load and machine capacity.* The workload is sent in real time, and too high an arrival rate exceeds what
the machine can handle (many requests time out or fail); `--rate-scale` lowers it. Accelerating the signal
clock does not change the arrival rate; to keep the daily pattern of the workload in step with the
accelerated clock, compress the trace with `--time-scale` and lower the rate with `--rate-scale` accordingly.

== Manual operation: cpc up

To watch and operate the system while it runs, start all services with `cpc up`, which runs until Ctrl+C:

```bash
cpc up -s configs/scenario.yaml --speedup 60
```

In another terminal (with the virtual environment activated) replay the workload and write a result directory:

```bash
cpc live -s configs/scenario.yaml --local --duration 120 --rate-scale 0.1 --out results/manual-1
```

`--local` connects to the services already running at the local ports (started by `cpc up`) instead of
starting another set. Any OpenAI-compatible client can use the gateway as well:

```bash
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
     -H "x-cpc-source: east" \
     -d '{"model": "cpc", "max_tokens": 32,
          "messages": [{"role": "user", "content": "hello"}]}'
```

The header `x-cpc-source` names the source region; the response headers `x-cpc-site` and `x-cpc-node` give
the site and server that handled the request, and `x-cpc-route-reason` the routing reason.

*Watching the system.* Every service has JSON state interfaces to open in a browser:

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("Address", "Content"),
    [`http://localhost:9101/state`], [first site: server states, requests in service and queued, power and accumulated energy],
    [`http://localhost:8080/state`], [gateway: arrival rate per source region, load and signals of the sites, routing policy and weights],
    [`http://localhost:9300/state`], [controller: capacity policy and its last decision],
    [`http://localhost:9000/v1/signals`], [current price, carbon intensity and power limit of every site],
    [`http://localhost:8080/docs`], [interactive interface documentation of the gateway (likewise for the other services)],
  ),
  caption: [State interfaces],
)

*Operations at run time.* Switching policies and injecting demand-response events:

```bash
curl -X PUT http://localhost:8080/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "cost_aware"}'                       # routing policy
curl -X PUT http://localhost:9300/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "grid_aware"}'                       # capacity policy (with routing policy weighted)
curl -X POST http://localhost:9300/admin/step            # capacity decision now
cpc event -s configs/scenario.yaml --local --site gd --cap-kw 8 --duration-min 120
```

The last command limits the facility power of site gd to 8 kW for 120 minutes (signal time). With the
environment variable `CPC_ADMIN_TOKEN` set, admin interfaces need the header `Authorization: Bearer <token>`
(`cpc event` reads the variable itself).

== Endpoints files and distributed deployment <g-endpoints>

Services can start separately, even on different hosts. An endpoints file then gives the address of every service:

```yaml
grid: http://10.0.0.10:9000
gateway: http://10.0.0.10:8080
controller: http://10.0.0.10:9300
sites:
  sh: http://10.0.0.11:9100
  gd: http://10.0.0.12:9100
```

```bash
cpc grid       -s scenario.yaml -e endpoints.yaml                 # grid-signal service first
cpc site       -s scenario.yaml -e endpoints.yaml --site sh       # one per site
cpc gateway    -s scenario.yaml -e endpoints.yaml --log gateway.jsonl
cpc controller -s scenario.yaml -e endpoints.yaml --log controller.jsonl
# replay, sample, write the result
cpc live       -s scenario.yaml -e endpoints.yaml --duration 3600
```

Each service listens on its own address of the endpoints file; `--host` and `--port` override it (e.g.
`0.0.0.0` in containers). All services use the same scenario file; gateway, controller and `cpc live` must
reach the grid-signal service at startup. `cpc live -e` connects to running services, whose global
policies are those the gateway and controller were started with.

== Connecting real servers <g-real>

A real server is an OpenAI-compatible inference instance (such as vLLM) with the GPU it uses. The steps
below, on a Linux host with an NVIDIA GPU, connect a vLLM instance to site lab of
`configs/scenario.local-gpu.yaml`, with power measured by NVML and power on/off by starting and stopping
the container.

*Step 1: environment.* Install the NVIDIA driver (`nvidia-smi` shows the GPU), Docker and the NVIDIA
Container Toolkit (`docker run --rm --gpus all ubuntu nvidia-smi` shows the GPU), and the optional
dependencies of the platform:

```bash
pip install -e ".[nvml,docker]"
cpc sensors --types nvml          # read GPU power to confirm NVML works
```

*Step 2: inference instance.* The command starts a vLLM container serving a small model under the name `cpc`:

```bash
docker run -d --name cpc-vllm-local --gpus all -p 127.0.0.1:8000:8000 --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest --model Qwen/Qwen2.5-0.5B-Instruct --served-model-name cpc \
  --max-model-len 16384 --gpu-memory-utilization 0.6 --max-num-seqs 32
# returns 200 once the model is loaded (the first start downloads and compiles, minutes)
curl http://127.0.0.1:8000/health
```

*Step 3: declare the real server in the scenario.* Site lab of `configs/scenario.local-gpu.yaml`:

```yaml
sites:
  - id: lab
    name: local GPU (real server)
    region: guizhou
    lat: 26.40
    lon: 106.40
    pue: 1.2
    pools:
      - id: gpu
        profile: local                     # server type (capacity estimates and simulation)
        backend: openai                    # real servers
        upstreams:                         # one entry per inference instance
          - {id: lab-0, url: "http://127.0.0.1:8000", gpu_indices: [0],
             container: cpc-vllm-local, max_inflight: 32}
    measurement:                           # power measurement
      sensors: [{type: nvml, gpus: [0]}]
      host: excluded                       # GPU power only
```

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("Field", "Meaning"),
    [`url`], [address of the inference instance; the site agent forwards requests here and checks readiness at `url` + `ready_path` (default `/health`)],
    [`gpu_indices` or `devices`], [GPU indices of the instance (or device names given by the sensors), attributing GPU power to this server; CPU-only instances give neither and their power is the host part (`measurement.host` `server` or `cpu_dram`)],
    [`container`], [container name (optional). With it, power off stops and power on starts the container; without it, power off only stops dispatch],
    [`max_inflight`], [concurrency limit, best equal to the largest batch of the engine (`--max-num-seqs` of vLLM)],
    [`measurement.sensors`], [power sensors (@g-sensors)],
    [`measurement.host`], [how the host part of power is computed: `server` (whole-server reading minus GPU readings; needs a whole-server reading such as `ipmi`), `cpu_dram` (sum of CPU and memory readings), `excluded` (not counted)],
  ),
  caption: [Fields of real servers],
)

*Step 4: run the experiment.*

```bash
cpc live -s configs/scenario.local-gpu.yaml --routing least_loaded --duration 60 \
    --model cpc \
    --out results/live-gpu
```

`--model cpc` makes the model name of the requests match `--served-model-name` of vLLM; `least_loaded`
spreads requests over the less loaded site (lab or the emulated site emu). The power of site lab in the
result comes from the measured GPU readings; while it runs, `http://localhost:9101/state` shows
`power_source` `sensors` for the server.

*Power on/off.* Instructions to the site show the switching (during experiments the capacity policy also
sends them with the load):

```bash
curl -X POST localhost:9101/admin/capacity -H 'Content-Type: application/json' \
     -d '{"active_nodes": 0}'
docker inspect -f '{{.State.Status}}' cpc-vllm-local     # exited: the container stopped
curl -X POST localhost:9101/admin/capacity -H 'Content-Type: application/json' \
     -d '{"active_nodes": 1}'
```

After power-on the server is booting and turns on, accepting requests, once its health check passes. With
the container stopped the GPU still draws idle power, and the measurement shows it.

*Power capping.* A site agent started with `cpc site --enable-power-cap` sets the GPU power limit through
NVML from the power-cap ratio of its instructions (root needed; some cloud servers do not allow it);
otherwise it only records the target ratio.

*Startup checks.* At startup the site agent checks that the sensors work, the declared GPUs have
readings and the configured containers exist, and exits with the reason otherwise. When a sensor reading
fails at run time, the power of the server is missing (never replaced by an estimate) and reported in state
and metrics, while requests are served as usual.

== Cloud servers <g-cloud>

A few rented cloud GPU servers become the real servers of the sites, and the scenario supplies the
geography: the servers are usually in one region, and the grid regions of the sites, the grid signals and
the wide-area latency come from the scenario file. The example scenario is
`deploy/cloud/scenario.cloud.example.yaml`.

=== Planning

Recommended minimum:

#figure(
  tbl(
    columns: (8em, 6em, 1fr),
    header: ("Role", "Number", "Recommended"),
    [GPU servers], [2-3], [one GPU suffices; Ubuntu 22.04/24.04; images with preinstalled GPU drivers save the driver installation],
    [control node], [1], [ordinary instance with 4 cores and 8 GB; runs grid-signal service, gateway, controller, Prometheus, Grafana and emulated sites; holds the datasets],
  ),
  caption: [Cloud planning],
)

- put all servers in one region and VPC and let them talk over private addresses;
- every GPU server is the real pool (`backend: openai`) of one site; sites with emulated servers only can run
  on the control node to complete the geography;
- switch off or release pay-as-you-go instances when not in use.

=== Network and security

#figure(
  tbl(
    columns: (7em, 9em, 1fr),
    header: ("Port", "Service", "Open to"),
    [22], [SSH], [your address],
    [9100], [site agent], [VPC only (reached by the control node)],
    [9000 / 8080 / 9300], [grid / gateway / controller], [VPC only; for external load tests open 8080 to your address temporarily],
    [8000], [vLLM], [not exposed (local site agent only)],
    [9090 / 3000], [Prometheus / Grafana], [bound to 127.0.0.1, reached through SSH tunnels],
  ),
  caption: [Ports and security groups],
)

All servers and the control stack must share the admin token `CPC_ADMIN_TOKEN`. Generating a token and
viewing the monitoring through an SSH tunnel:

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
# then open http://localhost:3000
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 <control node>
```

=== Driver, Docker and NVIDIA Container Toolkit

On every GPU server (skip step 1 with preinstalled drivers):

```bash
# 1. NVIDIA driver (Ubuntu)
sudo ubuntu-drivers install && sudo reboot
nvidia-smi                                   # shows the GPU

# 2. Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# 3. NVIDIA Container Toolkit (after adding the repository as NVIDIA documents)
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi   # the container shows the GPU
```

The repository setup of the NVIDIA Container Toolkit changes between versions; follow NVIDIA's installation guide.

=== Platform image and code

```bash
git clone https://github.com/X1ngChui/cpc-platform.git && cd cpc-platform
# or xingchui/cpc-platform:0.4.2, or docker build yourself
docker pull ghcr.io/x1ngchui/cpc-platform:0.4.2
```

=== Control stack (control node, first)

Scenario and endpoints file are maintained on the control node only: site agents fetch them from its
grid-signal service at startup (`GET /v1/config`, admin token), so the files are not copied to site servers.

```bash
cd deploy/cloud/control-stack
cat > .env <<'EOF'
CPC_ADMIN_TOKEN=<your token, the same on all servers>
GRAFANA_PASSWORD=<your password>
CPC_IMAGE=ghcr.io/x1ngchui/cpc-platform:0.4.2
EOF
cp ../scenario.cloud.example.yaml scenario.yaml      # adapt to the experiment
cp ../endpoints.cloud.example.yaml endpoints.yaml    # fill in the private addresses
cp ../prometheus.cloud.example.yml prometheus.yml    # fill in the server addresses
docker compose up -d
```

The grid-signal service of the control stack mounts the dataset directory (`CPC_DATA_DIR`) of the control
node, which `cpc live` on the control node reads as well; prepare the datasets of the scenario there first
(@g-data). After changing the scenario, restart the control stack (`docker compose up -d --force-recreate`)
and the site agents (`docker compose restart site-agent`).

=== Site stack (every GPU server)

```bash
cd deploy/cloud/site-stack
# set SITE_ID, CONTROL_URL, CPC_ADMIN_TOKEN, MODEL, CPC_IMAGE
cp .env.example .env
docker compose up -d                 # starts vllm-0 and site-agent
docker compose logs -f site-agent    # wait for "power sensors: ['nvml']" and vLLM
```

`CONTROL_URL` is the address of the grid-signal service of the control node (e.g. `http://10.0.0.10:9000`).
The site agent waits for the control node within its startup deadline (120 s by default) and exits with a
non-zero status when the token differs or the scenario is invalid; `docker compose logs site-agent` shows why.

The power measurement of a site is declared in `measurement` of the scenario (the example reads the GPUs
with NVML, and `host: excluded` counts GPU power only). At startup the site agent checks that the sensors
work, the declared GPUs have readings and the configured containers exist, and the container exits with a
non-zero status otherwise. Servers with a local BMC may add the `ipmi` sensor with `host: server`. On bare
metal, the site-agent container needs the device `/dev/ipmi0` for the BMC, and for RAPL the host directory
`/sys/devices/virtual/powercap` mounted at `/host/powercap` (Docker masks it in containers) with the sensor
`{type: rapl, root: /host/powercap}`; the `site-agent` service of `docker-compose.yml` has the corresponding
commented lines.

The site stack consists of `vllm-0` (one GPU per inference instance; dual-GPU servers enable `vllm-1` with
`--profile gpu2`), `site-agent` (queueing and dispatch, power collection, starting and stopping vLLM
through the mounted Docker socket) and the optional `node-exporter` (`--profile host`, port 9101).

*Power capping.* Power-cap ratios from the capacity policy are only recorded by default. To set GPU power
limits through NVML, start with the override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.powercap.yml up -d
```

The site agent then runs with `--enable-power-cap` (the container has `SYS_ADMIN`) and sets the GPU power
limit to ratio × maximum limit; failures go to `last_error` of the server. GPU power limits have a lower
bound (100-165 W on the A30, for example), so `cap_min_ratio` of the server type is best the ratio of the
minimum and maximum limits (about 0.61 for the A30); smaller ratios use the minimum and log a warning. The
effect of capping on throughput depends on model and load and should be measured: for instance,
Qwen2.5-1.5B-Instruct on an A30 capped at 0.7 drew 116 W instead of 162 W at concurrency 32 without losing
throughput.

`MODEL` in `.env` should fit into GPU memory; in mainland China models download through
`HF_ENDPOINT=https://hf-mirror.com`, or from ModelScope with `VLLM_USE_MODELSCOPE=True`. Models can also be
downloaded on the host first (`huggingface-cli download <model>`) and loaded offline from the mounted cache.
`VLLM_ARGS` passes extra arguments to vLLM. GPUs without bfloat16 (V100, T4) need `VLLM_ARGS=--dtype half`, and
current vLLM releases are no longer built for the V100 (compute capability 7.0): there `VLLM_TAG` names a
release that still supports it, such as `v0.8.5.post1`, which runs on its V0 engine.

Checks:

```bash
# power_source of real servers is sensors
curl -s localhost:9100/state | python3 -m json.tool | head -30
curl -s localhost:9100/readyz                                         # readiness
docker compose exec site-agent cpc sensors --types nvml               # NVML readings
# the vLLM model name is cpc
curl -s localhost:8000/v1/models
```

=== Servers without GPUs (CPU inference)

Bare-metal servers without GPUs run llama.cpp on their CPUs as the real server of a site; RAPL (CPU and
memory) and the local BMC (whole server) measure their power. The site stack is `docker-compose.cpu.yml`:

```bash
cd deploy/cloud/site-stack
cp .env.example .env     # set SITE_ID, CONTROL_URL, CPC_ADMIN_TOKEN, CPC_IMAGE
M=https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main
mkdir -p /data/models && curl -L -o /data/models/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  $M/qwen2.5-0.5b-instruct-q4_k_m.gguf
docker compose -f docker-compose.cpu.yml up -d
```

Pool and measurement of the site in the scenario (the upstream declares no accelerators; server power is the
whole-server reading):

```yaml
pools:
  - id: cpu
    profile: cpu-llm                     # generated from cpc measure (@g-calibrate)
    backend: openai
    upstreams:
      - {id: sh-llm0, url: "http://llm-0:8080", container: cpc-llm-0, max_inflight: 4}
measurement:
  sensors: [{type: ipmi, max_w: 2000}, {type: rapl, root: /host/powercap}]
  host: server
```

`max_inflight` equals the slots `PARALLEL` of llama.cpp (default 4). llama.cpp splits the context `CONTEXT`
(default 65536) evenly over its slots; each slot must hold the longest request of the workload, or those
requests fail with 400.

=== Checking the deployment

With all sites started, run the smoke test on the control node (with the Python environment of @g-setup):

```bash
python3 ../../../scripts/smoke_test.py --external -e endpoints.yaml -s scenario.yaml
```

=== Running experiments and collecting data

1. Signal clock: for final experiments set `clock.speedup` to 1 and `clock.start` to the date and time to emulate;
2. run the experiment on the control node: `cpc live -s scenario.yaml -e endpoints.yaml --duration 7200
   --out results/cloud-1`; the result directory has the format of simulations;
3. other data: request and decision logs are in `control-stack/logs/`; metrics are in Prometheus (export
   through its HTTP API:
   `curl 'localhost:9090/api/v1/query_range?query=site:facility_power_watts:sum&start=...&end=...&step=15'`);
4. afterwards stop the site and control stacks (`docker compose down`) and switch off or release instances.

*Monitoring.* Grafana in the control stack has the dashboard "cpc platform": facility power, cost rate,
carbon rate, request rate and SLO attainment, and per site power, price, online servers, carbon intensity,
request rate, TTFT P99, demand-response headroom, energy per token, queue length, GPU power, upstream
availability and sensor health. The Prometheus page answers queries on the standard metrics, e.g.:

```text
site:facility_power_watts:sum                       facility power per site
sum(site:energy_cost_per_hour)                      global cost rate (currency of the price per hour)
increase(cpc_node_energy_joules_total[10m]) / 3.6e6 IT energy per server in the last 10 minutes (kWh)
histogram_quantile(0.99, sum by (le, site) (rate(cpc_gateway_ttft_seconds_bucket[5m])))
```

=== Latency between servers

Servers in one region have little latency between them, so the gateway injects the wide-area latency
(`network.emulate_wan: true`). When the servers really are in different regions, set `emulate_wan` to
`false` and let the network produce the latency.

== Server types from measurements of real servers <g-calibrate>

To make simulated servers behave like real ones, measure power and inter-token latency of the real server at
several concurrency levels, then generate a server type from the measurements.

*Step 1: preparation.* Site agent and inference instance run, and the measured server is the only server of
the site that is on: the site dispatches the measurement requests over all servers that are on, and
`cpc measure` stops with an error otherwise. Stop the capacity controller (`docker compose stop controller`)
so that it does not switch servers meanwhile, and on sites with several servers switch the others off
(`POST /admin/capacity` with `{"active_nodes": 1}`; `/state` shows which server stays on). The site should
carry no other load during the measurement.

*Step 2: measurement.* On a machine that reaches the site agent:

```bash
export CPC_ADMIN_TOKEN=<the token of the site>      # only for the boot measurement (--boot)
cpc measure --site http://<site address>:9100 --device A10 --model-name Qwen2.5-7B-Instruct \
    --boot --out measurements/a10-qwen7b
```

`cpc measure` keeps a fixed number of streaming requests (256 input and 256 output tokens) at concurrency
0, 1, 2, 4, ... up to the server's limit; after 15 seconds of warm-up per level it measures for 60 seconds:
every second it reads the measured power of the server from the site agent, and the streams give
inter-token latency, TTFT and throughput. Level 0 measures power only, the online idle power. `--boot`
switches the server off through the admin interface of the site, measures the power while off (inference
instance stopped, machine running), then switches all servers on, times how long the measured one takes to
serve again and restores the number of servers that were on. Levels are printed as they finish:

```text
  concurrency 0: warm-up 0 s, measurement 60 s ...
    power 67.6 W (85% complete)  in service 0.00  inter-token -  throughput 0.0/s  requests 0  errors 0
  concurrency 1: warm-up 15 s, measurement 60 s ...
    power 202.9 W (100% complete)  in service 1.00  inter-token 29.4 ms  throughput 29.9/s  requests 14  errors 0
  ...
```

The result is a power_profile dataset with one row per level (`concurrency` is the measured mean of
requests in service); `cpc.measurement` of the descriptor records the conditions (site, server, power scope,
sensors, request lengths) and the online idle power, power while off and boot time. The power scope follows
`measurement` of the site: server power with `host: server` or `cpu_dram`, accelerators only with
`host: excluded`. A completeness below 100 % means some samples had no power (e.g. readings above `max_w`)
and are left out of the mean. When all levels show almost the same power, `cpc measure` warns and records
the warning in the dataset: the sensor may not follow the load (some BMCs do not update their reading), and
RAPL is the better choice. `--concurrency 0,1,2,3,4` sets the levels, `--duration` and `--warmup` the time
per level, `--input-tokens` and `--output-tokens` the request lengths.

*Step 3: generate the server type.*

```bash
cpc profile measurements/a10-qwen7b --name a10-qwen7b
```

Slots, online idle power, power while off and boot time come from the measurement, prefill throughput is
estimated from the TTFT at concurrency 1, and comments in the output name the sources; `--slots`,
`--idle-w`, `--off-w`, `--boot-s` and `--prefill-tokens-per-s` give them explicitly. When the power covers
accelerators only but servers should include the host part, give the host power with `--host-w`. Put the
output into `node_profiles` of the scenario, and the simulated servers of this type follow the measured tables.

*Step 4: compare live experiment and simulation.* Run a live experiment with the measured server type, then
a simulation of the same workload window, and compare them with `cpc compare`:

```bash
cpc live -s scenario.yaml -e endpoints.yaml --duration 900 --out results/live-1
cpc sim -s scenario.yaml --hours 0.25 --out results/sim-1
cpc compare results/live-1 results/sim-1
```

Part of the output for a 15-minute experiment with two CPU servers (llama.cpp) as real sites and one
emulated site:

```text
  scope   metric                       live-1       sim-1   ratio
  total   served                          233         233    1.00
          SLO attainment                84.1%       83.7%    0.99
          IT energy (kWh)               0.137       0.131    0.96
  sh      served                           60          63    1.05
          TTFT P50 (s)                  3.494       3.847    1.10
          IT energy (kWh)               0.041       0.042    1.00
```

Ratios far from 1 point to differences between the server type and the real server, for instance a
different idle power of another server of the same model or an underestimated prefill throughput; measure
again or adjust the parameters of the server type accordingly.
