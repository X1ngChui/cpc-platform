#import "../../template/cpc-doc.typ": *

= 编写场景 <g-scenario>

场景文件（YAML 格式）描述一次运行的全部对象：用户从哪里来、有哪些数据中心与服务器、电网信号与负载取自哪些数据集、
服务目标是什么、采用哪些调度策略。同一个场景文件既用于仿真，也用于实验。编写场景时，通常从 `examples/quickstart/scenario.yaml`
或 `configs/scenario.yaml` 复制一份再修改，每次修改后用 `cpc check -s <场景文件>` 检查。

场景文件中的相对路径相对于场景文件所在目录；路径中可以使用环境变量，如 `${CPC_DATA_DIR}`。拼写错误的字段名会使加载失败
并指出该字段，而不会被忽略。

== 整体结构

```yaml
name: my-scenario          # 场景名称
clock: {...}               # 信号时钟：从哪个时刻开始
sources: [...]             # 用户来源区域
node_profiles: {...}       # 服务器类型
sites: [...]               # 数据中心站点
network: {...}             # 网络时延（可省略）
grid: {...}                # 电网信号与需求响应
workload: {...}            # 负载
slo: {...}                 # 服务目标（可省略）
scheduler: {...}           # 全局策略与运行参数（可省略）
```

== clock：信号时钟

```yaml
clock:
  start: "2026-07-15T11:00:00+08:00"   # 起点，须带时区
  speedup: 1                           # 实验中的加速倍数（仿真中不起作用）
```

`start` 决定电网信号从哪个日期与时刻开始取值（例如是否处于夏季、是否为尖峰时段），须在电网信号数据的覆盖范围内。
仿真中，第 $t$ 秒对应信号时间 `start` + $t$。实验中，信号时间 = `start` + 真实经过的时间 × `speedup`；
`speedup: 60` 表示真实 1 分钟对应信号时间 1 小时，便于在短时间内观察电价变化。

== sources：来源区域

```yaml
sources:
  - {id: east,  name: Shanghai,  lat: 31.23, lon: 121.47, share: 0.40}
  - {id: south, name: Guangzhou, lat: 23.13, lon: 113.26, share: 0.35}
```

`lat`、`lon` 为纬度与经度，用于计算到各站点的距离与往返时延；`share` 为请求量的占比，平台按比例把负载中的每个请求
随机分配到一个来源区域（不要求和为 1，按比例归一化）。

== node_profiles：服务器类型

```yaml
node_profiles:
  h100x8:
    description: 8xH100, Llama-3.1-405B-Instruct-FP8
    slots: 128
    power_w: [[0, 1360], [0.0624, 4060.4], [0.2493, 4544.3],
              [0.4989, 4962.1], [0.9973, 5406.1]]
    tpot_s: [[0.0624, 0.03406], [0.2493, 0.04039], [0.4989, 0.04689], [0.9973, 0.06527]]
    prefill_tokens_per_s: 5000
    boot_s: 180
```

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("字段", "含义"),
    [`slots`], [并发槽数：一台服务器同时处理的请求数上限（对应推理引擎的最大批大小，如 vLLM 的 `max_num_seqs`）],
    [`power_w`], [功率表：\[占用率, 服务器 IT 功率（W）\] 的列表。第一个点的占用率须为 0，表示开机但没有请求时的功率；占用率从小到大排列],
    [`tpot_s`], [token 间隔表：\[占用率, 秒\]。批中的请求越多，每个请求生成一个 token 所需的时间越长],
    [`prefill_tokens_per_s`], [预填充吞吐：处理输入 token 的速度，决定首 token 前的计算时间],
    [`boot_s`], [从开机到可以服务的时间（含加载模型）],
    [`off_w`], [关机时的功率，缺省 0。真实服务器的"关机"通常只停止推理实例而机器仍在运行，此时宜填写实测值
      （`cpc measure --boot` 测量）],
    [`cap_min_ratio`、`cap_perf_exponent`], [功率封顶：封顶比例的下限（缺省 0.5）；封顶比例为 c 时速度变为原来的 c 的该指数次方（缺省 0.4）],
    [`description`], [说明文字（可选）],
  ),
  caption: [服务器类型的字段],
)

表的生成方法见 @g-profile。功率封顶比例为 c 时，服务器功率不超过"空载 + c ×（满载 − 空载）"。

== sites：数据中心站点

```yaml
sites:
  - id: nm                         # 站点标识（在命令行、指标与结果中使用）
    name: Inner Mongolia - Horinger
    region: inner_mongolia         # 电网区域：电网信号数据集中的区域名
    lat: 40.38
    lon: 111.82
    pue: 1.20                      # 设施功率 = IT 功率 × PUE
    pools:                         # 服务器池
      - {id: h100, profile: h100x8, count: 2}
      - {id: b200, profile: b200x4, count: 2}
    min_active: 1                  # 容量策略至少保留的在线服务器数（缺省 1）
    grid_cap_kw: 30                # 并网容量上限（kW，可选）
    scheduler:                     # 站内调度（可选）
      dispatch: {policy: efficient_first}
      power: {policy: efficient}
```

*服务器池。* 每个池引用一个服务器类型：仿真服务器池给出数量 `count`；真实服务器池（`backend: openai`）列出推理实例
（@g-real）。一个站点可以有多个池。

*站内调度。* `dispatch` 为到达站点的请求选择服务器，缺省 `least_loaded`（占用率最低者）；`power` 决定开关哪几台服务器，
缺省 `ordered`（按配置顺序）。站点有多种服务器时，可用 `efficient_first` 与 `efficient` 优先使用单位并发槽功率低的服务器。
全部可选的策略见 @g-policy。

== network：网络时延

```yaml
network:
  base_ms: 4                # 固定时延
  route_factor: 1.5         # 路由绕行系数
  km_per_ms: 200            # 光纤中的传播速度（km/ms）
  emulate_wan: true         # 实验中由网关注入时延
  default_source: east      # 未标明来源区域的请求归属的区域（实验中使用）
```

往返时延 = `base_ms` + 2 × 距离 × `route_factor` / `km_per_ms`。整个 `network` 部分可以省略，此时使用上述缺省值
（`default_source` 为空）。`cpc check` 打印各来源区域到各站点的往返时延。

== grid：电网信号与需求响应

```yaml
grid:
  price:  {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: price}
  carbon: {dataset: "${CPC_DATA_DIR}/cn-grid-2026", signal: carbon_intensity}
  extras:                           # 扩展信号（可选），策略从 signal.extra 中读取
    - {name: renewable, dataset: "${CPC_DATA_DIR}/my-grid", signal: renewable_share}
  dr_events:                        # 需求响应事件（可选）
    - {site: gd, cap_kw: 5, start: "2026-07-15T11:20:00+08:00",
       end: "2026-07-15T11:50:00+08:00"}
```

`price` 与 `carbon` 各指定一个数据集与其中的信号名，站点的 `region` 须在数据集中收录。站点所用的区域名与数据集中的不一致时，
用 `regions` 对应：`price: {dataset: ..., signal: price, regions: {sh: east}}`。需求响应事件要求站点在时段内的设施功率
不超过 `cap_kw`；实验运行时也可以用 `cpc event` 注入。碳价（`scheduler.carbon_price_per_kg`）与电价使用同一货币。

== workload：负载

```yaml
workload:
  dataset: "${CPC_DATA_DIR}/azure-llm-2024-conv"
  start_s: 230400        # 从原始 trace 的第 230400 秒开始取（缺省 0）
  duration_s: 86400      # 取 86400 秒（缺省取到结束）
  time_scale: 1          # 时间压缩：2 表示把 trace 的时间压缩为一半
  rate_scale: 1          # 到达率缩放：0.5 随机保留一半请求，2 复制为两倍
  attributes: []         # 传给策略的扩展字段
  seed: 1                # 随机种子（到达率缩放与来源区域分配）
```

负载的第 0 秒对应 `clock.start`。时间窗超出数据范围时，加载失败并给出数据的覆盖范围。`cpc sim`、`cpc live` 等命令的
`--start-s`、`--duration-s`、`--time-scale`、`--rate-scale` 参数可以临时覆盖这些值。

== slo 与 scheduler

```yaml
slo:
  ttft_s: 2.0                     # 首 token 时延上限（秒）
  tpot_s: 0.15                    # token 间隔上限（秒）

scheduler:
  routing:  {policy: nearest, params: {max_wait_s: 0.5}}     # 全局路由策略
  capacity: {policy: reactive, params: {target_util: 0.6}}   # 全局容量策略
  carbon_price_per_kg: 0.1        # 碳价（与电价同一货币 / kgCO2）
  sim_control_interval_s: 300     # 仿真中容量策略的决策周期（秒）
  control_interval_s: 15          # 实验中容量策略的决策周期（秒）
  # 站点队列上限 = max(在线并发槽数, 单台服务器的并发槽数) × 该系数
  queue_limit_per_slot: 4
```

`scheduler` 中其余运行参数（重试、状态刷新周期、流式响应的空闲超时 `stream_idle_timeout_s` 等）一般不需要修改，
含义见《平台设计说明书》"场景与模型"一章与"接口"一章。

== 常见错误

#figure(
  tbl(
    columns: (14em, 1fr),
    header: ("错误信息（摘要）", "原因与处理"),
    [`unknown field`], [字段名拼写错误或放错了层级；报错给出字段路径与相近的正确字段名（`did you mean ...`）],
    [`required field is missing`], [该字段没有缺省值，须填写；字段名拼错时与上一条同时出现],
    [`unknown routing policy`（或 capacity、dispatch、power policy）], [策略名拼写错误或策略包未安装；报错列出全部可选的策略名],
    [`does not accept`], [策略参数名拼写错误；报错列出该策略的全部参数],
    [`dataset ... does not exist`、`is missing`], [数据集尚未准备，或路径写错；`CPC_DATA_DIR` 是否与准备数据时一致],
    [`does not cover the site regions`], [站点的 `region` 在电网信号数据集中不存在；改正 `region` 或用 `regions` 对应],
    [`does not include the clock start`], [`clock.start` 超出电网信号数据的覆盖范围；转换数据时扩大时间范围或调整起点],
    [`window ... is outside`], [`workload.start_s` 与 `duration_s` 超出负载数据的长度],
    [`less than the simulated`], [`cpc sim --hours` 大于负载变换后的长度；减小时长或扩大时间窗],
    [`the first point must be at occupancy 0`], [功率表须从空载开始],
  ),
  caption: [场景的常见错误],
)
