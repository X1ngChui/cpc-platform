#import "../template/cpc-doc.typ": *

= 场景与模型 <ch-scenario>

场景文件（YAML）描述实验对象，与运行方式和部署方式无关。加载时拒绝未知字段，并检查引用关系；
相对路径相对于场景文件所在目录解析。场景的各部分如下。

#figure(
  tbl(
    columns: (9em, 1fr),
    header: ("部分", "内容"),
    [`clock`], [信号时钟起点（带时区）与实验中的加速倍数。],
    [`sources[]`], [来源区域：标识、名称、经纬度、请求量占比 `share`。],
    [`node_profiles`], [服务器类型：功率与性能模型参数（@sec-server-model）。],
    [`sites[]`], [站点：标识、名称、电网区域 `region`、经纬度、PUE、服务器池 `pools`、功率测量 `measurement`、最少在线服务器数 `min_active`、并网功率上限 `grid_cap_kw`、站内调度策略 `scheduler`。],
    [`network`], [来源区域到站点的往返时延模型与实测值覆盖；实验中是否由网关注入时延。],
    [`grid`], [电价、碳强度与扩展信号的数据集与区域对应；需求响应事件。],
    [`workload`], [负载数据集、传给策略的扩展字段、时间窗与缩放变换、随机种子。],
    [`slo`], [TTFT 与 TPOT 上限。],
    [`scheduler`], [全局路由策略与容量策略及其参数；控制周期、排队上限、重试、状态刷新等运行参数。],
  ),
  caption: [场景的组成],
)

== 站点与服务器池

站点由一个或多个服务器池组成，每个池引用一种服务器类型：

```yaml
sites:
  - id: nm
    name: 内蒙古·和林格尔
    region: inner_mongolia          # 电网信号数据集中的区域键
    lat: 40.38
    lon: 111.82
    pue: 1.20
    pools:
      - {id: h100, profile: h100x8, count: 2}                  # 仿真服务器池
      - {id: b200, profile: b200x4, count: 2}
      - id: lab                                                 # 真实服务器池
        profile: local
        backend: openai
        upstreams:
          - {id: lab-0, url: "http://10.0.0.8:8000", gpu_indices: [0],
             container: vllm-0, max_inflight: 32}
    measurement:                     # 有真实服务器池时必填
      sensors: [{type: nvml}]
      host: excluded                 # server / cpu_dram / excluded
    scheduler:                       # 站内调度（缺省：least_loaded + ordered）
      dispatch: {policy: efficient_first}
      power: {policy: efficient}
```

仿真服务器池（`backend: emulated`）给出服务器数 `count`，并发槽数取服务器类型的 `slots`。
真实服务器池（`backend: openai`）的每个 `upstreams` 项是一台真实服务器：OpenAI 兼容推理实例的地址、占用的加速卡
（`gpu_indices` 或传感器层的设备名 `devices`，二者择一；只用 CPU 的实例不填，其功率为主机部分，此时 `measurement.host`
不得为 `excluded`）、可选的容器名（用于开关机）、并发上限 `max_inflight` 与就绪检查路径。
真实服务器的功率由站点 `measurement` 中的传感器测量（@ch-energy）；其服务器类型的参数在实验中用于全局策略的容量与功率估计，
在仿真中用于模拟该服务器。

== 服务器模型 <sec-server-model>

服务器类型以两张随占用率变化的表描述服务器的功率与速度。占用率 $u = "在服务的请求数" \/ "并发槽数"$，取值 $[0, 1]$；
表中各点之间线性插值，超出表的范围时取端点值。

```yaml
node_profiles:
  h100x8:
    description: 8×H100，Llama-3.1-405B-Instruct-FP8
    slots: 128                                   # 并发槽数
    power_w: [[0, 1360], [0.0624, 4060.4], [0.2493, 4544.3],
              [0.4989, 4962.1], [0.9973, 5406.1]]
    tpot_s:  [[0.0624, 0.03406], [0.2493, 0.04039], [0.4989, 0.04689], [0.9973, 0.06527]]
    prefill_tokens_per_s: 5000                   # 预填充吞吐
    boot_s: 180                                  # 开机到可服务的时间
```

*功率。* `power_w` 给出服务器 IT 功率 $P(u)$，第一个点须为 $u = 0$，即在线而没有请求时的功率 $P_"idle"$；
$P_"max"$ 为表中的最大值。启动中的服务器按 $P_"idle"$ 计，关机的服务器按 `off_w`（缺省 0）计。
功率封顶比例为 $c$ 时，功率上限为 $P_"idle" + c dot (P_"max" - P_"idle")$，服务器功率取 $min(P(u), "上限")$。
大模型推理的功率随并发凹增长，且从空载到少量并发有明显的跃升 @patel2024characterizing；以表描述可以直接使用测量点，
不需要假设函数形式。

*速度。* 预填充时间 $T_"pre" = L_"in" \/ (R_"pre" dot s(c))$，token 间隔 $T_"pot" = T(u) \/ s(c)$，$T$ 为 `tpot_s` 表，
$s(c) = c^beta$ 为功率封顶后的相对速度（$c$ 截断到 $["cap_min_ratio", 1]$，$beta$ = `cap_perf_exponent`）。
服务时间为 $T_"pre" + L_"out" dot T_"pot"$；首 token 时延为排队时间、$T_"pre"$、一个 $T_"pot"$ 与往返时延之和。
$u$ 取请求开始服务时（含该请求）的占用率。

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ([字段（`node_profiles.<名称>`）], [含义]),
    [`description`], [说明（可选），如硬件、模型与数据来源],
    [`slots`], [并发槽数（必填）],
    [`power_w`], [功率表 \[\[占用率, 服务器 IT 功率 W\], …\]（必填），首点占用率为 0，占用率严格递增],
    [`tpot_s`], [token 间隔表 \[\[占用率, 秒\], …\]（必填），占用率严格递增],
    [`prefill_tokens_per_s`], [预填充吞吐 $R_"pre"$（必填）],
    [`boot_s`], [开机到可服务的时间（必填）],
    [`off_w`], [关机时的功率（缺省 0）。真实服务器的关机通常是停止推理实例而机器仍在运行，其值宜取实测],
    [`cap_min_ratio`、`cap_perf_exponent`], [功率封顶比例的下限（缺省 0.5；真实 GPU 宜取设备最小与最大功率上限之比，如 A30 为 100/165）；速度对封顶比例的指数 $beta$（缺省 0.4，宜实测）],
  ),
  caption: [服务器类型的字段],
)

=== 由测量数据生成 <sec-calibration>

`cpc profile` 由功率特性数据集中同一组（设备、设备数、模型）的测量生成服务器类型：每次测量以
$u = "concurrency" \/ "slots"$ 为占用率，成为 `power_w` 表的一个点（功率为测量值加 `--host-w`）与 `tpot_s` 表的一个点；
`power_w` 的首点为 `--idle-w` 给出的在线空载功率。测量只覆盖在服务期间，在线空载功率、主机功率、预填充吞吐与开机时间
须另行给出，未给出的必填项在输出中注明。表也可以按任意公式计算若干点后写入。

默认场景的 `h100x8` 由 ML.ENERGY Benchmark v3 中 8×H100 运行 Llama-3.1-405B-Instruct-FP8 的 6 次测量生成，
`b200x4` 由 4×B200 运行同一模型的测量生成；两者的在线空载功率与主机功率为示意值。

== 网络时延

来源区域到站点的往返时延 $"RTT" = "base_ms" + 2 dot d dot "route_factor" \/ "km_per_ms"$，$d$ 为两地大圆距离，
`route_factor` 为路由绕行系数，`km_per_ms` 为光纤中的传播速度（约 200 km/ms）；`network.overrides` 可按来源与站点给出实测值。
实验中，`emulate_wan` 为真时网关在转发前与返回后各注入 RTT 的一半；各服务实际分布在不同地域时应设为假。
`cpc check` 打印 RTT 矩阵。

== 电网信号与需求响应

```yaml
grid:
  price:  {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: price}
  carbon: {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: carbon_intensity}
  extras:                                   # 扩展信号：原样传给策略（GridSignal.extra）
    - {name: renewable_share, dataset: "${CPC_DATA_DIR}/my-grid", signal: renewable_share}
  dr_events:
    - {site: gd, cap_kw: 8, start: "2026-07-15T15:00:00+08:00",
       end: "2026-07-15T17:00:00+08:00"}
```

每个信号来源给出数据集、信号名与可选的站点到区域对应 `regions`（缺省为站点的 `region`）。电网信号模型
（`cpc.grid.signals.GridModel`）在信号时间 $t$ 给出站点的 `GridSignal`：电价及其时段标签、碳强度、货币单位、
功率上限（并网上限与生效的需求响应事件上限中的较小者）、PUE、碳价与扩展信号。等效电价 = 电价 + 碳价 × 碳强度
（按设施电量计），碳价 `scheduler.carbon_price_per_kg` 与电价使用同一货币。电价或碳强度不可用时该站点的信号缺失；
扩展信号不可用时其取值为 `None`。

需求响应事件抽象为"某站点在某时段内设施功率不得超过 `cap_kw`"，可在场景中登记，也可在实验运行时注入。

*信号时钟。* 仿真中信号时间 = `clock.start` + 仿真时间。实验中信号时间 = `clock.start` + 真实经过时间 × `clock.speedup`，
电网信号服务是时钟的权威来源，其他服务启动时从 `/v1/clock` 同步；加速只改变信号随时间变化的速度，
请求服务时间、开机时间与能耗积分仍按真实时间计算。

== 负载与变换

```yaml
workload:
  dataset: "${CPC_DATA_DIR}/azure-llm-2024-conv"
  attributes: [session_id]   # 传给策略的扩展字段（须在数据集中声明）
  start_s: 230400            # 时间窗起点（原始 trace 秒）
  duration_s: 86400          # 时间窗长度
  time_scale: 1.0            # 时间压缩：变换后时间 = (t − start_s) / time_scale
  rate_scale: 1.0            # 到达率缩放
  seed: 1                    # 缩放与来源区域分配的随机种子
```

时间窗超出数据覆盖范围时报错。`rate_scale` ≤ 1 时每个请求以该概率独立保留；> 1 时叠加 ⌈`rate_scale`⌉ 个稀疏化副本，
每个副本的保留概率为 `rate_scale` / ⌈`rate_scale`⌉。请求按 `sources[].share` 随机分配到来源区域；
仿真器与负载生成器使用同一种子，因此两者的请求序列与来源分配一致。
