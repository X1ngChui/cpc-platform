# cpc-platform：算电协同跨数据中心 AI 计算任务调度虚拟化平台

面向"算电协同"研究的实验平台：把多个（虚拟）数据中心站点、电网侧信号（分时电价、碳排放因子、需求响应）、
**全局请求路由**与**节点容量管理**组织为一套接口统一的系统，并以**多硬件能耗感知**为基础。

- **三种运行形态，共用一套策略代码**：离散事件仿真（24 小时约 1 分钟跑完）、容器化虚拟部署（单机 / Docker Compose / Kubernetes）、接入真实 GPU 服务器（vLLM + 硬件功率传感器），三者可混合。
- **标准接口**：推理接口兼容 OpenAI API；指标为 Prometheus 格式，GPU 指标与 NVIDIA dcgm-exporter 同名；部署为 Docker 镜像与 Kubernetes 清单。
- **能耗感知**：设备 → 服务器 → 设施（PUE）→ 电网四层；支持 NVIDIA（NVML / nvidia-smi）、AMD（AMD SMI / hwmon）、Intel 独显（hwmon）、CPU（RAPL）、华为昇腾（DCMI）、服务器整机（IPMI / Redfish）以及任意 Prometheus 导出器、命令行工具、HTTP 接口。
- **运行环境**：服务端组件只面向 Linux（x86_64、aarch64）；开发环境为 Linux 或 WSL2（Ubuntu）。
- **数据集驱动**：负载、电网信号与节点功率特性经统一的数据集接口接入，清单记录版本、来源、许可、校验和与引用（`cpc data list`）。
  已接入 Azure LLM Inference Trace 2023 / 2024、BurstGPT、Mooncake FAST'25 trace、ML.ENERGY Benchmark v3、EIA-930，
  以及各省分时电价政策与生态环境部 2023 年省级电力排放因子；默认场景的节点参数由 ML.ENERGY 测量数据标定。
- **策略可插拔**：路由策略、容量策略、功率传感器都是带注册装饰器的 Python 类；仓库只提供演示数据通路用的参考实现。
- **失败显式化**：配置与环境错误在启动时报错；运行期故障（传感器、站点、电网信号服务）被隔离在其所属范围内，
  通过 `/healthz`、`/readyz`、状态字段与 `cpc_*_up` / `*_errors_total` 指标报告，缺失的读数不以其他值替代。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/pdf/design.pdf](docs/pdf/design.pdf) | 《平台设计说明书》：需求、架构、能耗感知、请求路由、容量管理、接口、实验支撑、扩展与上手路线 |
| [docs/pdf/guide.pdf](docs/pdf/guide.pdf) | 《部署与使用手册》：本机 / Compose / kind / 云服务器部署，能耗感知操作，开发指南，常见问题 |

文档源文件为 Typst，`pip install typst && python docs/build.py` 重新编译。

## 快速开始

```bash
git clone https://github.com/X1ngChui/cpc-platform.git ~/cpc-platform && cd ~/cpc-platform
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest                                  # 单元测试（离线）
cpc data fetch azure-llm-2024-conv      # 下载默认场景的负载数据集（约 1.1 GB）并校验
python scripts/smoke_test.py            # 端到端冒烟测试（启动全部服务、压测、需求响应、故障隔离）

cpc up --speedup 60                     # 本机启动：电网信号 9000、站点 9101-9104、网关 8080、控制器 9300
cpc loadgen --local --duration 120 --rate-scale 0.2   # 另一个终端回放负载（--local：本机模式地址）
cpc compare --hours 24                  # 仿真对比四组策略组合
cpc sensors --list                      # 列出功率传感器类型；cpc sensors --types nvml 读取指定传感器
```

Docker Compose（含 Prometheus 与 Grafana 仪表盘）：

```bash
docker compose -f deploy/compose/docker-compose.yml up -d --build
docker compose -f deploy/compose/docker-compose.yml run --rm loadgen
# Grafana: http://localhost:3000   Prometheus: http://localhost:9090
```

Kubernetes（kind，每个站点一个带 region 标签的节点）：`python deploy/k8s/kind_up.py`

云服务器（真实 GPU）：见 `deploy/cloud/` 与《部署与使用手册》第 6 章。

## 镜像

- Docker Hub：`xingchui/cpc-platform:0.1.0`（amd64 / arm64）
- GHCR：`ghcr.io/x1ngchui/cpc-platform`（GitHub Actions 自动构建）

```bash
docker run --rm --gpus all xingchui/cpc-platform:0.1.0 sensors          # 容器内探测 GPU 功率
docker run -d --gpus all -p 9400:9400 xingchui/cpc-platform:0.1.0 exporter --site gz
```

## 目录

```text
src/cpc/      datasets 数据集 · grid 电网信号 · site 站点代理 · sensors 功率传感器 · gateway 路由网关 · controller 容量控制器
              sched 策略插件 · sim 离散事件仿真 · loadgen 负载生成 · cli 命令行
configs/      场景文件（实验世界）与地址文件（服务在哪里）
deploy/       compose · k8s · cloud（站点栈 / 控制栈）· prometheus 记录规则 · grafana 仪表盘
scripts/      冒烟测试、公开数据集检验、真实 GPU 检查、文档插图
tests/        单元测试
docs/         Typst 源文件、模板、插图与 PDF
```

## 已验证的环境

- WSL2（Ubuntu 26.04）+ Python 3.12：单元测试与单进程冒烟测试通过；NVML 可读取 NVIDIA GeForce RTX 5070 Ti 的功率与能耗计数；
- v0.1.0：单进程、Docker Compose、kind v0.31.0 三种部署均通过冒烟测试；
- NVIDIA GeForce RTX 5070 Ti + vLLM v0.34.0（Qwen2.5-0.5B-Instruct）：真实站点的请求转发、就绪检查、NVML 实测功率、容器级开关机（v0.1.0）；
- 发布镜像的 amd64 与 arm64 版本均可运行；容器内 NVML 可读到 GPU 功率。

其余硬件的传感器依据官方接口文档实现，解析逻辑有单元测试，需在目标硬件上验证（`cpc sensors --list` 中标注）。

## 说明

节点功率与性能模型的默认参数、电价的平段价格为示意值，仅用于让仿真量级合理；接入真实服务器后应按手册标定。
平台附带的调度策略只用于演示接口与数据通路，不代表推荐的研究方法。
