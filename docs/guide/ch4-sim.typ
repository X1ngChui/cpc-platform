#import "../template/cpc-doc.typ": *

= 仿真 <g-sim>

仿真在一个进程中按事件推进时间：请求到达、经网络时延到达站点、排队、在服务器上处理、完成；容量策略按决策周期开关服务器。
仿真与实验使用同一套服务器模型、数据中心内部规则与调度策略，因此仿真中比较出的策略差异可以在实验中复现。

== 运行一次仿真：cpc sim

```bash
cpc sim -s configs/scenario.yaml --hours 24 --out results/base
```

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ("参数", "含义"),
    [`-s`、`--scenario`], [场景文件，缺省 `configs/scenario.yaml`],
    [`--hours`], [仿真时长（小时，缺省 24），不得超过负载的长度],
    [`--routing`、`--routing-params`], [覆盖场景中的路由策略及其参数（参数为 JSON，如 `'{"max_wait_s": 1}'`）],
    [`--capacity`、`--capacity-params`], [覆盖场景中的容量策略及其参数],
    [`--start-s`、`--duration-s`、`--time-scale`、`--rate-scale`], [覆盖场景中的负载时间窗、时间压缩与到达率缩放],
    [`--bin-s`], [时间序列中每行的时长（秒，缺省 300）],
    [`--out`], [结果目录，缺省 `results/sim-<路由策略>__<容量策略>`],
  ),
  caption: [cpc sim 的参数],
)

例如，把到达率提高到 1.5 倍、改用成本感知路由：

```bash
cpc sim -s configs/scenario.yaml --routing cost_aware --rate-scale 1.5 \
    --out results/cost-1.5x
```

== 比较多组策略：cpc run

把要比较的变体写在实验描述文件中：

```yaml
name: policies                         # 实验名，缺省输出目录为 results/<实验名>
scenario: ../../configs/scenario.yaml  # 场景文件，相对于本文件
hours: 24                              # 仿真时长（小时）
bin_s: 300                             # 时间序列每行的时长（秒，缺省 300）
variants:
  - name: nearest-static               # 变体名，也是结果子目录名
    routing:  {policy: nearest, params: {max_wait_s: 0.5}}
    capacity: {policy: static}
  - name: weighted-gridaware
    routing:  {policy: weighted}
    capacity: {policy: grid_aware}
  - name: cost-heavy-load
    routing:  {policy: cost_aware}
    workload: {rate_scale: 1.5}        # 覆盖场景 workload 中的字段（包括 dataset）
```

变体中省略的 `routing`、`capacity` 取场景中的设置。运行：

```bash
cpc run examples/experiments/policies.yaml --jobs 4
```

各变体以独立进程并行运行（`--jobs` 限制同时运行的数量；每个进程各自加载负载数据，负载很大时可适当减少），
结束后打印对比表并写出 `results/<实验名>/comparison.csv`。`examples/experiments/` 中另有美国电网场景的示例。

== 结果目录

每次运行的结果目录包含以下文件（`cpc live` 的结果格式相同）。

*summary.json*：汇总指标。

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("字段", "含义"),
    [`arrived`], [到达的请求数],
    [`served`、`rejected`、`unrouted`、`unfinished`], [完成服务的请求数；被站点拒绝（队列已满）的；被路由策略拒绝的；仿真结束时尚未完成的（实验中另有 `failed`：连接失败或其他错误）],
    [`slo_attainment`], [完成的请求中首 token 时延与 token 间隔都满足 SLO 的比例],
    [`ttft_p50_s`、`ttft_p99_s`、`e2e_p50_s`], [首 token 时延的中位数与 99 分位数、端到端时延的中位数（秒，含网络往返时延）],
    [`it_kwh`、`facility_kwh`], [IT 电量与设施电量（kWh）],
    [`cost`、`currency`、`carbon_kg`], [电费（货币单位见 `currency`）与碳排放（kg）],
    [`wh_per_request`], [每个完成的请求消耗的设施电量（Wh）],
    [`signal_coverage`], [有电网信号的设施电量占比；小于 1 时，缺少信号的部分未计入电费与碳排放],
    [`sites`], [各站点的电量、电费、碳排放与完成的请求数],
    [`route_reasons`], [路由决策原因的计数，例如 `nearest`、`overflow_wait_exceeded`（就近站点排队过长而转向其他站点）],
    [`skipped_cycles`], [因输入不完整（如缺少电网信号）而跳过的容量决策周期],
    [`workload`、`grid`], [所用负载（名称 \@ 版本、时间窗、缩放）与电网信号数据集],
  ),
  caption: [汇总指标],
)

*requests.parquet*：每个请求一行，字段为 `arrival_s`（到达时间，秒）、`source`、`site`、`server`、`status`、`route_reason`、
`input_tokens`、`output_tokens`、`queue_s`（站内排队时间）、`ttft_s`、`e2e_s`、`tpot_s`、`slo_ok`。

*timeseries.csv*：每个时间片一行。`t_h` 为开始时刻（小时），`time` 为对应的信号时间；对每个站点 `<站点>` 有
`<站点>_it_kw`（平均 IT 功率）、`<站点>_nodes`（开启的服务器数）、`<站点>_rps`（到达率）、`<站点>_price`、
`<站点>_carbon`、`<站点>_cap_kw`（功率上限，没有上限时为空）。

*decisions.jsonl*：容量策略每个周期一行：时间、各站点的指令（`active_nodes`：开启的服务器数，`cap_ratio`：功率封顶比例）与说明。

*run.yaml*：运行方式、策略及参数、仿真时长、平台版本与展开后的完整场景。

== 在 Python 中分析结果

```python
import cpc

run = cpc.read_run("results/policies/weighted-gridaware")
s = run["summary"]
print(s["facility_kwh"], s["cost"], s["currency"], s["slo_attainment"])

req = run["requests"].to_pandas()        # pyarrow 表转为 pandas（需安装 pandas）
served = req[req.status == "served"]
print(served.groupby("site").ttft_s.quantile(0.99))      # 各站点首 token 时延的 P99
print(served.groupby("source").slo_ok.mean())            # 各来源区域的 SLO 达成率
```

也可以在 Python 中直接运行仿真，便于批量扫描参数：

```python
import cpc

for scale in (0.5, 1.0, 1.5):
    res = cpc.simulate("configs/scenario.yaml", routing="cost_aware", hours=6,
                       rate_scale=scale)
    print(scale, res.summary["facility_kwh"], res.summary["slo_attainment"])
```

`cpc.simulate` 的关键字参数：`routing`、`routing_params`、`capacity`、`capacity_params`、`hours`、`bin_s`、`out`
（给出时同时写出结果目录），其余关键字参数覆盖场景 `workload` 中的同名字段。

== 作图

```bash
pip install -e ".[analysis]"          # 首次使用时安装 matplotlib
cpc report results/policies           # 对比图 comparison.png 与各变体的 run.png
cpc report results/base               # 单次运行
```

`run.png` 为各站点 IT 功率、开启的服务器数与电价随时间的变化；`comparison.png` 为各变体的电费、设施电量、碳排放与 SLO 达成率。

== 结果的可复现性

相同的场景（含数据集的名称与版本）、策略与参数、仿真时长与随机种子，得到相同的结果。`workload.seed` 决定到达率缩放与
来源区域的分配；改变种子可以评估随机性的影响。`run.yaml` 记录了复现所需的全部信息。
