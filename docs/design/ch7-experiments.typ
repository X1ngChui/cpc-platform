#import "../template/cpc-doc.typ": *

= 实验支撑 <ch-exp>

== 负载生成

负载生成器 `cpc loadgen` 与离散事件仿真器使用同一个负载数据集（场景 `workload`，@ch-data）：变换后 trace 的相对时间即请求的
发送时刻（真实秒），来源区域按 `share` 比例分配，输入、输出 token 数取自 trace。时间窗、时间压缩与到达率缩放由场景给出，
也可在命令行用 `--start-s`、`--duration-s`、`--time-scale`、`--rate-scale` 显式覆盖。

请求体带 `max_tokens` 与 `ignore_eos: true`，使真实模型的输出长度与设定一致，这是推理压测的通行做法。
负载生成器默认使用流式请求，记录 TTFT 与端到端时延，按 SLO 统计达成率。

== 离散事件仿真器

`cpc sim` 不启动任何服务，以仿真时间运行场景：生成到达事件 → 调用路由策略 → 经 RTT/2 到达站点 → 占用并发槽或排队 →
按性能模型完成 → 释放并派发排队请求；每个控制周期调用容量策略并按节点状态机执行；功率分段恒定，能耗按 5 分钟时间片累计，
再乘以 PUE、电价、碳排放因子得到成本与碳排放。24 小时、约 100 万个请求的默认场景在普通笔记本上约 45 秒完成。

`cpc compare` 用多进程并行运行多组"路由策略 : 容量策略"组合并输出对比表；`cpc report` 根据结果作图。

#note[仿真器与在线平台共用策略、功率/性能模型与电网信号代码，但站点内部的排队实现是两套（仿真器用事件队列，站点代理用 asyncio）。
二者的状态机规则保持一致；修改其中一处时，请同步修改另一处并运行测试。]

== 日志与可视化

- 网关请求日志（JSONL）：每个请求一行，含来源、站点、节点、时延、token 数、请求时刻的电价与碳强度、SLO 是否达成；
- 控制器决策日志（JSONL）：每个控制周期一行，含目标、执行结果、观测状态、电网信号与需求；
- 仿真输出：`summary.json`（汇总）、`timeseries.csv`（每 5 分钟各站点功率、节点数、请求率、电价、碳强度、功率上限）、`decisions.jsonl`；
- Grafana 仪表盘"算电协同调度平台"：设施功率、用电成本速率、碳排放速率、请求速率、SLO 达成率、各站点功率 / 电价 / 节点数 / 碳强度 /
  请求率 / TTFT P99 / 需求响应余量 / 每 token 能耗 / 排队数 / GPU 功率。仪表盘由 `deploy/grafana/build_dashboard.py` 生成，站点颜色固定。

== 平台功能演示

以下结果用于说明平台的数据通路与可观测性，*不是*调度算法的研究结论：参考策略未经优化，节点参数中空载、主机与预填充部分为示意值。

=== 仿真：四组策略组合

默认场景：4 个站点共 10 台 8×H100 服务器（节点参数由 ML.ENERGY Benchmark v3 标定，@sec-calibration），负载为 Azure LLM Inference
Trace 2024 对话服务中 2024-05-14 16:00 UTC 起的 24 小时（457.9 万个请求，平均约 53 请求/秒），2026 年 7 月 15 日分时电价与
2023 年省级年均排放因子，广东站点 15–17 时需求响应上限 8 kW。24 小时仿真结果如下（`python scripts/make_figures.py` 可复现）：

#figure(
  tbl(
    columns: (11em, 1fr, 1fr, 1fr, 1fr),
    align: (left, right, right, right, right),
    header: ("路由 + 容量策略", "电费（元）", "设施用电（kWh）", "碳排放（kg）", "SLO 达成率"),
    [nearest + static], [672.5], [1105.6], [622.4], [100.0%],
    [least_loaded + reactive], [446.2], [764.0], [426.9], [100.0%],
    [cost_aware + reactive], [321.2], [754.2], [440.0], [100.0%],
    [weighted + grid_aware], [410.0], [711.3], [399.6], [100.0%],
  ),
  caption: [默认场景 24 小时仿真（457.9 万个请求）],
)

#figure(image("../figures/comparison.png", width: 100%), caption: [四组策略组合的电费、设施用电量、碳排放与 SLO 达成率])

可以读出平台能够反映的几类效应：全部节点常开（static）时空载功率占比高，用电量最大；按负载伸缩（reactive）显著减少空载能耗；
成本感知路由把请求引向低价站点，电费最低，但低价站点（蒙西）的碳排放因子较高，碳排放高于按负载均衡的组合；
按"等效电价 × PUE"集中分配容量的 grid_aware 用电量最少。成本与碳排放两个目标并不总是一致，这正是算电协同研究需要权衡的地方。

#figure(image("../figures/run-weighted__grid_aware.png", width: 92%),
  caption: [weighted + grid_aware 组合的 24 小时运行曲线：夜间负载集中在贵州（低谷电价），午后转向蒙西（深谷电价），22 时后部分转向上海（低谷电价）])

=== 公开数据集检验

`python scripts/dataset_check.py` 以默认场景为基础，逐一替换负载数据集，运行两组策略；另以美国 EIA 逐时碳强度场景
（`configs/scenario.us-eia.yaml`）运行同样两组。结果如下（全部请求均被服务，信号覆盖率均为 1.0）：

#figure(
  tbl(
    columns: (13em, 9em, 1fr, 1fr, 1fr, 1fr),
    align: (left, left, right, right, right, right),
    header: ("负载", "策略", "请求数", "SLO 达成率", "TTFT P99（s）", "设施用电（kWh）"),
    [azure-llm-2024-conv（1 天）], [nearest + static], [4 578 859], [100.0%], [1.40], [1105.6],
    [], [cost_aware + reactive], [], [100.0%], [1.43], [754.2],
    [azure-llm-2024-code（1 天）], [nearest + static], [1 640 533], [100.0%], [1.58], [866.5],
    [], [cost_aware + reactive], [], [100.0%], [1.61], [264.0],
    [azure-llm-2023-conv（1 小时）], [nearest + static], [19 029], [99.99%], [0.87], [36.5],
    [azure-llm-2023-code（1 小时）], [nearest + static], [8 576], [100.0%], [1.53], [20.3],
    [burstgpt-1（第 10 天）], [nearest + static], [149 265], [100.0%], [0.65], [536.5],
    [], [cost_aware + reactive], [], [100.0%], [0.68], [202.1],
    [mooncake-fast25-conversation（1 小时）], [nearest + static], [11 885], [60.6%], [17.1], [38.1],
    [mooncake-fast25-toolagent（1 小时）], [nearest + static], [23 321], [81.7%], [12.3], [38.4],
    [美国 EIA 场景（1 天）], [nearest + static], [4 778 610], [100.0%], [1.39], [1307.7],
    [], [cost_aware + reactive], [], [100.0%], [1.43], [733.5],
  ),
  caption: [公开负载数据集上的仿真检验（完整结果见 `results/dataset-check/summary.md`）],
)

Mooncake trace 的平均输入长度为 8 000–12 000 token，按场景的预填充吞吐（示意值 5 000 token/s）计算，单次预填充即需 2 s 左右，
首 token 时延显著上升、SLO 达成率下降；这说明预填充吞吐对长上下文负载的结果有决定性影响，需要实测标定。
美国场景中，三个平衡区的逐时碳强度来自 EIA-930 发电结构，cost_aware + reactive 的碳排放为 235.8 kg（nearest + static 为 399.0 kg）。

=== 虚拟化部署

在一台笔记本（Windows 11，Docker Desktop）上分别以单进程、Docker Compose、kind（1 个控制面 + 4 个 worker，
每个 worker 带一个站点的 region 标签）三种方式部署默认场景，均通过冒烟测试 `scripts/smoke_test.py`：
全部服务健康；压测请求全部成功；控制器指令执行成功；站点导出 DCGM 兼容功率指标与设施功率指标；
电网信号服务导出电价与碳强度；向某站点注入需求响应事件后，控制器在一个周期内把该站点在线节点降到功率上限允许的数量，
设施功率不超过上限。

=== 真实 GPU 站点 <sec-realgpu>

在同一台笔记本的 NVIDIA GeForce RTX 5070 Ti 上运行 vLLM（v0.34.0，模型 Qwen2.5-0.5B-Instruct），场景
`configs/scenario.local-gpu.yaml` 把它配置为真实站点（`backend: openai`，功率传感器为 NVML，配置了容器名以便启停）。
`scripts/real_gpu_check.py` 以 6 请求/秒压测 60 秒，然后通过容量指令关停、重启推理实例：

#figure(image("../figures/real_gpu_power.png", width: 95%), caption: [真实 GPU 站点的实测功率轨迹（每秒采样站点快照）])

178 个请求全部成功（流式 TTFT 中位数 16 ms）；GPU 功率空载约 42.5 W，压测期间平均约 105 W、峰值约 155 W；
容量指令通过 Docker API 停止 vLLM 容器后，GPU 仍保持约 43 W 的空载功率（见 @sec-states 的说明）；重新开机后约 33 秒通过就绪检查恢复服务。
这一测试验证了真实后端的完整链路：请求转发与流式输出、就绪检查、NVML 实测功率与能耗计数、容器级开关机。
