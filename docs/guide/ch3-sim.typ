#import "../template/cpc-doc.typ": *

= 仿真 <g-sim>

== 编写场景

场景文件的结构见《平台设计说明书》"场景与模型"一章。以默认场景 `configs/scenario.yaml` 为起点，常改的部分：

- 站点：`sites[]` 的位置、PUE、电网区域 `region` 与服务器池 `pools`；异构站点可配置站内调度 `scheduler.dispatch`、`scheduler.power`；
- 服务器类型：`node_profiles`（参数式或查表式，标定方法见 @g-data）；
- 日期与信号：`clock.start` 决定季节与分时时段，须在电网信号数据的覆盖范围内；需求响应事件 `grid.dr_events[]`；
- 负载：`workload.dataset`、时间窗 `start_s` 与 `duration_s`、`rate_scale`、请求属性 `attributes`；
- 全局策略与参数：`scheduler.routing`、`scheduler.capacity`；碳价 `scheduler.carbon_price_per_kg`（与电价同一货币）。

```bash
# 某日 24 小时各站点的电价、时段标签与碳强度
cpc signals -s configs/scenario.yaml --date 2026-07-15
cpc rtt -s configs/scenario.yaml                          # 来源区域到站点的 RTT 矩阵
```

== 单次仿真

```bash
cpc sim -s configs/scenario.yaml --hours 24 --out results/base
cpc sim -s configs/scenario.yaml --routing cost_aware \
        --capacity reactive --capacity-params '{"target_util": 0.7}' \
        --hours 24 --rate-scale 1.5 --out results/cost-1.5x
```

`--routing`、`--capacity` 覆盖场景中的策略（参数由 `--routing-params`、`--capacity-params` 以 JSON 给出）；
`--start-s`、`--duration-s`、`--time-scale`、`--rate-scale` 覆盖负载变换参数；`--bin-s` 设定时间序列的时间片（缺省 300 秒）。
仿真时长不得超过负载变换后的覆盖范围。命令结束时打印汇总指标。

== 实验描述与多变体运行

比较多组策略或参数时，编写实验描述：

```yaml
# examples/experiments/policies.yaml
name: policies
scenario: ../../configs/scenario.yaml
hours: 24
variants:
  - name: nearest-static
    routing:  {policy: nearest, params: {max_wait_s: 0.5}}
    capacity: {policy: static}
  - name: cost-reactive
    routing:  {policy: cost_aware}
    capacity: {policy: reactive, params: {target_util: 0.6}}
  - name: weighted-gridaware
    routing:  {policy: weighted}
    capacity: {policy: grid_aware}
```

```bash
cpc run examples/experiments/policies.yaml --jobs 4           # 结果写入 results/policies/
cpc compare -s configs/scenario.yaml --runs "nearest:static,cost_aware:reactive" \
            --hours 24 --out results/cmp
```

各变体以独立进程并行运行，`--jobs` 限制并行数（每个变体加载一次负载数据，内存占用随负载规模增长）。
变体的 `workload` 可覆盖负载字段（包括 `dataset`），用于在不同负载上比较同一策略。`cpc compare` 是只覆盖策略名的简写形式，
策略参数取场景中的配置（策略名与场景不同时为空）。

== 结果

每个变体的结果目录是一个结果数据包：`run.yaml`（场景、策略与参数、数据集、平台版本）、`summary.json`（汇总指标）、
`requests.parquet`（逐请求记录）、`timeseries.csv`（各站点按时间片的功率、服务器数、到达率与信号）、`decisions.jsonl`（容量决策）。
对比表 `comparison.csv` 每行一个变体。字段定义见《平台设计说明书》"仿真与实验"一章。

```python
import pyarrow.parquet as pq

req = pq.read_table("results/policies/cost-reactive/requests.parquet").to_pandas()
served = req[req.status == "served"]
print(served.groupby("site").ttft_s.quantile(0.99))     # 各站点 TTFT 的 P99
print(served.groupby("source").slo_ok.mean())           # 各来源区域的 SLO 达成率
```

```bash
# comparison.png 与各变体的 run.png（需 analysis 可选依赖）
cpc report -s configs/scenario.yaml results/policies
```

汇总中的 `signal_coverage` 小于 1 表示部分时间片没有电网信号，这些时间片的电费与碳排放未计入；
`skipped_cycles` 记录因输入不完整而跳过的控制周期；`route_reasons` 给出路由决策原因的分布（如溢出次数）。
