# cpc-platform：算电协同跨数据中心 AI 计算任务调度平台

[English](README.md)

面向算电协同研究的跨数据中心 AI 推理调度平台。平台以统一的数据规范接入负载、电网信号与功率特性数据，
以场景描述数据中心、服务器池与电网环境，以全局（跨数据中心）与本地（数据中心内部）两级策略接口组织调度，
并以同一套服务器模型、数据中心运行时与策略实现支持离散事件仿真与接入真实推理实例的实验。

- **数据规范**：数据集为描述文件（Frictionless Data Package v2 子集 + `cpc` 类别规范）与数据文件（CSV / Parquet）。
  每类数据规定核心字段，其余信息以扩展字段承载并可传给策略；平台只做校验与列式加载，原始数据的转换范例见 `examples/datasets/`
  （Azure LLM Inference Trace 2023 / 2024、ML.ENERGY Benchmark、中国省级分时电价与排放因子）。
- **仿真与实验共用**：场景、服务器模型（功率与 token 间隔随占用率变化的表）、数据中心运行时（服务器状态机、排队与派发、容量指令、能耗计量）、
  全局与本地策略在两种运行方式中为同一实现；实验中仿真服务器与真实服务器可在同一站点共存。
- **两级调度**：全局路由策略与容量策略只依赖数据中心状态摘要；站内派发策略与电源管理策略负责数据中心内部，
  可利用请求扩展属性与异构服务器池。策略以 entry points 注册，新增实现不修改平台。
- **能耗测量**：NVIDIA GPU（NVML）、CPU 与内存（RAPL）、服务器整机（本机 BMC，IPMI DCMI），其他设备以命令行适配
  （command）或以 entry points 注册传感器；指标为 Prometheus 格式，GPU 指标与 dcgm-exporter 同名。
- **失败显式化**：配置与环境错误在启动时报错；运行期故障隔离在其所属范围内，以健康检查、状态字段与指标报告，
  缺失的数据不以其他值替代。
- **结果数据包**：仿真与实验的结果写为相同格式的数据包（逐请求记录、时间序列、汇总、决策、运行描述），记录场景、策略、数据集与平台版本。

服务端组件只面向 Linux（x86_64、aarch64）；开发环境为 Linux 或 WSL2（Ubuntu）。

## 文档

| 文档 | 中文 | 英文 | 内容 |
|---|---|---|---|
| 平台设计说明书 | [design-zh.pdf](docs/pdf/design-zh.pdf) | [design-en.pdf](docs/pdf/design-en.pdf) | 架构、数据规范、场景与模型、数据中心运行时、调度策略、能耗测量与核算、服务接口、仿真与实验、扩展 |
| 使用手册 | [guide-zh.pdf](docs/pdf/guide-zh.pdf) | [guide-en.pdf](docs/pdf/guide-en.pdf) | 安装、数据准备、场景、仿真、实验（本机 / 云服务器 / 真实服务器）、策略编写、观测与运维、开发 |

## 快速开始

```bash
git clone https://github.com/X1ngChui/cpc-platform.git && cd cpc-platform
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

pytest                                    # 单元测试
python scripts/smoke_test.py --offline    # 端到端冒烟测试（测试场景，不需要下载数据）

# 准备快速上手场景的数据（写入 ${CPC_DATA_DIR:-~/.cache/cpc/datasets}，约 1 MB）
python examples/datasets/azure_llm.py --variant 2023-conv --zero-output fail
python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01

S=examples/quickstart/scenario.yaml
cpc check -s $S                                     # 检查场景与数据，打印概要
cpc sim -s $S --hours 0.9 --out results/qs          # 仿真，结果写为结果目录
cpc run examples/quickstart/experiment.yaml         # 比较多组策略
cpc live -s $S --duration 120 --rate-scale 0.2      # 以真实时间运行实验，结果格式与仿真相同
cpc compare results/quickstart/nearest-reactive results/quickstart/cost-reactive   # 并列比较两个结果
```

Python 中：`cpc.simulate("examples/quickstart/scenario.yaml", routing="cost_aware", hours=0.9)`、`cpc.read_run(<结果目录>)`。
默认场景 `configs/scenario.yaml` 使用 Azure 2024 一周的负载（`azure_llm.py --variant 2024-conv`，原始文件约 1 GB）。
云服务器部署与真实服务器接入见《使用手册》。

## 镜像

`ghcr.io/x1ngchui/cpc-platform`（GitHub Actions 构建）与 `xingchui/cpc-platform`（Docker Hub），均为 amd64 / arm64，入口为 `cpc`：

```bash
docker run --rm --gpus all ghcr.io/x1ngchui/cpc-platform:0.4.1 sensors --types nvml
```

## 目录

```text
src/cpc/              datasets 数据集 · datacenter 数据中心运行时 · sched 策略 · sim 仿真 · results 结果数据包
                      grid / site / gateway / controller / live 实验服务与实验 · sensors 功率传感器 · measure 测量 · cli 命令行
examples/datasets/    原始公开数据的转换范例
examples/experiments/ 实验描述范例
examples/quickstart/  快速上手场景与实验描述
configs/              场景文件与地址文件
deploy/               cloud（站点栈 / 控制栈）· prometheus 记录规则 · grafana 仪表盘
scripts/              端到端冒烟测试
tests/                单元测试与测试数据
docs/                 Typst 源文件（zh、en）、draw.io 插图源文件与 PDF
```

## 说明

服务器类型中不来自测量数据的参数（在线空载功率、主机功率、预填充吞吐、开机时间）与分时电价的平段价格为示意值；
接入真实服务器后应以实测数据生成服务器类型（`cpc measure`、`cpc profile`）。平台附带的调度策略用于演示接口与数据通路，
作为比较的基线，不代表推荐的研究方法。
