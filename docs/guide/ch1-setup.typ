#import "../template/cpc-doc.typ": *

= 概述 <g-overview>

平台用于开展算电协同的跨数据中心 AI 推理调度研究，有两种运行方式：

- *仿真*：`cpc sim`、`cpc run` 以离散事件仿真运行一个场景，数十秒至数分钟完成 24 小时的运行，用于比较调度策略；
- *实验*：电网信号服务、站点代理、网关、控制器等服务进程以真实时间运行；站点中的服务器可以是仿真服务器
  （由服务器模型给出功率与时延），也可以是真实推理实例（如 vLLM），功率由传感器测量，两者可在同一场景中共存。

两种方式使用同一场景文件、同一服务器模型与同一套策略实现。典型的工作流程为：准备数据集（@g-data）→ 编写场景 →
在仿真中比较策略（@g-sim）→ 部署实验系统并接入真实服务器验证（@g-deploy）。

#figure(
  tbl(
    columns: (9em, 1fr, 6em),
    header: ("部署方式", "适用情形", "章节"),
    [本机单进程], [开发调试与功能演示；不需要 Docker], [@g-local],
    [Docker Compose], [单机完整环境，含 Prometheus 与 Grafana], [@g-compose],
    [Kubernetes（kind）], [Kubernetes 原生部署；每个站点固定在带 region 标签的节点上], [@g-kind],
    [云服务器], [接入真实 GPU 服务器], [@g-cloud],
  ),
  caption: [实验系统的部署方式],
)

= 安装 <g-setup>

== 运行环境

服务端组件（电网信号服务、站点代理、路由网关、容量控制器、通用功率导出器）只面向 Linux（x86_64、aarch64），
容器镜像同样提供这两种架构。开发环境为 Linux 或 WSL2（Ubuntu）；在 WSL2 中，仓库应位于 Linux 文件系统内（如 `~/`），
不放在 `/mnt/*` 下，以避免跨文件系统访问的性能损失与文件权限问题。

== 获取代码与镜像

```bash
git clone https://github.com/X1ngChui/cpc-platform.git
cd cpc-platform
```

全部服务打包在同一个容器镜像中（amd64 与 arm64）：GitHub Container Registry 的 `ghcr.io/x1ngchui/cpc-platform`
（推送 `main` 与版本标签时自动构建），以及 Docker Hub 的 `xingchui/cpc-platform`。也可在仓库根目录以
`docker build -t cpc-platform:latest .` 构建，构建参数 `PIP_INDEX_URL` 可指定 PyPI 镜像。

== Python 环境

需要 Python 3.10 及以上。在仓库根目录创建虚拟环境并以可编辑模式安装：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # 平台 + 测试与作图依赖
pip install -e ".[nvml,docker]"  # 可选：NVIDIA GPU 功率采集；通过 Docker 启停推理容器
pip install -e ".[docs]"         # 可选：编译文档（Typst）
```

Debian / Ubuntu 的系统 Python 需要先安装 `python3-venv`；也可以使用 uv（`uv venv --python 3.12 && uv pip install -e ".[dev]"`）。
安装后即可使用命令 `cpc`（等价于 `python -m cpc`），`cpc <子命令> --help` 给出各子命令的参数。

== Docker 与 Kubernetes 工具（可选）

Docker Compose 与 kind 部署需要 Docker Engine 与 Compose 插件；kind 部署运行 5 个节点容器，Docker 可用内存宜不少于 8 GB。
kind（Kubernetes in Docker）与 kubectl 均为单个可执行文件，从各自官网下载 Linux 版本放入 `PATH` 即可。

== 验证安装

```bash
pytest                                  # 单元测试
python scripts/smoke_test.py --offline  # 端到端冒烟测试（测试场景，不需要下载数据）
```

冒烟测试把各服务作为独立进程启动，依次检查：全部服务就绪；压测请求全部成功；控制器完成决策并下发指令；能耗与电网信号指标；
需求响应事件生效；传感器故障（只有真实服务器的站点的功率计不可用）与站点故障（停止一个站点进程）被隔离并显式报告，
其余部分正常服务。输出"冒烟测试通过"即说明环境正常。准备好默认场景的数据集（@g-data）后，
`python scripts/smoke_test.py` 以默认场景运行同样的检查。
