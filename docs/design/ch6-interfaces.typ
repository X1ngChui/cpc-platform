#import "../template/cpc-doc.typ": *

= 接口规范 <ch-api>

各服务均基于 FastAPI 实现，启动后访问 `/docs` 可查看自动生成的 OpenAPI 文档并在线调试。
标注"管理"的接口在设置了环境变量 `CPC_ADMIN_TOKEN` 时需要请求头 `Authorization: Bearer <令牌>`。

== 站点代理

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [POST], [`/v1/chat/completions`], [OpenAI 兼容对话接口，支持 `stream`。响应头：`x-cpc-site`、`x-cpc-node`、`x-cpc-queue-s`（站内排队时间）；非流式另有 `x-cpc-service-s`、`x-cpc-energy-j`（估计能耗）。排队已满返回 503。],
    [POST], [`/v1/completions`], [OpenAI 兼容补全接口，同上。],
    [GET], [`/v1/models`], [模型列表（真实后端转发给上游实例）。],
    [GET], [`/state`], [站点状态快照 `SiteSnapshot`。],
    [POST], [`/admin/capacity`（管理）], [容量指令 `{"active_nodes": 整数或 null, "cap_ratio": 0.5–1.0 或 null}`，返回执行后的快照。],
    [GET], [`/metrics`、`/healthz`], [Prometheus 指标、健康检查。],
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
    [POST], [`/v1/chat/completions`、`/v1/completions`], [路由并转发。请求头 `x-cpc-source` 指定来源区域。透传站点的 `x-cpc-*` 响应头。无可用站点时返回 503。],
    [GET], [`/v1/models`], [转发给任一可达站点。],
    [GET], [`/state`], [需求统计（各来源到达率 `rates`、各站点分配速率 `site_rates`）、当前策略与权重、RTT 矩阵、各站点负载视图。],
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
    [GET], [`/state`], [当前策略、控制周期、执行器、最近一次决策记录。],
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
    [`site`、`backend`、`healthy`], [站点 id、后端类型（emulated / openai / sim）、是否健康],
    [`nodes_total`、`nodes_on`、`nodes_booting`、`nodes_draining`], [节点总数与各状态数量],
    [`slots_per_node`、`slots_on`], [每节点并发槽数、在线并发槽总数],
    [`busy`、`queue_len`、`queue_limit`], [在服务请求数、排队数、排队上限（在线槽数 × `queue_limit_per_slot`）],
    [`cap_ratio`], [当前功率封顶比例],
    [`it_power_w`、`facility_power_w`、`pue`], [IT 功率、设施功率、PUE],
    [`energy_it_j`], [累计 IT 能耗],
    [`est_service_s`、`arrival_rate`], [平均服务时间与到达率的滑动估计],
    [`node_idle_w`、`node_peak_w`], [单节点空载 / 满载功率（模型参数），供容量策略估算],
    [`nodes`], [各节点状态、在途数、功率与功率来源],
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
    [`sites[]`], [站点：`id`、`name`、`province`（电价与排放因子数据的键）、`lat`、`lon`、`pue`、`nodes`、`profile`、`min_active`、`grid_cap_kw`、`backend`（emulated / openai）、`upstreams[]`、`sensors[]`、`carbon_amplitude`],
    [`sites[].upstreams[]`], [真实推理实例：`id`、`url`、`gpu_indices` 或 `devices`、`container`（可选，用于启停）、`max_inflight`],
    [`node_profiles`], [节点功率与性能参数（@tbl-params）],
    [`network`], [`base_ms`、`route_factor`、`km_per_ms`、`emulate_wan`、`overrides`],
    [`grid`], [`tariffs`、`carbon_factors`、`carbon_profile`（flat / synthetic）、`traces`、`dr_events[]`],
    [`workload`], [`rps`、`diurnal_amplitude`、`peak_hour`、`input_tokens`、`output_tokens`（对数正态：`mean`、`sigma`、`min`、`max`）、`trace`、`seed`],
    [`slo`], [`ttft_s`、`tpot_s`],
    [`scheduler`], [`routing`、`capacity`（`policy` 与 `params`）、`control_interval_s`、`sim_control_interval_s`、`carbon_price_yuan_per_kg`、`queue_limit_per_slot`],
  ),
  caption: [场景文件字段],
)

=== 地址文件

```yaml
grid: http://grid:9000
gateway: http://gateway:8080
controller: http://controller:9300
sites:
  sh: http://site-sh:9100
  gd: http://site-gd:9100
```

未指定地址文件时使用本机默认端口（信号 9000、网关 8080、控制器 9300、站点从 9101 起依次编号）。
也可用环境变量 `CPC_ENDPOINTS` 指定地址文件。

=== 外部信号文件（可选）

`grid.traces` 指向的 CSV 列为 `time,key,price_yuan_per_kwh,carbon_kg_per_kwh`，`key` 为站点 id 或省份键，
按整点匹配；某列留空表示不覆盖该项。可用于接入电力现货价格或逐时碳强度。
