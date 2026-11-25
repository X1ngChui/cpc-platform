#import "../../template/cpc-doc.typ": *

= 服务接口 <ch-api>

各服务均基于 FastAPI 实现，启动后访问 `/docs` 可查看自动生成的 OpenAPI 文档并在线调试。
标注"管理"的接口在设置了环境变量 `CPC_ADMIN_TOKEN` 时需要请求头 `Authorization: Bearer <令牌>`；平台内部的 HTTP 客户端不经过系统代理。
所有服务都提供 `/healthz`（存活检查，进程可响应即返回 200）与 `/readyz`（就绪检查，未就绪时返回 503 并给出原因）；
`/state` 中的 `components` 或 `upstreams` 给出各依赖的健康状态（`up`、`errors_total`、`last_error`、`last_ok_age_s`）。

== 站点代理

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [POST], [`/v1/chat/completions`], [OpenAI 兼容对话接口，支持 `stream`。请求头 `x-cpc-source`、`x-cpc-attr-<字段名>`（JSON 值）作为请求信息传给派发策略。响应头：`x-cpc-site`、`x-cpc-node`（服务器）、`x-cpc-queue-s`（站内排队时间）；非流式另有 `x-cpc-service-s`（服务时间）。队列已满返回 503（`type: overloaded`）；站点有仿真服务器而请求未给出 `max_tokens` 时返回 400。流式请求在取得
      上游的首个数据片段后才发出响应头：上游连接失败或返回错误时以相应状态（502、400 等）返回；流开始后上游中断或停滞
      超过 `scheduler.stream_idle_timeout_s` 时以错误事件结束。],
    [POST], [`/v1/completions`], [OpenAI 兼容补全接口，同上。],
    [GET], [`/v1/models`], [模型列表（有真实服务器时转发给在线的推理实例）。],
    [GET], [`/state`], [状态摘要 `SiteSnapshot`（@ch-runtime），另含传感器等依赖的健康状态 `components`。],
    [POST], [`/admin/capacity`（管理）], [数据中心指令 `{"active_nodes": 整数或 null, "cap_ratio": 比例或 null}`，返回执行后的状态摘要。],
    [GET], [`/readyz`], [启动检查已通过且周期任务（状态推进、功率采样）正常运行时返回 200。],
    [GET], [`/metrics`、`/healthz`], [Prometheus 指标、存活检查。],
  ),
  caption: [站点代理接口],
)

仿真服务器对请求的处理：输入 token 数取"按空白分词数"与"字符数 / 4"中的较大者，输出 token 数取 `max_tokens`
（负载生成器同时设置 vLLM 的 `ignore_eos: true`，使真实模型输出同样长度），按服务器模型等待相应时间后返回占位文本。

== 路由网关

#figure(
  tbl(
    columns: (4em, 13em, 1fr),
    header: ("方法", "路径", "说明"),
    [POST], [`/v1/chat/completions`、`/v1/completions`], [路由并转发。请求头 `x-cpc-source` 指定来源区域（未携带时归属 `network.default_source`；未配置或来源未知返回 400）；`x-cpc-attr-*` 作为请求属性传给路由策略并转发给站点。透传站点的 `x-cpc-*` 响应头，另加 `x-cpc-route-reason`。路由策略拒绝时返回 503（`type: route_rejected`，含 `reason`）；可重试的失败用尽尝试次数时返回 503（`retries_exhausted`）；不重试的转发失败返回 502（`upstream_error`）；错误响应均含逐次尝试记录 `attempts`。流式响应中出现错误事件、
      没有以 `data: [DONE]` 结束或停滞超过 `scheduler.stream_idle_timeout_s` 时，请求日志与指标记为 502。],
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
    [GET], [`/state`], [当前策略及其所需输入 `requires`、控制周期、上游健康状态、最近一次周期记录（`result` 为 `applied` 或 `skipped`，含跳过原因与排除的站点）。],
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
    [GET], [`/readyz`], [数据集加载与校验在启动时完成，服务运行即返回 200。],
    [GET], [`/v1/signals[?at=带时区的时间]`], [各站点的 `GridSignal`；`missing` 列出信号不可用的站点及原因，`sources` 给出各信号的数据集（`名称@版本:信号`）。],
    [GET], [`/v1/events`], [已登记的需求响应事件。],
    [POST], [`/v1/events`（管理）], [注入事件：`{"site", "cap_kw", "start"/"end" 或 "duration_min", "kind"}`。],
    [DELETE], [`/v1/events[?site=]`（管理）], [清除全部或某站点的事件。],
    [GET], [`/v1/config`（管理）], [场景文件的文本与各服务地址（`{"scenario", "endpoints"}`）。其他机器上的站点代理以
      `cpc site --control <地址>` 启动时据此取得配置，场景与地址文件只需在控制节点上维护。],
  ),
  caption: [电网信号服务接口],
)

== 主要数据结构

状态摘要 `SiteSnapshot` 的字段见 @ch-runtime；数据中心指令 `CapacityTarget` 为 `active_nodes`（目标在线服务器数）与
`cap_ratio`（功率封顶比例），任一项为 `null` 时不改变。

`GridSignal` 的字段：`site`、`time`、`price`、`price_label`、`carbon_intensity`、`currency`、`power_cap_kw`、`dr_event`、`pue`、
`carbon_price`、`extra`（扩展信号，不可用时为 `null`），以及计算属性 `effective_price`（电价 + 碳价 × 碳强度）。

== 地址文件

```yaml
grid: http://grid:9000
gateway: http://gateway:8080
controller: http://controller:9300
sites:
  sh: http://site-sh:9100
  gd: http://site-gd:9100
```

地址文件由 `-e` 或环境变量 `CPC_ENDPOINTS` 指定，须覆盖场景中的全部站点。本机运行时可用 `--local` 显式使用本机地址
（信号 9000、网关 8080、控制器 9300、站点按场景顺序从 9101 起编号），`cpc up` 未给出 `-e` 时使用同样的地址。

== 环境变量

#figure(
  tbl(
    columns: (11em, 1fr),
    header: ("变量", "含义"),
    [`CPC_DATA_DIR`], [数据集目录（场景中 `${CPC_DATA_DIR}` 的取值），缺省 `~/.cache/cpc/datasets`],
    [`CPC_ENDPOINTS`], [地址文件路径],
    [`CPC_ADMIN_TOKEN`], [管理接口令牌；设置后管理接口须携带 `Authorization: Bearer <令牌>`],
    [`CPC_LOG_LEVEL`、`CPC_LOG_FORMAT`], [日志级别；日志格式（`json` 为结构化日志）],
    [`HF_TOKEN`], [转换范例下载受控访问的原始数据（ML.ENERGY Benchmark）时使用的 Hugging Face 访问令牌],
  ),
  caption: [环境变量],
)
