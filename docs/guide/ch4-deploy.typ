#import "../template/cpc-doc.typ": *

= 实验部署 <g-deploy>

实验系统由电网信号服务、各站点代理、网关与控制器组成，负载生成器按场景回放请求。各服务的地址由地址文件给出
（`-e` 或环境变量 `CPC_ENDPOINTS`）；本机运行时以 `--local` 使用固定的本机地址。场景文件与部署方式无关。

== 本机单进程 <g-local>

```bash
cpc up --speedup 60
```

`cpc up` 在一个进程中启动电网信号服务（9000）、各站点代理（9101 起）、网关（8080）与控制器（9300）；
任一服务启动失败时停止其余服务并以非零状态退出。`--speedup 60` 使信号时钟加速 60 倍（真实 1 分钟 = 信号 1 小时）。
请求日志与决策日志写入 `logs/`。常用选项：`--routing`、`--capacity` 指定全局策略，`--interval` 指定控制周期，
`--no-controller` 不启动控制器，`--initial-active` 指定各站点初始在线的服务器数，`-s` 指定场景文件。

另开一个终端发送负载：

```bash
cpc loadgen --local --duration 120 --rate-scale 0.2 --out logs/loadgen.jsonl
```

负载按真实时间回放，来源区域分配与仿真一致，场景 `workload.attributes` 选定的字段以 `x-cpc-attr-*` 请求头发送。
负载与信号时钟的加速相互独立；需要负载的日内变化与加速后的信号时钟同步时，以 `--time-scale` 压缩 trace 时间，
并以 `--rate-scale` 相应降低到达率。结束后打印汇总：发送数、成功数、端到端时延与 TTFT 的中位数和 P99、SLO 达成率、各站点分担比例。

也可以用任何 OpenAI 兼容客户端访问网关：

```bash
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
     -H "x-cpc-source: east" -H 'x-cpc-attr-session_id: "u42"' \
     -d '{"model": "cpc", "max_tokens": 32,
          "messages": [{"role": "user", "content": "hello"}]}'
```

#figure(
  tbl(
    columns: (17em, 1fr),
    header: ("地址", "内容"),
    [`http://localhost:9101/state`], [站点状态摘要：各服务器池与服务器的状态、在服务与排队请求、功率及其完整率],
    [`http://localhost:8080/state`], [网关：各来源到达率、各站点视图、当前路由策略与权重、上游健康状态],
    [`http://localhost:9300/state`], [控制器：最近一次周期的决策、观测与信号],
    [`http://localhost:9000/v1/signals`], [各站点当前的电价、碳强度、功率上限与数据来源],
    [`http://localhost:8080/docs`], [网关的交互式接口文档（其他服务同理）],
  ),
  caption: [本机运行时的观察入口],
)

运行时操作（设置了 `CPC_ADMIN_TOKEN` 时须携带 `Authorization: Bearer <令牌>`）：

```bash
curl -X PUT http://localhost:8080/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "cost_aware"}'
curl -X PUT http://localhost:9300/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "grid_aware"}'
# 需求响应：站点 gd 设施功率不超过 8 kW，持续 120 分钟（信号时间）
cpc event --local --site gd --cap-kw 8 --duration-min 120
```

== 各服务独立进程

```bash
cpc grid --local &                         # 先启动电网信号服务（信号时钟的权威来源）
cpc site --local --site sh &               # 每个站点一个进程
cpc gateway --local --log logs/gateway.jsonl &
cpc controller --local --log logs/controller.jsonl &
```

分布到多台主机时，编写地址文件（格式见《平台设计说明书》"服务接口"一章）并以 `-e` 给出；`--host`、`--port` 覆盖监听地址。
网关、控制器与负载生成器启动时须在期限内（`--startup-timeout`，缺省 30 秒）从电网信号服务同步信号时钟。

== Docker Compose <g-compose>

=== 启动

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
docker compose -f deploy/compose/docker-compose.yml ps
```

启动的容器：`grid`、`site-sh`、`site-gd`、`site-gz`、`site-nm`、`gateway`、`controller`、`prometheus`、`grafana`。
各服务通过容器名互相访问（地址文件 `deploy/compose/endpoints.yaml`），并映射到宿主机端口：

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("服务", "宿主机地址"),
    [网关], [`http://localhost:8080`],
    [电网信号], [`http://localhost:9000`],
    [控制器], [`http://localhost:9300`],
    [站点 sh/gd/gz/nm], [`http://localhost:9101` – `9104`],
    [Prometheus], [`http://localhost:9090`],
    [Grafana], [`http://localhost:3000`（匿名只读；管理员 `admin` / 环境变量 `GRAFANA_PASSWORD`，默认 `admin`）],
  ),
  caption: [Compose 部署的访问地址],
)

可在 `deploy/compose/.env` 中设置环境变量：`CPC_SPEEDUP`（信号时钟加速，默认 60）、`CPC_ROUTING`、`CPC_CAPACITY`（默认策略）、
`CPC_ADMIN_TOKEN`（管理接口令牌）、`GRAFANA_PASSWORD`。

=== 压测与观察

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm loadgen        # 默认 10 分钟
CPC_LOAD_SECONDS=1800 docker compose -f deploy/compose/docker-compose.yml run --rm loadgen
```

电网信号服务与负载生成器容器以只读方式挂载宿主机的数据集目录（`${CPC_DATA_DIR:-~/.cache/cpc/datasets}`），
须先在宿主机准备默认场景的数据集（@g-data）；到达率可用环境变量 `CPC_RATE_SCALE` 缩放。

打开 Grafana 的"算电协同调度平台"仪表盘，可以看到
设施总功率、用电成本速率、碳排放速率、请求速率、SLO 达成率，以及各站点的功率、电价、在线服务器数、碳强度、请求率、
TTFT P99、需求响应余量、每 token 能耗、排队数与 GPU 功率。请求日志与决策日志在 `logs/compose/`。

在 Prometheus 页面（`http://localhost:9090`）可以直接查询标准化指标，例如：

```text
site:facility_power_watts:sum                       各站点设施功率
sum(site:energy_cost_per_hour)                      全局用电成本速率（电价货币单位/小时）
increase(cpc_node_energy_joules_total[10m]) / 3.6e6 各服务器最近 10 分钟 IT 用电（kWh）
histogram_quantile(0.99, sum by (le, site) (rate(cpc_gateway_ttft_seconds_bucket[5m])))
```

=== 验证与停止

```bash
python scripts/smoke_test.py --external -e configs/endpoints.compose-host.yaml
docker compose -f deploy/compose/docker-compose.yml down
```

== Kubernetes（kind） <g-kind>

=== 部署

```bash
python deploy/k8s/kind_up.py
```

脚本依次完成：按场景生成 Kubernetes 清单与 kind 集群配置（`deploy/k8s/generate.py`）→ 创建名为 `cpc` 的 kind 集群
（1 个控制面节点 + 每个站点 1 个 worker 节点，worker 带 `topology.kubernetes.io/region=<站点 id>` 标签；
控制面节点挂载宿主机的数据集目录 `${CPC_DATA_DIR}`，供电网信号服务读取）→ 构建平台镜像 →
把平台、Prometheus、Grafana 镜像加载到集群节点 → `kubectl apply` → 等待全部 Deployment 就绪。完成后：

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("服务", "地址（NodePort 映射到宿主机）"),
    [网关], [`http://localhost:30080`],
    [Grafana], [`http://localhost:30300`],
    [Prometheus], [`http://localhost:30090`],
    [电网信号 / 控制器], [`http://localhost:30900` / `http://localhost:30930`],
    [站点], [`http://localhost:30101` 起],
  ),
  caption: [kind 部署的访问地址],
)

```bash
# 站点 Pod 分布在各自 region 的节点上
kubectl --context kind-cpc -n cpc get pods -o wide
kubectl --context kind-cpc -n cpc logs deploy/controller -f
python scripts/smoke_test.py --external -e configs/endpoints.kind-host.yaml
python deploy/k8s/kind_up.py --down                     # 删除集群
```

=== 清单结构

`deploy/k8s/generated/` 中的清单由生成器产生，修改场景（如增加站点）后重新运行 `python deploy/k8s/generate.py` 即可：

- `00-namespace.yaml`：命名空间 `cpc`；
- `10-config.yaml`：地址文件、Prometheus 配置与记录规则、Grafana 数据源与仪表盘（ConfigMap）；
- `20-grid.yaml`、`22-gateway.yaml`、`23-controller.yaml`：电网信号服务（固定在控制面节点，以 hostPath 读取数据集）、网关与控制器；
- `21-sites.yaml`：每个站点一个 Deployment + Service，`nodeSelector` 固定到对应 region 的节点；
- `30-monitoring.yaml`：Prometheus 与 Grafana。

#note(title: "代理环境")[如果宿主机通过 `127.0.0.1` 上的代理上网，kind 节点会继承这个代理地址，但在节点容器内它是不可达的，
直接拉取镜像会失败。`kind_up.py` 因此先在宿主机准备好全部镜像再加载到节点中（必要时导出单一平台的镜像包），节点本身不需要联网。]

=== 真实 Kubernetes 集群

在真实集群（例如在云服务器上用 k3s 搭建）中，推荐的做法是：用 NVIDIA GPU Operator 安装设备插件与 dcgm-exporter；
给节点打上 `topology.kubernetes.io/region` 标签表示站点；每个站点部署一个 vLLM Deployment（每个副本占一张 GPU）；
容量控制器使用 Kubernetes 执行器直接伸缩副本数：

```bash
cpc controller -s scenario.yaml --actuator kubernetes \
               --k8s-namespace cpc --k8s-template "vllm-{site}"
```

此时站点代理的真实服务器池（`backend: openai`）的上游指向该站点 vLLM 的 Service。
== 接入真实服务器 <g-real>

真实服务器是一个 OpenAI 兼容推理实例（vLLM、SGLang、llama.cpp server 等）及其占用的加速卡，在场景中以真实服务器池声明，
站点的功率测量在 `measurement` 中声明：

```yaml
node_profiles:
  gpu-server: {gpus: 1, gpu_model: measured, host_idle_w: 150, host_max_w: 250, slots: 32}
sites:
  - id: lab
    region: guizhou
    pools:
      - id: gpu
        profile: gpu-server
        backend: openai
        upstreams:
          - {id: lab-0, url: "http://127.0.0.1:8000", gpu_indices: [0],
             container: cpc-vllm-0, max_inflight: 32}
    measurement:
      sensors: [{type: nvml, gpus: [0]}]
      host: excluded
```

- `gpu_indices`（或 `devices`）声明实例占用的加速卡，站点代理按此把传感器读数归属到服务器；
- `container` 为可选的容器名：给出时站点代理通过 Docker Engine 启停该容器实现开关机（需安装 `docker` 可选依赖，并能访问 Docker 套接字）；
  未给出时开关机只停止向该实例派发请求；
- `max_inflight` 为并发上限，宜与推理引擎的最大批大小一致；`ready_path`（缺省 `/health`）用于就绪检查；
- 服务器类型的参数在实验中用于全局策略的容量与功率估计，在仿真中用于模拟该服务器，宜由该服务器的测量标定（@g-calibrate）。

站点有仿真服务器时，请求须给出 `max_tokens`；负载生成器发送 `max_tokens` 与 `ignore_eos: true`，使真实模型的输出长度与 trace 一致，
请求中的 `model` 由 `cpc loadgen --model` 指定，须与推理实例的模型名一致。
站点代理启动时检查传感器可用、声明的加速卡有读数、配置的容器存在，任一不满足即退出并报告原因。

*功率封顶。* 以 `cpc site --enable-power-cap` 启动时，容量指令中的功率封顶比例通过传感器设置加速卡功率上限（NVML 需要 root 权限）；
未开启时只记录目标比例。云服务器上部分 GPU 不允许用户修改功率上限。

*整机开关。* 停止推理容器后 GPU 处于空载，仍然耗电，测得的功率如实反映这一点。研究整机关机节能时，
可通过 BMC（IPMI、Redfish）或云平台接口实现新的执行器钩子（《平台设计说明书》"扩展"一章）。

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
# 或 xingchui/cpc-platform:0.2.0，或自行 docker build
docker pull ghcr.io/x1ngchui/cpc-platform:0.2.0
```

=== 站点栈（每台 GPU 服务器）

```bash
cd deploy/cloud/site-stack
cp .env.example .env                 # 修改 SITE_ID、MODEL、CPC_ADMIN_TOKEN、CPC_IMAGE 等
cp ../scenario.cloud.example.yaml scenario.yaml
cp ../endpoints.cloud.example.yaml endpoints.yaml   # 填入各服务器内网 IP
docker compose up -d                 # 启动 vllm-0 与 site-agent
docker compose logs -f site-agent    # 等待"功率传感器：['nvml']"与 vLLM 就绪
```

站点的功率测量在场景文件的 `measurement` 中声明（示例为 NVML 读取 GPU，`host: excluded` 表示服务器 IT 功率只含 GPU）。
站点代理启动时检查传感器可用、服务器声明的 GPU 有读数、配置的容器存在；任一不满足时容器以非零状态退出，
原因见 `docker compose logs site-agent`。服务器提供整机功率读数（IPMI、Redfish）时，可加入相应传感器并改为 `host: server`。

站点栈包含：`vllm-0`（每个推理实例占一张 GPU；双卡服务器可启用 `--profile gpu2` 的 `vllm-1`）、`site-agent`
（排队与派发、NVML 功率采集、通过挂载的 Docker 套接字启停 vLLM 容器）、可选的 `dcgm-exporter`（`--profile dcgm`）
与 `node-exporter`（`--profile host`，监听 9101）。

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

=== 控制栈（控制节点）

```bash
cd deploy/cloud/control-stack
cat > .env <<'EOF'
CPC_ADMIN_TOKEN=<与站点相同>
GRAFANA_PASSWORD=<自定>
CPC_IMAGE=ghcr.io/x1ngchui/cpc-platform:0.2.0
EOF
cp ../scenario.cloud.example.yaml scenario.yaml      # 与各站点使用同一份场景文件
cp ../endpoints.cloud.example.yaml endpoints.yaml
cp ../prometheus.cloud.example.yml prometheus.yml    # 填入各服务器 IP
docker compose up -d
python3 ../../../scripts/smoke_test.py --external -e endpoints.yaml -s scenario.yaml
```

冒烟测试脚本需要 Python 环境：在控制节点上按 @g-setup 安装（`pip install -e .`）后运行。控制栈的电网信号服务与负载生成器
挂载控制节点的数据集目录（`CPC_DATA_DIR`），须先在控制节点准备场景所需的数据集（@g-data）。

=== 运行实验与收集数据

1. 确认信号时钟：正式实验把场景中的 `clock.speedup` 设为 1，`clock.start` 设为希望模拟的日期时刻；
2. 在控制节点运行负载：`cpc loadgen -s scenario.yaml -e endpoints.yaml --duration 7200 --out logs/loadgen.jsonl`；
3. 数据位置：网关请求日志与控制器决策日志在 `control-stack/logs/`；指标在 Prometheus 中（可用 HTTP API 导出：
   `curl 'localhost:9090/api/v1/query_range?query=site:facility_power_watts:sum&start=...&end=...&step=15'`）；
4. 实验结束后停止站点栈与控制栈（`docker compose down`），按需关机或释放实例。

=== 多台服务器之间的时延

同一地域的服务器之间时延很小，跨数据中心时延默认由网关注入（`network.emulate_wan: true`）。如果希望时延出现在网络层，
可以把 `emulate_wan` 设为 `false`，在控制节点上用 `deploy/cloud/netem.sh` 对各站点 IP 加单向时延（需要 root）：

```bash
sudo ./netem.sh eth0 add 10.0.0.11=14 10.0.0.12=12 10.0.0.13=10
sudo ./netem.sh eth0 del
```

== 由真实服务器的测量标定 <g-calibrate>

使仿真中的服务器模型与真实服务器一致的步骤：

1. 只向一个真实服务器所在的站点发请求，逐级提高到达率（例如每级 3 分钟），直至并发接近上限：
   `cpc loadgen -s scenario.yaml --gateway http://<站点地址>:9100 --grid http://<控制节点>:9000 --rate-scale <r> --duration 180`；
2. 由 Prometheus 导出各时刻的在服务请求数与 GPU 功率（`cpc_node_busy_slots`、`DCGM_FI_DEV_POWER_USAGE`），
   由请求日志得到 token 间隔；按稳态区间整理为 power_profile 数据集（每级一行：`device`、`device_count`、`model`、
   `concurrency`、`power_w`、`itl_s`）；
3. `cpc data calibrate <数据集> --where ... --slots <并发上限>`（可加 `--curves`），把结果写入场景的 `node_profiles`；
   空载功率取无请求时的实测值，预填充吞吐由首 token 时延估计；
4. 在数据集描述文件中记录测量条件（GPU 型号、模型、推理引擎及其版本、批大小上限）。

