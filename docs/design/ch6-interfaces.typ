#import "../template/cpc-doc.typ": *

= 接口规范 <ch-api>

各服务均基于 FastAPI 实现，启动后访问 `/docs` 可查看自动生成的 OpenAPI 文档并在线调试。
标注"管理"的接口在设置了环境变量 `CPC_ADMIN_TOKEN` 时需要请求头 `Authorization: Bearer <令牌>`。
所有服务都提供 `/healthz`（存活检查，进程可响应即返回 200）与 `/readyz`（就绪检查，未就绪时返回 503 并给出原因）；
`/state` 中的 `components` 或 `upstreams` 给出各依赖的健康状态（`up`、`errors_total`、`last_error`、`last_ok_age_s`）。

== 站点代理

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [POST], [`/v1/chat/completions`], [OpenAI 兼容对话接口，支持 `stream`。响应头：`x-cpc-site`、`x-cpc-node`、`x-cpc-queue-s`（站内排队时间）；非流式另有 `x-cpc-service-s`、`x-cpc-energy-est-j`（估计能耗，节点功率缺失时不给出）。排队已满返回 503（`type: overloaded`）；仿真后端的请求未给出 `max_tokens` 时返回 400。],
    [POST], [`/v1/completions`], [OpenAI 兼容补全接口，同上。],
    [GET], [`/v1/models`], [模型列表（真实后端转发给上游实例）。],
    [GET], [`/state`], [站点状态快照 `SiteSnapshot`。],
    [POST], [`/admin/capacity`（管理）], [容量指令 `{"active_nodes": 整数或 null, "cap_ratio": 0.5–1.0 或 null}`，返回执行后的快照。],
    [GET], [`/readyz`], [启动检查已通过且周期任务（状态推进、功率采样）正常运行时返回 200。],
    [GET], [`/metrics`、`/healthz`], [Prometheus 指标、存活检查。],
  ),
  caption: [站点代理接口],
)

仿真后端对请求的处理：输入 token 数取"按空白分词数"与"字符数 / 4"中的较大者，输出 token 数取 `max_tokens`
（压测时配合 vLLM 的 `ignore_eos: true` 使真实模型输出同样长度），按性能模型等待相应时间后返回假文本。

== 路由网关

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [POST], [`/v1/chat/completions`、`/v1/completions`], [路由并转发。请求头 `x-cpc-source` 指定来源区域（未携带时归属 `network.default_source`；未配置或来源未知返回 400）。透传站点的 `x-cpc-*` 响应头，另加 `x-cpc-route-reason`。路由策略拒绝时返回 503（`type: route_rejected`，含 `reason`）；可重试的失败用尽尝试次数时返回 503（`retries_exhausted`）；不重试的转发失败返回 502（`upstream_error`）；错误响应均含逐次尝试记录 `attempts`。],
    [GET], [`/v1/models`], [转发给状态可用的站点中 id 最小者；无可用站点返回 503。],
    [GET], [`/state`], [需求统计（各来源到达率 `rates`、各站点分配速率 `site_rates`）、当前策略与权重、RTT 矩阵、路由视图中各站点的负载与信号可用性、上游健康状态 `upstreams`。],
    [GET], [`/readyz`], [至少一个站点状态可用时返回 200，并列出可用站点与电网信号状态。],
    [PUT], [`/admin/policy`（管理）], [切换路由策略：`{"policy": 名称, "params": {...}}`。],
    [PUT], [`/admin/weights`（管理）], [设置路由权重：`{"weights": {来源: {站点: 非负权重}}}`；来源与站点必须是场景中已有的。],
  ),
  caption: [路由网关接口],
)

== 容量控制器

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [GET], [`/state`], [当前策略及其所需输入 `requires`、控制周期、执行器、上游健康状态、最近一次周期记录（`result` 为 `applied` 或 `skipped`，含跳过原因与排除的站点）。],
    [GET], [`/readyz`], [信号时钟同步完成后返回 200。],
    [POST], [`/admin/step`（管理）], [立即执行一次控制周期并返回决策记录。],
    [PUT], [`/admin/policy`（管理）], [切换容量策略。],
  ),
  caption: [容量控制器接口],
)

== 电网信号服务

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [GET], [`/v1/clock`], [信号时钟：起点、加速倍数、起始墙钟时间、当前信号时间。],
    [GET], [`/readyz`], [数据加载与校验在启动时完成，服务运行即返回 200。],
    [GET], [`/v1/signals[?at=ISO 时间]`], [各站点的 `GridSignal`：电价、时段、碳排放因子、功率上限、需求响应事件、PUE、碳价。],
    [GET], [`/v1/forecast?site=&hours=24&step_min=60`], [某站点未来若干小时的逐点信号（分时电价与合成碳强度可预知）。],
    [GET], [`/v1/events`], [已登记的需求响应事件。],
    [POST], [`/v1/events`（管理）], [注入事件：`{"site", "cap_kw", "start"/"end" 或 "duration_min", "kind"}`。],
    [DELETE], [`/v1/events[?site=]`（管理）], [清除全部或某站点的事件。],
  ),
  caption: [电网信号服务接口],
)

== 主要数据结构

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("SiteSnapshot 字段", "含义"),
    [`site`、`backend`], [站点 id、后端类型（emulated / openai / sim）],
    [`nodes_total`、`nodes_on`、`nodes_booting`、`nodes_draining`], [节点总数与各状态数量],
    [`slots_per_node`、`slots_on`], [每节点并发槽数、在线并发槽总数],
    [`busy`、`queue_len`、`queue_limit`], [在服务请求数、排队数、排队上限（在线槽数 × `queue_limit_per_slot`）],
    [`cap_ratio`、`cap_min_ratio`], [当前功率封顶比例、节点允许的最低封顶比例],
    [`it_power_w`、`facility_power_w`、`pue`], [IT 功率、设施功率、PUE；任一节点功率缺失时功率为 `null`],
    [`power_scope`], [`node`（加速卡 + 主机部分）或 `accelerators`（只含加速卡，`power.host: excluded`）],
    [`energy_it_j`、`energy_coverage`], [功率有效区间内的累计 IT 能耗；功率有效时长占比],
    [`est_service_s`、`arrival_rate`], [平均服务时间与到达率的滑动估计],
    [`node_idle_w`、`node_peak_w`], [单节点空载 / 满载功率（模型参数），供容量策略估算],
    [`nodes`], [各节点状态、在途数、功率与功率来源、缺失的功率组成部分 `power_missing`、最近一次操作错误 `last_error`],
    [`components`], [真实后端：主机功率方式、各传感器的健康状态与探测到的能力、各读数的功率采样完整率],
  ),
  caption: [站点快照],
)

`GridSignal` 字段：`site`、`province`、`time`、`price_yuan_per_kwh`、`period`（sharp / peak / flat / valley / deep_valley / trace）、
`carbon_kg_per_kwh`、`power_cap_kw`、`dr_event`、`pue`、`carbon_price_yuan_per_kg`，以及计算属性 `effective_price`。

== 配置文件

=== 场景文件

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("字段", "说明"),
    [`clock`], [`start`（信号时间起点，带时区）、`speedup`（加速倍数）],
    [`sources[]`], [来源区域：`id`、`name`、`lat`、`lon`、`share`（请求量占比）],
    [`sites[]`], [站点：`id`、`name`、`province`（电价与排放因子数据的键）、`lat`、`lon`、`pue`、`nodes`、`profile`、`min_active`、`grid_cap_kw`、`backend`（emulated / openai）、`upstreams[]`、`power`、`carbon_amplitude`],
    [`sites[].upstreams[]`], [真实推理实例：`id`、`url`、`gpu_indices` 或 `devices`（二者择一，必填）、`container`（可选，用于启停）、`max_inflight`、`ready_path`（默认 `/health`）],
    [`sites[].power`], [真实后端必填：`sensors`（传感器列表，同类型多个实例用 `name` 区分）、`host`（`server` / `cpu_dram` / `excluded`）],
    [`node_profiles`], [节点功率与性能参数（@tbl-params）],
    [`network`], [`base_ms`、`route_factor`、`km_per_ms`、`emulate_wan`、`overrides`、`default_source`],
    [`grid`], [`tariffs`、`carbon_factors`、`carbon_profile`（flat / synthetic）、`traces`、`dr_events[]`],
    [`workload`], [`rps`、`diurnal_amplitude`、`peak_hour`、`input_tokens`、`output_tokens`（对数正态：`mean`、`sigma`、`min`、`max`）、`trace`、`seed`],
    [`slo`], [`ttft_s`、`tpot_s`],
    [`scheduler`], [`routing`、`capacity`（`policy` 与 `params`）、`control_interval_s`、`sim_control_interval_s`、`carbon_price_yuan_per_kg`、`queue_limit_per_slot`、`retry`（`max_attempts`、`retry_on`）、`state_refresh_s`、`state_stale_s`],
  ),
  caption: [场景文件字段],
)

场景文件中的未知字段在加载时报错；相对路径相对于场景文件所在目录解析，引用的文件必须存在。

=== 地址文件

```yaml
grid: http://grid:9000
gateway: http://gateway:8080
controller: http://controller:9300
sites:
  sh: http://site-sh:9100
  gd: http://site-gd:9100
```

地址文件由 `-e` 或环境变量 `CPC_ENDPOINTS` 指定，须覆盖场景中的全部站点。本机运行时可用 `--local` 显式使用本机模式地址
（信号 9000、网关 8080、控制器 9300、站点按场景顺序从 9101 起编号），`cpc up` 未给出 `-e` 时使用同样的地址。

=== 外部信号文件（可选）

`grid.traces` 指向的 CSV 列为 `time,key,price_yuan_per_kwh,carbon_kg_per_kwh`，`key` 为站点 id 或省份键，
按整点匹配；某列留空表示不覆盖该项。可用于接入电力现货价格或逐时碳强度。
