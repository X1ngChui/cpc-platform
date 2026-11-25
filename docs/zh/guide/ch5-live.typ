#import "../../template/cpc-doc.typ": *

= 实验 <g-live>

实验以真实时间运行场景：各个服务通过 HTTP 通信，`cpc live` 按 trace 中的到达时间发送 OpenAI 兼容的推理请求。
实验用于检验策略在真实系统中的行为，并可以接入真实的推理实例与功率传感器。

== 组成

#figure(
  tbl(
    columns: (9em, 6em, 1fr),
    header: ("服务", "本机端口", "作用"),
    [电网信号服务], [9000], [提供各站点当前的电价、碳强度与功率上限；接受需求响应事件；是信号时钟的权威来源],
    [站点代理], [9101 起], [每个站点一个：管理站点的服务器（开关、排队、派发）、测量功率，提供推理接口],
    [网关], [8080], [用户请求的入口：为每个请求选择站点（全局路由策略）并转发，模拟跨地域的网络时延],
    [控制器], [9300], [周期性地读取各站点状态与电网信号，调用容量策略，向站点下发开启服务器数与功率封顶比例],
  ),
  caption: [实验系统的服务],
)

本机运行时各服务使用上表中的端口；分布部署时，服务地址写在地址文件中（@g-endpoints）。

== 运行一次实验：cpc live

```bash
cpc live -s configs/scenario.yaml --duration 600 --rate-scale 0.1 --speedup 60 \
    --out results/live-1
```

`cpc live` 在本进程中启动全部服务，等待它们就绪，回放 600 秒负载，同时每隔 10 秒（`--sample-s`）读取各站点的状态与电网信号，
结束后停止服务并写出结果目录。结果目录的格式与仿真相同（@g-sim），另有 `logs/` 子目录保存网关的请求日志与控制器的决策日志。

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("参数", "含义"),
    [`--duration`], [实验时长（秒，必填）],
    [`--speedup`], [信号时钟加速倍数；如 60 表示 10 分钟的实验覆盖信号时间 10 小时],
    [`--routing`、`--capacity`], [覆盖场景中的全局策略（只用于本进程启动的服务）],
    [`--interval`], [容量策略的决策周期（秒）],
    [`--initial-active`], [各站点开始时开启的服务器数（缺省全部开启）],
    [`--sample-s`], [状态采样周期（秒，缺省 10），即结果时间序列中每行的时长],
    [`--rate-scale` 等], [负载变换参数，与 `cpc sim` 相同],
    [`--model`], [请求中的模型名；接入真实推理实例时须与其服务的模型名一致（缺省 `cpc`）],
    [`--no-stream`], [使用非流式请求（不能测量首 token 时延）],
    [`-e`、`--endpoints`], [连接已部署的服务（见下文），而不在本进程中启动],
    [`--local`], [连接本机端口上已运行的服务（`cpc up`）],
  ),
  caption: [cpc live 的参数],
)

结果中的请求状态：HTTP 200 为 served，503（站点队列已满或路由策略拒绝）为 rejected，其他错误、连接失败与不完整的流式响应为 failed。
电量取各站点累计能耗在采样区间内的增量；电费与碳排放按每个采样区间开始时的电价与碳强度计算。

*负载与本机能力。* 负载按真实时间发送，到达率过高会超出本机的处理能力（表现为大量请求超时或失败），可用 `--rate-scale`
降低到达率。信号时钟加速不改变负载的到达速度；需要负载的日内变化与加速后的信号时钟同步时，用 `--time-scale`
压缩 trace 的时间，并用 `--rate-scale` 相应降低到达率。

== 手动操作：cpc up

需要在运行期间手动观察与操作时，用 `cpc up` 启动全部服务，它会一直运行到按 Ctrl+C：

```bash
cpc up -s configs/scenario.yaml --speedup 60
```

另开一个终端（同样先激活虚拟环境）回放负载并写出结果目录：

```bash
cpc live -s configs/scenario.yaml --local --duration 120 --rate-scale 0.1 --out results/manual-1
```

`--local` 表示连接本机端口上已运行的服务（`cpc up` 启动的服务），而不在本进程中另行启动。也可以用任何 OpenAI 兼容的客户端
访问网关：

```bash
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
     -H "x-cpc-source: east" \
     -d '{"model": "cpc", "max_tokens": 32,
          "messages": [{"role": "user", "content": "hello"}]}'
```

请求头 `x-cpc-source` 指明来源区域；响应头 `x-cpc-site`、`x-cpc-node` 给出处理请求的站点与服务器，`x-cpc-route-reason`
给出路由原因。

*观察运行状态。* 各服务提供 JSON 格式的状态接口，可以在浏览器中打开：

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("地址", "内容"),
    [`http://localhost:9101/state`], [第一个站点的状态：各服务器的状态、正在处理与排队的请求、功率与累计能耗],
    [`http://localhost:8080/state`], [网关：各来源区域的到达率、各站点的负载与信号、当前路由策略与权重],
    [`http://localhost:9300/state`], [控制器：当前容量策略与最近一次决策],
    [`http://localhost:9000/v1/signals`], [各站点当前的电价、碳强度与功率上限],
    [`http://localhost:8080/docs`], [网关的交互式接口文档（其他服务同理）],
  ),
  caption: [状态接口],
)

*运行时操作。* 切换策略、注入需求响应事件：

```bash
curl -X PUT http://localhost:8080/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "cost_aware"}'                       # 切换路由策略
curl -X PUT http://localhost:9300/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "grid_aware"}'                       # 切换容量策略（配合路由策略 weighted）
curl -X POST http://localhost:9300/admin/step            # 立即执行一次容量决策
cpc event -s configs/scenario.yaml --local --site gd --cap-kw 8 --duration-min 120
```

最后一条命令要求站点 gd 的设施功率在 120 分钟（信号时间）内不超过 8 kW。设置了环境变量 `CPC_ADMIN_TOKEN` 时，
管理接口须带请求头 `Authorization: Bearer <令牌>`（`cpc event` 自动读取该环境变量）。

== 地址文件与分布部署 <g-endpoints>

各服务可以分别启动，甚至位于不同的主机上。此时用地址文件说明每个服务的地址：

```yaml
grid: http://10.0.0.10:9000
gateway: http://10.0.0.10:8080
controller: http://10.0.0.10:9300
sites:
  sh: http://10.0.0.11:9100
  gd: http://10.0.0.12:9100
```

```bash
cpc grid       -s scenario.yaml -e endpoints.yaml                 # 先启动电网信号服务
cpc site       -s scenario.yaml -e endpoints.yaml --site sh       # 每个站点一个
cpc gateway    -s scenario.yaml -e endpoints.yaml --log gateway.jsonl
cpc controller -s scenario.yaml -e endpoints.yaml --log controller.jsonl
# 回放负载、采样、写出结果
cpc live       -s scenario.yaml -e endpoints.yaml --duration 3600
```

每个服务按地址文件中自己的地址监听，`--host`、`--port` 可以覆盖（例如在容器中监听 `0.0.0.0`）。各服务使用同一份场景文件；
网关、控制器与 `cpc live` 启动时须能访问电网信号服务。`cpc live -e` 连接已运行的服务，此时全局策略由网关、控制器启动时的参数决定。

== 接入真实服务器 <g-real>

真实服务器是一个 OpenAI 兼容的推理实例（例如 vLLM）及其占用的 GPU。以下步骤在一台装有 NVIDIA GPU 的 Linux 主机上，
把一个 vLLM 实例接入场景 `configs/scenario.local-gpu.yaml` 中的站点 lab，功率由 NVML 测量，开关机通过启停容器实现。

*第一步：准备环境。* 安装 NVIDIA 驱动（`nvidia-smi` 能看到 GPU）、Docker 与 NVIDIA Container Toolkit
（`docker run --rm --gpus all ubuntu nvidia-smi` 能看到 GPU），并安装平台的可选依赖：

```bash
pip install -e ".[nvml,docker]"
cpc sensors --types nvml          # 读取 GPU 功率，确认 NVML 可用
```

*第二步：启动推理实例。* 以下命令启动一个 vLLM 容器，服务一个小模型，对外的模型名为 `cpc`：

```bash
docker run -d --name cpc-vllm-local --gpus all -p 127.0.0.1:8000:8000 --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest --model Qwen/Qwen2.5-0.5B-Instruct --served-model-name cpc \
  --max-model-len 16384 --gpu-memory-utilization 0.6 --max-num-seqs 32
# 模型加载完成后返回 200（首次需要下载模型与编译，可能需要数分钟）
curl http://127.0.0.1:8000/health
```

*第三步：在场景中声明真实服务器。* `configs/scenario.local-gpu.yaml` 中的站点 lab：

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
        profile: local                     # 服务器类型（用于容量估计与仿真）
        backend: openai                    # 真实服务器
        upstreams:                         # 每一项是一个推理实例
          - {id: lab-0, url: "http://127.0.0.1:8000", gpu_indices: [0],
             container: cpc-vllm-local, max_inflight: 32}
    measurement:                           # 功率测量
      sensors: [{type: nvml, gpus: [0]}]
      host: excluded                       # 只计 GPU 功率
```

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("字段", "含义"),
    [`url`], [推理实例的地址；站点代理把请求转发到这里，并以 `url` + `ready_path`（缺省 `/health`）检查是否就绪],
    [`gpu_indices` 或 `devices`], [实例占用的 GPU 序号（或传感器给出的设备名），用于把 GPU 功率归属到这台服务器；
      只用 CPU 的推理实例不填，服务器功率为主机部分（`measurement.host` 设为 `server` 或 `cpu_dram`）],
    [`container`], [容器名（可选）。给出时，关机即停止容器、开机即启动容器；未给出时关机只停止派发请求],
    [`max_inflight`], [并发上限，宜与推理引擎的最大批大小（vLLM 的 `--max-num-seqs`）一致],
    [`measurement.sensors`], [功率传感器列表（@g-sensors）],
    [`measurement.host`], [主机部分功率的计算方式：`server`（整机读数减 GPU 读数，需要 `ipmi` 等整机读数）、`cpu_dram`（CPU 与内存读数之和）、`excluded`（不计主机部分）],
  ),
  caption: [真实服务器的字段],
)

*第四步：运行实验。*

```bash
cpc live -s configs/scenario.local-gpu.yaml --routing least_loaded --duration 60 \
    --model cpc \
    --out results/live-gpu
```

`--model cpc` 使请求中的模型名与 vLLM 的 `--served-model-name` 一致；`least_loaded` 把请求分给负载较低的站点（lab 或仿真站点 emu）。
结束后，结果中站点 lab 的功率来自 GPU 的实测读数；运行期间访问 `http://localhost:9101/state` 可以看到服务器的
`power_source` 为 `sensors`。

*开关机。* 向站点下发指令即可观察开关机过程（实验运行期间，容量策略也会按负载自动下发）：

```bash
curl -X POST localhost:9101/admin/capacity -H 'Content-Type: application/json' \
     -d '{"active_nodes": 0}'
docker inspect -f '{{.State.Status}}' cpc-vllm-local     # exited：容器已停止
curl -X POST localhost:9101/admin/capacity -H 'Content-Type: application/json' \
     -d '{"active_nodes": 1}'
```

开机后服务器处于 booting 状态，就绪检查通过后转为 on 并开始接收请求。停止容器后 GPU 仍有空载功耗，测得的功率如实反映这一点。

*功率封顶。* 以 `cpc site --enable-power-cap` 启动站点代理时，指令中的功率封顶比例通过 NVML 设置 GPU 的功率上限（需要 root 权限；
部分云服务器不允许修改）；未开启时只记录目标比例。

*启动检查。* 站点代理启动时检查传感器可用、声明的 GPU 有读数、配置的容器存在，任一不满足即退出并给出原因。
运行中传感器读取失败时，该服务器的功率记为缺失（不以估计值替代），在状态与指标中报告，请求服务不受影响。

== 云服务器 <g-cloud>

租用少量云 GPU 服务器作为各站点的真实服务器，跨地域部分由场景给定：服务器通常位于同一地域，
站点所在的电网区域、电网信号与广域网时延由场景文件给出。示例场景为 `deploy/cloud/scenario.cloud.example.yaml`。

=== 规划

推荐的最小配置：

#figure(
  tbl(
    columns: (8em, 6em, 1fr),
    header: ("角色", "数量", "建议规格"),
    [GPU 服务器], [2–3 台], [单卡即可；Ubuntu 22.04/24.04；云厂商的"GPU 驱动预装"镜像可省去安装驱动的步骤],
    [控制节点], [1 台], [4 核 8 GB 的普通云主机；运行电网信号服务、网关、控制器、Prometheus、Grafana 与仿真站点；存放数据集],
  ),
  caption: [云服务器规划],
)

- 所有服务器放在同一地域、同一 VPC，使用内网 IP 互访；
- 每台 GPU 服务器作为一个站点的真实服务器池（`backend: openai`），还可以在控制节点上运行只有仿真服务器的站点补足地域；
- 按需计费的实例在不用时及时关机或释放，避免产生费用。

=== 网络与安全

#figure(
  tbl(
    columns: (7em, 9em, 1fr),
    header: ("端口", "服务", "开放范围"),
    [22], [SSH], [你的 IP],
    [9100], [站点代理], [仅 VPC 内网（控制节点访问）],
    [9000 / 8080 / 9300], [电网信号 / 网关 / 控制器], [仅 VPC 内网；网关若需从外部压测，可临时对你的 IP 开放 8080],
    [8000], [vLLM], [不对外开放（仅本机站点代理访问）],
    [9090 / 3000], [Prometheus / Grafana], [只绑定 127.0.0.1，通过 SSH 隧道访问],
  ),
  caption: [端口与安全组],
)

所有服务器与控制栈必须设置相同的管理接口令牌 `CPC_ADMIN_TOKEN`。生成令牌与通过 SSH 隧道查看监控：

```bash
python3 -c "import secrets; print(secrets.token_hex(16))"
# 然后访问 http://localhost:3000
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 <控制节点>
```

=== 安装驱动、Docker 与 NVIDIA Container Toolkit

在每台 GPU 服务器上（若镜像已预装驱动，跳过第 1 步）：

```bash
# 1. NVIDIA 驱动（Ubuntu）
sudo ubuntu-drivers install && sudo reboot
nvidia-smi                                   # 能看到 GPU 即可

# 2. Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker

# 3. NVIDIA Container Toolkit（按 NVIDIA 官方文档添加软件源后安装）
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi   # 容器内能看到 GPU 即可
```

NVIDIA Container Toolkit 的软件源配置命令随版本变化，请以 NVIDIA 官方安装文档为准。

=== 获取平台镜像与代码

```bash
git clone https://github.com/X1ngChui/cpc-platform.git && cd cpc-platform
# 或 xingchui/cpc-platform:0.4.0，或自行 docker build
docker pull ghcr.io/x1ngchui/cpc-platform:0.4.0
```

=== 控制栈（控制节点，先启动）

场景文件与地址文件只在控制节点上维护：各站点代理启动时从控制节点的电网信号服务取得（`GET /v1/config`，需要管理令牌），
不需要在站点服务器上复制这两个文件。

```bash
cd deploy/cloud/control-stack
cat > .env <<'EOF'
CPC_ADMIN_TOKEN=<自定，各服务器相同>
GRAFANA_PASSWORD=<自定>
CPC_IMAGE=ghcr.io/x1ngchui/cpc-platform:0.4.0
EOF
cp ../scenario.cloud.example.yaml scenario.yaml      # 按实验修改
cp ../endpoints.cloud.example.yaml endpoints.yaml    # 填入各服务器内网 IP
cp ../prometheus.cloud.example.yml prometheus.yml    # 填入各服务器 IP
docker compose up -d
```

控制栈的电网信号服务挂载控制节点的数据集目录（`CPC_DATA_DIR`），在控制节点上运行的 `cpc live` 也读取该目录，
须先在控制节点准备场景所需的数据集（@g-data）。
修改场景后，重启控制栈（`docker compose up -d --force-recreate`）与各站点代理（`docker compose restart site-agent`）。

=== 站点栈（每台 GPU 服务器）

```bash
cd deploy/cloud/site-stack
# 修改 SITE_ID、CONTROL_URL、CPC_ADMIN_TOKEN、MODEL、CPC_IMAGE
cp .env.example .env
docker compose up -d                 # 启动 vllm-0 与 site-agent
docker compose logs -f site-agent    # 等待"power sensors: ['nvml']"与 vLLM 就绪
```

`CONTROL_URL` 为控制节点电网信号服务的地址（如 `http://10.0.0.10:9000`）。站点代理在启动期限（缺省 120 秒）内等待控制节点可达，
令牌不一致或场景无效时以非零状态退出，原因见 `docker compose logs site-agent`。

站点的功率测量在场景文件的 `measurement` 中声明（示例为 NVML 读取 GPU，`host: excluded` 表示服务器 IT 功率只含 GPU）。
站点代理启动时检查传感器可用、服务器声明的 GPU 有读数、配置的容器存在；任一不满足时容器以非零状态退出。
服务器有本机 BMC 时，可加入 `ipmi` 传感器并改为 `host: server`。裸金属服务器上，站点代理容器读取本机 BMC 需要设备
`/dev/ipmi0`，读取 RAPL 需要把宿主机的 `/sys/devices/virtual/powercap` 挂载到 `/host/powercap`（Docker 默认屏蔽容器内的该目录），
并在传感器配置中写 `{type: rapl, root: /host/powercap}`；`docker-compose.yml` 的 `site-agent` 中有相应的注释行。

站点栈包含：`vllm-0`（每个推理实例占一张 GPU；双卡服务器可启用 `--profile gpu2` 的 `vllm-1`）、`site-agent`
（排队与派发、功率采集、通过挂载的 Docker 套接字启停 vLLM 容器）与可选的 `node-exporter`（`--profile host`，监听 9101）。

*功率封顶。* 容量策略下发的功率封顶比例缺省只记录、不作用于硬件。需要经 NVML 设置 GPU 功率上限时，叠加补充文件启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.powercap.yml up -d
```

站点代理以 `--enable-power-cap` 启动（容器具有 `SYS_ADMIN` 权限），把封顶比例换算为功率上限（比例 × 最大功率上限）设置到 GPU；
设置失败时原因记入该服务器的 `last_error`。GPU 的功率上限有下限（如 A30 为 100–165 W），服务器类型的 `cap_min_ratio`
宜取最小上限与最大上限之比（A30 约 0.61），低于下限的比例按下限设置并在日志中警告。封顶对吞吐的影响与模型和负载有关，
宜实测：例如在 A30 上以 0.7 封顶运行 Qwen2.5-1.5B-Instruct 时，并发 32 的功率由 162 W 降至 116 W，吞吐没有下降。

`.env` 中的 `MODEL` 应选择能放进显存的模型；国内服务器下载模型可设置 `HF_ENDPOINT=https://hf-mirror.com`，或设置
`VLLM_USE_MODELSCOPE=True` 从 ModelScope 下载。也可以先在宿主机下载好模型（`huggingface-cli download <模型名>`），
容器挂载缓存目录后离线加载。

检查：

```bash
# 真实服务器的 power_source 为 sensors
curl -s localhost:9100/state | python3 -m json.tool | head -30
curl -s localhost:9100/readyz                                         # 就绪状态
docker compose exec site-agent cpc sensors --types nvml               # 读取 NVML 读数
# vLLM 模型名应为 cpc
curl -s localhost:8000/v1/models
```

=== 没有 GPU 的服务器（CPU 推理）

没有 GPU 的裸金属服务器可以用 llama.cpp 在 CPU 上推理，作为一个站点的真实服务器；功率由 RAPL（CPU 与内存）与本机 BMC
（整机）测量。站点栈为 `docker-compose.cpu.yml`：

```bash
cd deploy/cloud/site-stack
cp .env.example .env     # 修改 SITE_ID、CONTROL_URL、CPC_ADMIN_TOKEN、CPC_IMAGE
M=https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main
mkdir -p /data/models && curl -L -o /data/models/qwen2.5-0.5b-instruct-q4_k_m.gguf \
  $M/qwen2.5-0.5b-instruct-q4_k_m.gguf
docker compose -f docker-compose.cpu.yml up -d
```

场景中该站点的服务器池与测量（上游不声明加速卡，服务器功率为整机读数）：

```yaml
pools:
  - id: cpu
    profile: cpu-llm                     # 由 cpc measure 的测量生成（@g-calibrate）
    backend: openai
    upstreams:
      - {id: sh-llm0, url: "http://llm-0:8080", container: cpc-llm-0, max_inflight: 4}
measurement:
  sensors: [{type: ipmi, max_w: 2000}, {type: rapl, root: /host/powercap}]
  host: server
```

`max_inflight` 与 llama.cpp 的并发槽数 `PARALLEL`（缺省 4）一致。llama.cpp 把上下文长度 `CONTEXT`（缺省 65536）平均分给各并发槽，
每槽的上下文须容纳负载中最长的请求，否则这些请求以 400 失败。

=== 检查部署

全部站点启动后，在控制节点上运行冒烟测试（需要按 @g-setup 安装的 Python 环境）：

```bash
python3 ../../../scripts/smoke_test.py --external -e endpoints.yaml -s scenario.yaml
```

=== 运行实验与收集数据

1. 确认信号时钟：正式实验把场景中的 `clock.speedup` 设为 1，`clock.start` 设为希望模拟的日期时刻；
2. 在控制节点运行实验：`cpc live -s scenario.yaml -e endpoints.yaml --duration 7200 --out results/cloud-1`，
   结果目录的格式与仿真相同；
3. 其他数据：网关请求日志与控制器决策日志在 `control-stack/logs/`；指标在 Prometheus 中（可用 HTTP API 导出：
   `curl 'localhost:9090/api/v1/query_range?query=site:facility_power_watts:sum&start=...&end=...&step=15'`）；
4. 实验结束后停止站点栈与控制栈（`docker compose down`），按需关机或释放实例。

*监控。* 控制栈中的 Grafana 提供仪表盘 "cpc platform"：设施总功率、用电成本速率、碳排放速率、请求速率、SLO 达成率，
以及各站点的功率、电价、在线服务器数、碳强度、请求率、TTFT P99、需求响应余量、每 token 能耗、排队数、GPU 功率、
上游可用性与功率传感器状态。在 Prometheus 页面可以直接查询标准化指标，例如：

```text
site:facility_power_watts:sum                       各站点设施功率
sum(site:energy_cost_per_hour)                      全局用电成本速率（电价货币单位/小时）
increase(cpc_node_energy_joules_total[10m]) / 3.6e6 各服务器最近 10 分钟 IT 用电（kWh）
histogram_quantile(0.99, sum by (le, site) (rate(cpc_gateway_ttft_seconds_bucket[5m])))
```

=== 多台服务器之间的时延

同一地域的服务器之间时延很小，跨数据中心时延默认由网关注入（`network.emulate_wan: true`）。服务器确实分布在不同地域时，
把 `emulate_wan` 设为 `false`，由真实网络产生时延。

== 由真实服务器的测量生成服务器类型 <g-calibrate>

使仿真中的服务器与真实服务器一致：先测量真实服务器在各级并发下的功率与 token 间隔，再由测量生成服务器类型。

*第一步：准备。* 站点代理与推理实例已启动，被测服务器处于开启状态；测量期间站点不应承担其他负载，
并停止容量控制器（`docker compose stop controller`），避免控制器在测量中开关服务器。

*第二步：测量。* 在能访问站点代理的机器上：

```bash
export CPC_ADMIN_TOKEN=<与站点相同>          # 只在测量开机时间（--boot）时需要
cpc measure --site http://<站点地址>:9100 --device A10 --model-name Qwen2.5-7B-Instruct \
    --boot --out measurements/a10-qwen7b
```

`cpc measure` 依次在并发 0、1、2、4……直至服务器的并发上限下保持固定数目的流式请求（输入、输出各 256 token），
每级预热 15 秒后测量 60 秒：每秒读取站点代理给出的该服务器实测功率，并由流式响应计算 token 间隔、首 token 时延与吞吐。
并发 0 一级只测量功率，作为在线空载功率；`--boot` 经站点管理接口关闭服务器，测量关机状态的功率
（推理实例停止而机器仍在运行时的功率），再开启服务器并测量开机到可服务的时间。
运行中逐级打印结果：

```text
  concurrency 0: warm-up 0 s, measurement 60 s ...
    power 67.6 W (85% complete)  in service 0.00  inter-token -  throughput 0.0/s  requests 0  errors 0
  concurrency 1: warm-up 15 s, measurement 60 s ...
    power 202.9 W (100% complete)  in service 1.00  inter-token 29.4 ms  throughput 29.9/s  requests 14  errors 0
  ...
```

结果写为 power_profile 数据集：每级一行（`concurrency` 为实测的平均在服务请求数），描述文件的 `cpc.measurement`
记录测量条件（站点、服务器、功率范围、传感器、请求长度）与在线空载功率、关机功率、开机时间。
功率范围与站点的 `measurement` 一致：`host: server` 或 `cpu_dram` 时为服务器功率，`host: excluded` 时只含加速卡。
读数完整率低于 100% 表示部分采样的功率缺失（如传感器读数超出 `max_w`），这些采样不计入平均。各级的功率几乎相同时，
`cpc measure` 给出警告并记入数据集：传感器读数可能不随负载变化（有的 BMC 的读数长期不更新），此时宜改用 RAPL。
`--concurrency 0,1,2,3,4` 指定各级并发，`--duration`、`--warmup` 调整每级时长，`--input-tokens`、`--output-tokens` 调整请求长度。

*第三步：生成服务器类型。*

```bash
cpc profile measurements/a10-qwen7b --name a10-qwen7b
```

并发槽数、在线空载功率、关机功率与开机时间取自测量，预填充吞吐由并发 1 时的首 token 时延估计，输出中以注释标明来源；
也可以用 `--slots`、`--idle-w`、`--off-w`、`--boot-s`、`--prefill-tokens-per-s` 显式给出。若功率只含加速卡而希望服务器功率包含主机部分，
用 `--host-w` 给出主机功率。把输出写入场景的 `node_profiles`，仿真中的该类服务器即按实测的表运行。

*第四步：比较实验与仿真。* 用写入了实测服务器类型的场景运行一次实验，再对同一负载时间窗运行仿真，用 `cpc compare` 对比：

```bash
cpc live -s scenario.yaml -e endpoints.yaml --duration 900 --out results/live-1
cpc sim -s scenario.yaml --hours 0.25 --out results/sim-1
cpc compare results/live-1 results/sim-1
```

以两台 CPU 服务器（llama.cpp）为真实站点、另有一个仿真站点的 15 分钟实验为例，输出的一部分：

```text
  scope   metric                       live-1       sim-1   ratio
  total   served                          233         233    1.00
          SLO attainment                84.1%       83.7%    0.99
          IT energy (kWh)               0.137       0.131    0.96
  sh      served                           60          63    1.05
          TTFT P50 (s)                  3.494       3.847    1.10
          IT energy (kWh)               0.041       0.042    1.00
```

比值偏离 1 较多的项指出服务器类型与真实服务器的差异，例如另一台同型号服务器的空载功率不同、预填充吞吐的估计偏低等；
据此重新测量或调整服务器类型的参数。

