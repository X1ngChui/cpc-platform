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
（`gpu_indices` 或传感器层的设备名 `devices`，二者择一）、可选的容器名（用于开关机）、并发上限 `max_inflight` 与就绪检查路径。
真实服务器的功率由站点 `measurement` 中的传感器测量（@ch-energy）；其服务器类型的参数在实验中用于全局策略的容量与功率估计，
在仿真中用于模拟该服务器。

== 服务器模型 <sec-server-model>

服务器模型给出服务器在给定状态、占用率与功率封顶比例下的功率，以及请求的服务时间。设占用率
$u = "在服务请求数" \/ "并发槽数"$，功率封顶比例为 $c$。

*功率。* 单卡功率在 $u = 0$ 时为空载功率 $P_"idle"$；$u > 0$ 时有两种给法：

- 参数式：$P(u, c) = P_a + (P_"cap" - P_a) dot u^alpha$，$P_"cap" = max(P_a, c dot P_"max")$。
  $P_a$ 为在服务时的功率下限（`gpu_active_min_w`，未给出时取 $P_"idle"$），$P_"max"$ 为满载功率，指数 $alpha$ 刻画功率随批大小的增长；
  大模型推理的解码阶段受显存带宽限制，功率随并发凹增长 @patel2024characterizing；
- 查表式：$P(u, c) = P_a + (C(u) - P_a) dot (P_"cap" - P_a) \/ (P_"max" - P_a)$，$C$ 为分段线性曲线 `gpu_power_curve`，
  $P_a$、$P_"max"$ 为曲线的最小值与最大值。曲线通常直接取自功率特性数据集的测量点，适用于参数式拟合不佳的情形（如部分混合专家模型）。

主机部分功率 $H(u) = H_"idle" + (H_"max" - H_"idle") dot u$；服务器 IT 功率 $= H(u) + n_"gpu" dot P(u, c)$。
关机服务器的功率为 `standby_w`，启动中的服务器按空载计。

*性能。* 预填充时间 $T_"pre" = L_"in" \/ (R_"pre" dot s(c))$；token 间隔 $T_"pot" = d_0 (1 + k u) \/ s(c)$（参数式）
或 $T(u) \/ s(c)$（查表式，曲线 `tpot_curve`），其中 $s(c) = c^beta$ 为功率封顶后的相对速度；
服务时间为 $T_"pre" + L_"out" dot T_"pot"$，首 token 时延为排队时间、$T_"pre"$、一个 $T_"pot"$ 与往返时延之和。
$u$ 取请求开始服务时（含该请求）的占用率。

#figure(
  tbl(
    columns: (13em, 1fr),
    header: ([参数（`node_profiles.<类型>`）], [含义]),
    [`gpus`、`gpu_model`], [每台服务器的加速卡数与型号],
    [`gpu_idle_w`], [单卡空载功率（没有在服务的请求）],
    [`gpu_active_min_w`、`gpu_max_w`、`power_exponent`], [参数式功率：在服务时的功率下限 $P_a$、满载功率 $P_"max"$、指数 $alpha$],
    [`gpu_power_curve`], [查表式功率：\[\[占用率, 单卡功率 W\], …\]，占用率在 (0, 1\] 内严格递增],
    [`host_idle_w`、`host_max_w`、`standby_w`], [主机部分空载、满载功率；关机功率],
    [`slots`], [并发槽数],
    [`prefill_tokens_per_s`], [预填充吞吐 $R_"pre"$],
    [`decode_s_per_token`、`decode_slowdown`], [参数式 token 间隔：$d_0$、$k$],
    [`tpot_curve`], [查表式 token 间隔：\[\[占用率, 秒\], …\]],
    [`cap_min_ratio`、`cap_perf_exponent`], [功率封顶比例下限；速度对封顶比例的敏感度 $beta$],
    [`boot_s`], [开机到可服务的时间],
  ),
  caption: [服务器模型参数],
)

=== 由功率特性数据标定 <sec-calibration>

`cpc data calibrate` 对功率特性数据集中同一组（设备、设备数、模型）的测量，以 $u = "concurrency" \/ "slots"$ 拟合：
单卡功率 $P(u) = P_a + (P_"max" - P_a) u^alpha$（最小二乘，$alpha$ 在 \[0.1, 2\] 上网格搜索，要求 $P_a >= 0$），
token 间隔 $T(u) = d_0 (1 + k u)$（线性最小二乘），并报告拟合残差；加 `--curves` 时另给出查表式曲线。
测量只覆盖在服务期间，空载功率、主机功率、预填充吞吐与开机时间不由标定给出，须另行实测或明确标注为示意值。

例如，默认场景的服务器类型 `h100x8` 由 ML.ENERGY Benchmark v3 中 8×H100 运行 Llama-3.1-405B-Instruct-FP8 的测量标定
（`slots` = 128）：$P_a$ = 306.4 W，$P_"max"$ = 581.6 W，$alpha$ = 0.38，$d_0$ = 31.63 ms，$k$ = 1.039；
`b200x4` 由 4×B200 运行同一模型的测量标定。

== 网络时延

来源区域到站点的往返时延 $"RTT" = "base_ms" + 2 dot d dot "route_factor" \/ "km_per_ms"$，$d$ 为两地大圆距离，
`route_factor` 为路由绕行系数，`km_per_ms` 为光纤中的传播速度（约 200 km/ms）；`network.overrides` 可按来源与站点给出实测值。
实验中，`emulate_wan` 为真时网关在转发前与返回后各注入 RTT 的一半；各服务实际分布在不同地域时应设为假。
`cpc rtt` 打印 RTT 矩阵。

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
