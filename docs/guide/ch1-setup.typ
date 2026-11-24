#import "../template/cpc-doc.typ": *

= 概述 <g-overview>

== 平台能做什么

平台把多个（虚拟）数据中心站点、电网信号（分时电价、碳排放因子、需求响应）、全局请求路由与节点容量管理组织在一起，
用于开展"算电协同"的跨数据中心调度实验。它有三种运行形态：

- *仿真*：`cpc sim` / `cpc compare`，几十秒仿真 24 小时，用于大规模比较调度策略；
- *虚拟化*：在一台电脑上用容器（或单进程）运行全部服务，节点由模型模拟，用于联调与演示；
- *真实*：站点接入真实 GPU 服务器上的 vLLM 推理实例，功率来自硬件传感器。

三种形态共用同一套策略代码与配置文件，并且可以混合（部分站点真实、部分站点仿真）。

== 选择部署方式

#figure(
  tbl(
    columns: (9em, 1fr, 9em),
    header: ("部署方式", "适合", "章节"),
    [本机单进程], [第一次上手、开发调试；不需要 Docker], [@g-quick],
    [Docker Compose], [单机完整环境，含 Prometheus 与 Grafana 仪表盘], [@g-compose],
    [Kubernetes（kind）], [学习 Kubernetes 原生部署；为在真实集群上部署做准备], [@g-kind],
    [云服务器], [接入真实 GPU 开展实验], [@g-cloud],
  ),
  caption: [部署方式与章节],
)

== 获取代码与镜像

```bash
git clone https://github.com/X1ngChui/cpc-platform.git
cd cpc-platform
```

平台的全部服务打包在同一个容器镜像中，已发布到两个镜像仓库（支持 amd64 与 arm64）：

- Docker Hub：`xingchui/cpc-platform:0.1.0`（以及 `latest`）
- GitHub Container Registry：`ghcr.io/x1ngchui/cpc-platform`（由 GitHub Actions 在每次推送 `main` 与发布标签时自动构建）

也可以在仓库根目录用 `docker build -t cpc-platform:latest .` 自行构建；国内服务器构建时可加
`--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` 使用 PyPI 镜像。

= 环境准备 <g-setup>

== 运行环境

平台的服务端组件（电网信号服务、站点代理、路由网关、容量控制器、通用功率导出器）只面向 Linux（x86_64、aarch64），
容器镜像同样提供这两种架构。开发环境为 Linux 或 WSL2（Ubuntu）；在 WSL2 中，仓库应克隆在 Linux 文件系统内（如 `~/`），
不放在 `/mnt/*` 下，以避免跨文件系统访问的性能损失与文件权限问题。

== Python 环境

需要 Python 3.10 及以上（CI 使用 3.12）。在仓库根目录创建虚拟环境并以可编辑模式安装：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # 平台 + 测试与作图依赖
pip install -e ".[nvml,docker]"  # 可选：NVIDIA GPU 功率采集、通过 Docker 启停推理容器
pip install -e ".[docs]"         # 可选：编译文档（Typst）
```

Debian / Ubuntu 的系统 Python 需要先安装 `python3-venv`。也可以使用 uv：
`uv venv --python 3.12 && uv pip install -e ".[dev]"`。安装后即可使用命令 `cpc`（等价于 `python -m cpc`）。

== Docker

Docker Compose 与 kind 部署需要 Docker Engine 与 Compose 插件（`curl -fsSL https://get.docker.com | sh`）。
验证：`docker version`、`docker compose version`。kind 部署需要运行 5 个节点容器，Docker 可用内存建议不少于 8 GB。

== kind 与 kubectl（可选）

kind（Kubernetes in Docker）是 Kubernetes 官方的本地集群工具，单个可执行文件。从
#link("https://kind.sigs.k8s.io/docs/user/quick-start/#installation") 下载 Linux 版本（本平台在 v0.31.0 上验证），
放到 `PATH` 中的目录；kubectl 从 Kubernetes 官网下载 Linux 版本。验证：`kind version`、`kubectl version --client`。

== 验证安装

```bash
pytest                              # 单元测试，约 10 秒
python scripts/smoke_test.py        # 端到端冒烟测试，约 1.5 分钟
```

冒烟测试把各服务作为独立进程启动，依次检查：全部服务就绪；压测请求全部成功；控制器完成决策；能耗与电网信号指标；
需求响应事件生效；传感器故障（真实后端站点的功率计不可用）与站点故障（停止一个站点进程）被隔离并显式报告，
其余部分正常服务。最后输出"冒烟测试通过"即说明环境正常。

= 本机快速开始 <g-quick>

== 启动全部服务

```bash
cpc up --speedup 60
```

`cpc up` 在一个进程中启动电网信号服务（9000）、4 个站点代理（9101–9104）、网关（8080）与容量控制器（9300）；
任一服务启动失败时停止其余服务并以非零状态退出。
`--speedup 60` 表示信号时钟加速 60 倍（真实 1 分钟 = 信号 1 小时），便于在短时间内看到电价变化。
请求日志与决策日志写入 `logs/` 目录。常用选项：`--routing`、`--capacity` 指定策略，`--interval` 指定控制周期，
`--no-controller` 不启动控制器，`-s` 指定其他场景文件。

== 发送负载

另开一个终端：

```bash
cpc loadgen --local --duration 120 --rps 10 --out logs/loadgen.jsonl
```

`--local` 表示使用与 `cpc up` 相同的本机模式地址；访问其他部署时用 `-e <地址文件>` 给出服务地址。

结束后打印汇总：发送数、成功数、端到端时延与 TTFT 的中位数和 P99、SLO 达成率、各站点分担比例。
也可以用任何 OpenAI 客户端直接访问网关，例如：

```bash
curl http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
     -H "x-cpc-source: east" \
     -d '{"model": "cpc", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 32}'
```

== 观察运行状态

#figure(
  tbl(
    columns: (19em, 1fr),
    header: ("地址", "内容"),
    [`http://localhost:9101/state`], [站点 sh 的状态快照：节点状态、在服务与排队请求、功率及其完整率],
    [`http://localhost:8080/readyz`], [网关就绪状态：可用站点、电网信号状态与各上游的健康状态],
    [`http://localhost:9101/metrics`], [站点指标（含 DCGM 兼容的 GPU 功率）],
    [`http://localhost:8080/state`], [网关：各来源到达率、各站点负载视图、当前路由策略与权重],
    [`http://localhost:9300/state`], [控制器：最近一次决策（目标、观测、电网信号）],
    [`http://localhost:9000/v1/signals`], [各站点当前电价、碳排放因子、功率上限],
    [`http://localhost:8080/docs`], [网关的交互式 API 文档（其他服务同理）],
  ),
  caption: [本机模式下的观察入口],
)

== 运行时操作

```bash
# 切换路由策略
curl -X PUT http://localhost:8080/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "cost_aware"}'
# 切换容量策略
curl -X PUT http://localhost:9300/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "grid_aware"}'
# 注入需求响应事件：站点 gd 设施功率不超过 8 kW，持续 120 分钟（信号时间）
cpc event --local --site gd --cap-kw 8 --duration-min 120
```

== 仿真与对比

```bash
cpc signals --date 2026-07-15        # 打印某日 24 小时各站点电价、时段、碳排放因子
cpc rtt                               # 打印来源—站点 RTT 矩阵
cpc sim --routing cost_aware --capacity reactive --hours 24 --out results/my-run
cpc compare --runs "nearest:static,least_loaded:reactive,cost_aware:reactive,weighted:grid_aware" \
            --hours 24 --out results/compare
cpc report results/compare            # 生成 comparison.png 与各组的 run.png
```

仿真输出目录中：`summary.json` 为汇总（请求数、SLO 达成率、TTFT 分位数、IT/设施用电、电费、碳排放、各站点明细），
`timeseries.csv` 为每 5 分钟的各站点功率、节点数、请求率、电价、碳强度，`decisions.jsonl` 为容量决策记录。
