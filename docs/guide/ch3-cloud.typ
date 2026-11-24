#import "../template/cpc-doc.typ": *

= 云服务器部署（真实 GPU） <g-cloud>

本章说明如何租用少量云服务器，把它们接入平台作为真实站点。跨地域部分仍通过配置模拟：
服务器通常位于同一地域，站点所在省份、电网信号与广域网时延由场景文件给定。

== 规划

推荐的最小配置：

#figure(
  tbl(
    columns: (8em, 6em, 1fr),
    header: ("角色", "数量", "建议规格"),
    [GPU 服务器], [2–3 台], [单卡即可（如 24 GB 显存级别的推理卡）；Ubuntu 22.04/24.04；云厂商的"GPU 驱动预装"镜像可省去装驱动的步骤],
    [控制节点], [1 台], [4 核 8 GB 的普通云主机即可；运行电网信号、网关、控制器、Prometheus、Grafana 与仿真站点],
  ),
  caption: [云服务器规划],
)

- 所有服务器放在同一地域、同一 VPC，使用内网 IP 互访；
- 每台 GPU 服务器扮演一个站点（场景文件中 `backend: openai` 的站点），还可以在控制节点上运行若干仿真站点补足地域；
- 按需计费的实例在不用时及时关机或释放，避免产生费用。

== 网络与安全

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
ssh -L 3000:localhost:3000 -L 9090:localhost:9090 <控制节点>     # 然后访问 http://localhost:3000
```

== 安装驱动、Docker 与 NVIDIA Container Toolkit

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

== 获取平台镜像与代码

```bash
git clone https://github.com/X1ngChui/cpc-platform.git && cd cpc-platform
docker pull xingchui/cpc-platform:0.1.0        # 或 ghcr.io/x1ngchui/cpc-platform:0.1.0，或自行 docker build
```

== 站点栈（每台 GPU 服务器）

```bash
cd deploy/cloud/site-stack
cp .env.example .env                 # 修改 SITE_ID、MODEL、CPC_ADMIN_TOKEN、CPC_IMAGE 等
cp ../scenario.cloud.example.yaml scenario.yaml
cp ../endpoints.cloud.example.yaml endpoints.yaml   # 填入各服务器内网 IP
docker compose up -d                 # 启动 vllm-0 与 site-agent
docker compose logs -f site-agent    # 等待"功率传感器：['nvml']"与 vLLM 就绪
```

站点的功率来源在场景文件的 `power` 中声明（示例为 NVML 读取 GPU，`host: excluded` 表示节点 IT 功率只含 GPU）。
站点代理启动时检查传感器可用、节点声明的 GPU 有读数、配置的容器存在；任一不满足时容器以非零状态退出，
原因见 `docker compose logs site-agent`。服务器提供整机功率读数（IPMI、Redfish）时，可加入相应传感器并改为 `host: server`。

站点栈包含：`vllm-0`（每个推理实例占一张 GPU；双卡服务器可启用 `--profile gpu2` 的 `vllm-1`）、`site-agent`
（排队与派发、NVML 功率采集、通过挂载的 Docker 套接字启停 vLLM 容器）、可选的 `dcgm-exporter`（`--profile dcgm`）
与 `node-exporter`（`--profile host`，监听 9101）。

`.env` 中的 `MODEL` 应选择能放进显存的模型；国内服务器下载模型可设置 `HF_ENDPOINT=https://hf-mirror.com`，或设置
`VLLM_USE_MODELSCOPE=True` 从 ModelScope 下载。也可以先在宿主机下载好模型（`huggingface-cli download <模型名>`），
容器挂载缓存目录后离线加载。

检查：

```bash
curl -s localhost:9100/state | python3 -m json.tool | head -30       # nodes_on 应为 1，power_source 为 sensors
curl -s localhost:9100/readyz                                         # 就绪状态
docker compose exec site-agent cpc sensors --types nvml               # 读取 NVML 读数
curl -s localhost:8000/v1/models                                      # vLLM 模型名应为 cpc
```

== 控制栈（控制节点）

```bash
cd deploy/cloud/control-stack
printf "CPC_ADMIN_TOKEN=<与站点相同>\nGRAFANA_PASSWORD=<自定>\nCPC_IMAGE=xingchui/cpc-platform:0.1.0\n" > .env
cp ../scenario.cloud.example.yaml scenario.yaml      # 与各站点使用同一份场景文件
cp ../endpoints.cloud.example.yaml endpoints.yaml
cp ../prometheus.cloud.example.yml prometheus.yml    # 填入各服务器 IP
docker compose up -d
python3 ../../../scripts/smoke_test.py --external -e endpoints.yaml -s scenario.yaml
```

冒烟测试脚本需要 Python 环境：在控制节点上按 @g-setup 安装（`pip install -e .`）后运行。

== 标定节点功率模型 <g-calibrate>

接入真实服务器后，建议用实测数据标定仿真站点的节点参数，使仿真与真实一致：

1. 只向一个真实站点发请求，逐级提高并发（例如每级 3 分钟，rps 从 0.5 提高到饱和）：
   `cpc loadgen -s scenario.yaml --gateway http://<站点IP>:9100 --grid http://<控制节点IP>:9000 --rps <r> --duration 180 --model cpc`；
2. 同时记录站点状态（`busy`、`gpu_power_w`）——可直接改用 `scripts/real_gpu_check.py` 的采样逻辑，或从 Prometheus 导出
   `cpc_node_busy_slots` 与 `DCGM_FI_DEV_POWER_USAGE`；
3. 以占用率 $u = "busy" \/ "slots"$ 为横轴拟合 $P = P_"idle" + (P_"max" - P_"idle") u^alpha$，得到 `gpu_idle_w`、`gpu_max_w`、`power_exponent`；
   用请求日志中的 TTFT 与 TPOT 标定 `prefill_tokens_per_s`、`decode_s_per_token`、`decode_slowdown`；
4. 把结果写入场景文件中仿真站点的 `node_profiles`，并在实验报告中注明标定条件（GPU 型号、模型、vLLM 版本、批大小上限）。

== 功率封顶与整机开关

- *GPU 功率封顶*：NVML 设置功率上限需要 root 权限。站点代理需以 `--enable-power-cap` 启动，并在容器中以特权模式运行
  （或直接在宿主机上以 root 运行 `cpc site`）。云服务器上部分 GPU 不允许用户修改功率上限，以实际测试为准。
- *整机开关*：停止 vLLM 容器只会让 GPU 进入空载，GPU 仍然耗电（本机实测 RTX 5070 Ti 约 43 W）。若研究需要真正的关机节能，
  可通过 BMC（IPMI / Redfish）或云厂商 API 关机，实现方式是编写一个新的节点钩子或执行器（见 @g-dev）。

== 运行实验与收集数据

1. 确认信号时钟：正式实验把场景中的 `clock.speedup` 设为 1，`clock.start` 设为希望模拟的日期时刻；
2. 在控制节点运行负载：`cpc loadgen -s scenario.yaml -e endpoints.yaml --duration 7200 --out logs/loadgen.jsonl`；
3. 数据位置：网关请求日志与控制器决策日志在 `control-stack/logs/`；指标在 Prometheus 中（可用 HTTP API 导出：
   `curl 'localhost:9090/api/v1/query_range?query=site:facility_power_watts:sum&start=...&end=...&step=15'`）；
4. 实验结束后停止站点栈与控制栈（`docker compose down`），按需关机或释放实例。

== 多台服务器之间的时延

同一地域的服务器之间时延很小，跨数据中心时延默认由网关注入（`network.emulate_wan: true`）。如果希望时延出现在网络层，
可以把 `emulate_wan` 设为 `false`，在控制节点上用 `deploy/cloud/netem.sh` 对各站点 IP 加单向时延（需要 root）：

```bash
sudo ./netem.sh eth0 add 10.0.0.11=14 10.0.0.12=12 10.0.0.13=10
sudo ./netem.sh eth0 del
```
