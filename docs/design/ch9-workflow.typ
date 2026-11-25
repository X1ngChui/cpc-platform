#import "../template/cpc-doc.typ": *

= 仿真与实验 <ch-workflow>

== 离散事件仿真

仿真器（`cpc.sim.des.Simulator`）以仿真时钟驱动各站点的数据中心运行时，事件如下：

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("事件", "处理"),
    [请求到达], [按负载 trace 顺序流式读取（不预先放入事件队列）。构造集群视图，调用路由策略；选中站点后经单程网络时延（RTT 的一半）到达站点。],
    [到达站点], [提交给该站点的数据中心运行时；被拒绝时记为 rejected。],
    [开始服务], [运行时回调。按服务器性能模型计算预填充时间、token 间隔与服务时间，记录首 token 时延与端到端时延（含往返时延），安排完成事件。],
    [服务完成], [通知运行时，释放并发槽并从队首派发。],
    [启动完成], [运行时发出开机动作时安排在 `boot_until` 时刻；启动期间被取消又重新开机时，旧事件失效。],
    [控制周期], [每 `scheduler.sim_control_interval_s` 一次：更新各站点到达率；容量策略所需输入齐全时调用 `decide()`，执行指令并更新路由权重，否则跳过并计数。],
  ),
  caption: [仿真事件],
)

电网信号每仿真分钟刷新一次。仿真时长须不超过负载变换后的覆盖范围，否则报错。全部服务器（包括场景中的真实服务器池）按服务器模型仿真。

== 实验

实验由以下服务进程组成，各进程从电网信号服务同步信号时钟：

- *电网信号服务*（`cpc grid`）：加载电网信号数据集，提供信号与需求响应事件接口；
- *站点代理*（`cpc site`，每个站点一个）：以真实时间驱动数据中心运行时；仿真服务器按模型计时，真实服务器转发给推理实例，
  功率由传感器采样；执行器钩子负责容器启停、功率上限设置与就绪检查；
- *网关*（`cpc gateway`）：拉取站点状态与电网信号，调用路由策略并转发请求，统计各来源区域的到达率；
- *控制器*（`cpc controller`）：周期性采集、调用容量策略，向站点代理下发指令，向网关下发路由权重；
- *负载生成器*（`cpc loadgen`）：按负载数据集与场景变换回放请求，来源区域分配与仿真一致，请求扩展属性以请求头发送。

*实验运行（`cpc live`）。* 一次实验的步骤为：启动服务（未给出地址文件时在本进程中启动，与 `cpc up` 相同；给出地址文件时连接
已部署的服务）→ 等待全部服务就绪 → 回放负载，同时按采样周期读取各站点的状态摘要、电网信号与控制器的决策 → 写出结果数据包
（@sec-results）。逐请求记录来自负载生成器：HTTP 200 记为 served，503 记为 rejected，其他状态与连接失败记为 failed；
各站点的电量取站点累计能耗的差分，与仿真的结果口径一致，可以直接比较。

`cpc up` 在一个进程中启动除负载生成器外的全部服务并持续运行，用于手动操作与演示；分布部署见《使用手册》。

== 实验描述

实验描述（YAML）给出一个场景与若干变体，每个变体覆盖策略或负载参数：

```yaml
name: policies
scenario: ../../configs/scenario.yaml     # 相对于实验描述文件
hours: 24                                 # 仿真时长
bin_s: 300                                # 时间序列的时间片
variants:
  - name: nearest-static
    routing:  {policy: nearest, params: {max_wait_s: 0.5}}
    capacity: {policy: static}
  - name: weighted-gridaware
    routing:  {policy: weighted}
    capacity: {policy: grid_aware}
    workload: {rate_scale: 1.5}           # 覆盖场景 workload 的字段
```

`cpc run <实验描述>` 以独立进程并行运行各变体，结果写入 `<输出目录>/<变体名>/`，对比表写入 `<输出目录>/comparison.csv`。
`cpc sim` 运行单个仿真，命令行参数可覆盖场景中的策略与负载参数。Python 中可用 `cpc.simulate()` 运行仿真，
`cpc.read_run()` 读取结果目录。

== 结果数据包 <sec-results>

每次仿真运行的结果写为一个目录，并以 `datapackage.yaml` 描述（`cpc.kind` 为 `run_result`）：

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("文件", "内容"),
    [`run.yaml`], [运行描述：平台版本、创建时间、变体名、运行方式、仿真时长、时间片、路由与容量策略及参数、展开后的完整场景],
    [`summary.json`], [汇总指标（下表）],
    [`requests.parquet`], [逐请求记录：`arrival_s`、`source`、`site`、`server`、`status`（served / rejected / unrouted / unfinished）、`route_reason`、`input_tokens`、`output_tokens`、`queue_s`、`ttft_s`、`e2e_s`、`tpot_s`、`slo_ok`],
    [`timeseries.csv`], [按时间片的各站点 IT 功率（kW）、非关机服务器数（时间平均）、到达率、电价、碳强度、功率上限],
    [`decisions.jsonl`], [容量策略的逐周期决策：时间、各站点指令、说明信息],
  ),
  caption: [结果数据包],
)

#figure(
  tbl(
    columns: (12em, 1fr),
    header: ("汇总指标", "定义"),
    [`arrived`、`served`、`rejected`、`unrouted`、`unfinished`], [到达请求数；完成服务数；站点拒绝数；路由策略拒绝数；仿真结束时仍在排队或在途的请求数（守恒：前者等于后四者之和）],
    [`slo_attainment`], [完成服务的请求中 TTFT 与 TPOT 均满足 SLO 的比例],
    [`ttft_p50_s`、`ttft_p99_s`、`e2e_p50_s`], [首 token 时延与端到端时延的分位数（含往返时延）],
    [`it_kwh`、`facility_kwh`], [IT 电量与设施电量],
    [`cost`、`currency`、`carbon_kg`], [电费（电价数据集的货币单位）与碳排放；只对信号可用的时间片核算],
    [`signal_coverage`], [有电网信号的设施电量占比],
    [`wh_per_request`], [每个完成请求的设施电量],
    [`sites`], [各站点的电量、电费、碳排放与完成请求数],
    [`route_reasons`、`skipped_cycles`], [路由决策原因的计数；跳过的控制周期及原因],
    [`workload`、`grid`], [负载数据集与变换参数、电网信号来源（`名称@版本:信号`）],
  ),
  caption: [汇总指标],
)

`cpc report <结果目录>` 由时间序列与对比表作图（需安装 `analysis` 可选依赖）。仿真结果的状态为 served、rejected、unrouted
（路由策略拒绝）与 unfinished（结束时仍在排队或在途）；实验结果的状态为 served、rejected 与 failed。

== 可复现性

运行结果由以下因素唯一确定：场景（含数据集的名称与版本）、策略及其参数、仿真时长与时间片、随机种子
（`workload.seed` 决定负载缩放与来源分配；`weighted` 路由策略的 `seed` 参数），以及平台版本。
`run.yaml` 记录全部这些信息；数据集的描述文件记录原始数据来源与转换参数。
