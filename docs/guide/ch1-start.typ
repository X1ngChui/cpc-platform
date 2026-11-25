#import "../template/cpc-doc.typ": *

= 概述 <g-overview>

== 平台的用途

平台用于研究跨数据中心的 AI 推理调度如何响应电网信号。典型的问题是：多个地域的数据中心同时为用户提供大模型推理服务，
各地的电价、电力碳排放强度不同并随时间变化，电网有时还要求某个数据中心在一段时间内限制用电；
应当把每个请求送到哪个数据中心，每个数据中心应当开启多少台服务器，才能在满足响应时间要求的前提下降低电费与碳排放。

平台把这类问题拆成可以替换的部分：数据（请求负载、电价与碳强度、服务器的功率特性）、实验对象的描述（场景）、调度策略，
以及运行这些部分的两种方式（仿真与实验）。研究者通常只需要准备数据、编写场景、编写或选择策略，然后比较结果。

== 基本概念

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("概念", "含义"),
    [站点], [一个数据中心，位于某个地理位置，接入某个电网区域，有自己的 PUE（设施总功率与 IT 功率之比）。],
    [服务器], [站点内处理推理请求的单元。仿真服务器的功率与速度由服务器类型的表给出；真实服务器是一个实际运行的推理实例（如 vLLM），功率由传感器测量。],
    [服务器类型], [一种服务器的功率与速度特性：功率、token 间隔随占用率变化的表，以及并发槽数、预填充吞吐、开机时间。],
    [服务器池], [站点中同一服务器类型的一组服务器。一个站点可以有多个池（例如新旧两代服务器）。],
    [并发槽、占用率], [一台服务器可以同时处理的请求数上限称为并发槽数；占用率 = 正在处理的请求数 / 并发槽数。],
    [来源区域], [用户请求的来源地。请求从来源区域到站点要经过网络往返时延（RTT）。],
    [负载], [请求序列：每个请求的到达时间、输入 token 数与输出 token 数，通常来自公开的服务日志（trace）。],
    [电网信号], [各电网区域随时间变化的电价、碳强度等；需求响应事件要求某个站点在一段时间内的设施功率不超过上限。],
    [全局策略], [在数据中心之间做决策：路由策略为每个请求选择站点；容量策略周期性地决定各站点开启多少台服务器。],
    [本地策略], [在数据中心内部做决策：派发策略为到达站点的请求选择服务器；电源管理策略决定开关哪几台服务器。],
    [仿真], [在一个进程中按事件推进时间，几秒到几分钟即可完成数小时到一天的运行，用于比较策略。],
    [实验], [各服务以真实时间运行，请求经真实的网络接口处理，可以接入真实的推理实例与功率传感器。],
    [结果数据包], [一次运行（仿真或实验）的结果目录：逐请求记录、时间序列、汇总指标、容量决策与运行描述。],
  ),
  caption: [基本概念],
)

== 工作流程

1. *准备数据*：把原始数据整理为平台格式的数据集（@g-data），或使用现成的转换脚本；
2. *编写场景*：在场景文件中描述站点、服务器、电网信号、负载与策略，用 `cpc check` 检查（@g-scenario）；
3. *仿真比较*：用 `cpc sim` 或 `cpc run` 运行仿真，比较不同策略的电费、碳排放与服务质量（@g-sim）；
4. *实验验证*：用 `cpc live` 以真实时间运行同一场景，可接入真实服务器（@g-live）；
5. *编写策略*：把自己的调度算法写成策略，在仿真与实验中使用（@g-policy）。

= 安装 <g-setup>

== 运行环境

平台的服务运行在 Linux（x86_64 或 aarch64）上。在 Windows 上可以使用 WSL2 中的 Ubuntu：此时仓库应放在 Linux 文件系统内
（例如 `~/cpc-platform`），不放在 `/mnt/c` 等 Windows 磁盘目录下，否则文件访问很慢且权限异常。需要 Python 3.10 或更高版本
（`python3 --version` 查看）与 git。

== 获取代码

```bash
git clone https://github.com/X1ngChui/cpc-platform.git
cd cpc-platform
```

以下所有命令都在仓库根目录 `cpc-platform/` 下执行。

== 创建 Python 环境并安装

平台安装在一个独立的 Python 虚拟环境中，避免与系统中的其他 Python 包冲突：

```bash
python3 -m venv .venv          # 在仓库目录下创建虚拟环境 .venv
. .venv/bin/activate           # 激活虚拟环境；命令行提示符前出现 (.venv)
pip install -e ".[dev]"        # 安装平台及测试、作图所需的依赖
```

每次打开新的终端，都需要先进入仓库目录并执行 `. .venv/bin/activate`。安装完成后可以使用 `cpc` 命令：

```bash
cpc --help                     # 列出全部子命令
cpc sim --help                 # 查看某个子命令的参数
```

可选依赖按需安装：`pip install -e ".[nvml]"`（读取 NVIDIA GPU 功率）、`pip install -e ".[docker]"`（通过 Docker 启停推理容器）、
`pip install -e ".[docs]"`（编译文档）。Debian / Ubuntu 的系统 Python 若提示缺少 venv 模块，先安装 `python3-venv` 软件包；
也可以使用 uv（`uv venv --python 3.12 && uv pip install -e ".[dev]"`）。

== 验证安装

```bash
pytest                                    # 运行单元测试，最后一行显示全部通过（passed）
python scripts/smoke_test.py --offline    # 启动全部服务并逐项检查，约 1 分钟
```

冒烟测试会把各服务作为独立进程启动，依次检查服务就绪、请求处理、容量决策、能耗指标、需求响应、传感器故障与站点故障的处理，
每一项输出"[通过]"，最后一行为"冒烟测试通过"。该测试使用随代码提供的测试数据，不需要下载。

= 快速上手 <g-quick>

本章用一个小规模的场景完成一次完整的流程：准备数据、检查场景、运行仿真、比较策略、查看结果、运行实验。
场景文件为 `examples/quickstart/scenario.yaml`：4 个数据中心站点（上海、广东、贵州、内蒙古）、3 个用户来源区域，
负载为 Azure LLM Inference Trace 2023 中约 1 小时的对话请求。

== 准备数据

```bash
python examples/datasets/azure_llm.py --variant 2023-conv --zero-output fail
python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01
```

第一条命令下载约 0.7 MB 的原始负载文件并转换为平台格式，第二条命令把随代码提供的各省分时电价规则与排放因子展开为
2026 年的电网信号。数据集写入 `~/.cache/cpc/datasets/`（可用环境变量 `CPC_DATA_DIR` 指定其他目录），
每个数据集是一个目录，例如：

```text
~/.cache/cpc/datasets/azure-llm-2023-conv/
├── datapackage.yaml      描述文件：名称、版本、来源、许可、字段定义
└── requests.parquet      数据：每行一个请求
```

== 检查场景

```bash
cpc check -s examples/quickstart/scenario.yaml
```

`cpc check` 读取场景与其引用的全部数据集，检查配置与数据是否有效，并打印概要。输出的主要部分：

```text
服务器类型
  名称            并发槽      空载 W      满载 W     满载 W/槽    开机 s  说明
  h100x8        128      1360      5406       42.2     180  8×H100，Llama-3.1-405B-...
  b200x4        128      1160      4450       34.8     180  4×B200，同一模型...
站点
  站点     区域                 PUE    服务器     并发槽     满载设施 kW  服务器池 / ...
  sh     shanghai          1.25      1     128         6.8  h100:h100x8×1；...
  ...
往返时延（ms，来源 → 站点）
                sh      gd      gz      nm
  east         4.5    19.8    27.4    24.1   （请求占比 0.4）
  ...
负载 azure-llm-2023-conv@7909210：19366 个请求，0.97 小时，平均 5.53 请求/秒，...
场景检查通过
```

有任何问题（字段拼写错误、数据集不存在、负载时间窗超出数据范围等）时，命令给出原因并以非零状态退出。

== 运行一次仿真

```bash
cpc sim -s examples/quickstart/scenario.yaml --hours 0.9 --out results/qs-base
```

仿真按场景中的策略（就近路由 `nearest` + 按负载开关服务器 `reactive`）运行 0.9 小时，结束后打印汇总：

```text
仿真完成（0.58 秒）：路由 nearest，容量 reactive，0.9 小时
  请求：到达请求 18356，完成 18356，站点拒绝 0，未路由 0，结束时未完成 0
  SLO 达成率 99.99%；首 token 时延 P50 0.199 s，P99 0.871 s
  设施电量 14.004 kWh（IT 11.449 kWh），电费 11.95 CNY，碳排放 8.07 kg，每请求 0.76 Wh
结果已写入 results/qs-base/
```

SLO 达成率是首 token 时延与 token 间隔都在场景规定的上限之内的请求所占比例。

== 比较多组策略

`examples/quickstart/experiment.yaml` 列出了 3 组策略（称为变体）：

```yaml
name: quickstart
scenario: scenario.yaml
hours: 0.9
bin_s: 60
variants:
  - {name: nearest-static,   routing: {policy: nearest},    capacity: {policy: static}}
  - {name: nearest-reactive, routing: {policy: nearest},    capacity: {policy: reactive}}
  - {name: cost-reactive,    routing: {policy: cost_aware}, capacity: {policy: reactive}}
```

```bash
cpc run examples/quickstart/experiment.yaml
```

各变体并行运行，结束后打印对比表：

```text
  变体                 完成   拒绝      SLO  TTFT P99  设施 kWh  电费 CNY  碳 kg
  nearest-static      18356      0   99.99%     0.865      17.9      15.7   10.0
  nearest-reactive    18356      0   99.99%     0.871      14.0      12.0    8.1
  cost-reactive       18356      0  100.00%     0.589       8.2       4.8    5.0
```

`static` 让全部服务器常开；`reactive` 按到达率关闭多余的服务器，降低了空载能耗；`cost_aware` 把请求送到等效电价
（电价 + 碳价 × 碳强度）较低的站点，进一步降低了电费与碳排放。

== 查看结果

每个变体的结果是一个目录（结果数据包），例如 `results/quickstart/cost-reactive/`：

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("文件", "内容"),
    [`summary.json`], [汇总指标：请求数、SLO 达成率、时延分位数、电量、电费、碳排放及各站点的明细],
    [`requests.parquet`], [逐请求记录：到达时间、来源、站点、服务器、状态、时延等],
    [`timeseries.csv`], [每个时间片一行：各站点的 IT 功率、开启的服务器数、到达率、电价、碳强度],
    [`decisions.jsonl`], [容量策略每个控制周期的决策],
    [`run.yaml`], [运行描述：完整的场景、策略与参数、平台版本],
  ),
  caption: [结果目录中的文件],
)

在 Python 中读取：

```python
import cpc

run = cpc.read_run("results/quickstart/cost-reactive")
print(run["summary"]["facility_kwh"], run["summary"]["cost"])
df = run["requests"].to_pandas()                         # 需要 pandas
print(df[df.status == "served"].groupby("site").ttft_s.describe())
```

作图（各站点功率、服务器数与电价随时间的变化，以及对比柱状图）：

```bash
cpc report results/quickstart        # 生成 comparison.png 与各变体目录中的 run.png
```

== 运行一次实验

实验以真实时间运行同一个场景：在本机启动电网信号服务、各站点代理、网关与控制器，按负载的到达时间发送真实的 HTTP 请求。
站点中的服务器是仿真服务器，按服务器类型的表计时并计算功率，因此不需要 GPU。

```bash
cpc live -s examples/quickstart/scenario.yaml --duration 120 --rate-scale 0.2 \
    --out results/qs-live
```

`--duration 120` 表示运行 120 秒；`--rate-scale 0.2` 把到达率降为原来的 20%，减轻本机的负担。命令结束后打印与仿真相同形式的
汇总，并写出相同格式的结果目录，可以用同样的方法读取与作图。接入真实 GPU 服务器的方法见 @g-real。
