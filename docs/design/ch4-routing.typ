#import "../template/cpc-doc.typ": *

= 请求路由子系统 <ch-routing>

== 网关的职责

路由网关是平台对外的唯一推理入口，职责包括：

+ 提供 OpenAI 兼容接口（`/v1/chat/completions`、`/v1/completions`，支持流式输出），因此任何 OpenAI 客户端、
  vLLM 自带的压测工具都可以直接对网关发请求；
+ 根据请求头 `x-cpc-source` 确定来源区域（未携带时归属场景的 `network.default_source`；未配置该项或来源未知时返回 400），
  构造集群视图，调用路由策略得到路由决策；
+ 模拟跨数据中心的广域网时延；
+ 按 `scheduler.retry` 处理转发失败：可重试的失败类型（`overloaded`：站点排队已满返回 503；`connect_error`：连接建立失败，
  请求未送达）排除该站点后重新决策，每个请求至多尝试 `max_attempts` 个站点（默认 3）；请求送达后的读取失败或超时不重试，返回 502；
+ 维护各站点状态与电网信号的健康状态：拉取失败或超过 `scheduler.state_stale_s` 未成功刷新的站点移出路由视图（@sec-failure）；
+ 统计各来源区域到达率（指数滑动平均），供容量控制器使用；
+ 记录每个请求的日志（来源、站点、节点、时延、token 数、电价、碳强度、SLO 是否达成）与 Prometheus 指标。

== 广域网时延

来源区域到站点的往返时延默认由大圆距离估计：

$ "RTT" = t_0 + 2 dot d dot r \/ v $

其中 $d$ 为大圆距离，$r$ 为光纤路由相对大圆距离的绕行系数（默认 1.5），$v$ 为光在光纤中的传播速度（约 200 km/ms），
$t_0$ 为固定开销（默认 4 ms）。默认场景的 RTT 矩阵如下（`cpc rtt` 打印）：

#figure(
  tbl(
    columns: (8em, 1fr, 1fr, 1fr, 1fr),
    align: (left, right, right, right, right),
    header: ("来源 \\ 站点", "sh 上海", "gd 韶关", "gz 贵安", "nm 和林格尔"),
    [east 上海], [4.5], [19.8], [27.4], [24.1],
    [south 广州], [21.7], [6.8], [15.7], [32.8],
    [north 北京], [19.9], [29.5], [30.4], [9.9],
  ),
  caption: [默认场景的 RTT 估计（ms）],
)

实测数据可写入场景的 `network.overrides` 覆盖估计值。时延注入的方式：网关在转发前等待 RTT/2，在返回响应（或流式输出的首个片段）
前再等待 RTT/2。当服务器真实分布在不同地域时，应把 `network.emulate_wan` 设为 `false`，由真实网络产生时延；
若希望时延出现在网络层（例如测试长连接、流式输出），可用 `deploy/cloud/netem.sh` 通过 Linux `tc netem` 对指定目的地址加时延。

可以看到，跨数据中心增加的时延为几十毫秒，相对于大模型推理秒级的首 token 时延要求较小，这正是推理请求可以在地域间灵活调度的前提；
真正限制跨域调度的往往是目标站点的排队与容量，而不是网络时延。

== 路由策略接口

路由策略是一个带 `choose()` 方法的类，返回结构化的路由决策 `RouteDecision(site, reason, detail)`：`site` 为 `None` 表示拒绝；
`reason` 为取值有限的短标识，写入请求日志、响应头 `x-cpc-route-reason` 与指标 `cpc_gateway_route_decisions_total{policy,reason}`。

```python
from cpc.sched import RouteDecision, RoutingPolicy, register_routing

@register_routing("my_policy")            # 注册名，在场景文件或命令行中引用
class MyPolicy(RoutingPolicy):
    def __init__(self, alpha: float = 1.0, **params):   # 参数来自场景文件的 params；未知参数在构造时报错
        super().__init__(**params)
        self.alpha = alpha

    def choose(self, req, view, exclude=()):
        # req:  RequestInfo(source, input_tokens, output_tokens, id)；output_tokens 在请求未给出 max_tokens 时为 None
        # view: ClusterView(now, sites, rtt_ms, slo, weights)；sites 只含状态可用的站点
        cands = self.candidates(view, exclude)      # 有在线容量、排队未满、未被排除的站点
        if not cands:
            return RouteDecision.reject("no_available_site")
        best = min(cands, key=lambda sv: sv.load + self.alpha * view.rtt(req.source, sv.id) / 100)
        return RouteDecision.to(best.id, "min_score")
```

电网信号缺失时 `sv.signal` 为 `None`。依赖信号的策略应把这类站点排除在评分之外，并通过原因说明，而不是按缺省价格计算。

集群视图中每个站点为一个 `SiteView`，提供以下便捷属性（均基于站点快照与网关本地的 pending 计数）：

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("属性", "含义"),
    [`sv.snap`], [站点快照 `SiteSnapshot`（字段见 @ch-api）],
    [`sv.signal`], [站点当前电网信号：电价、时段、碳排放因子、功率上限、PUE、等效电价 `effective_price`；信号缺失时为 `None`],
    [`sv.load`], [负载率 =（在服务 + 排队 + 在途）/ 在线并发槽数],
    [`sv.est_wait_s`], [估计排队等待时间],
    [`sv.available`], [是否可接收请求（有在线槽、排队未满）],
    [`view.rtt(src, site)`], [来源到站点的 RTT（ms）],
    [`view.est_ttft_s(req, sv)`], [估计 TTFT（不含预填充）= RTT + 排队等待],
    [`view.weights`], [控制器下发的路由权重 `{来源: {站点: 权重}}`],
  ),
  caption: [路由策略可用的视图信息],
)

同一策略对象既被在线网关调用，也被离散事件仿真器调用；策略内部不应依赖墙钟时间，时间请使用 `view.now`（信号时间）。

== 参考策略

#figure(
  tbl(
    columns: (7em, 1fr),
    header: ("策略", "行为"),
    [`round_robin`], [在可用站点间轮询（基线）。],
    [`random`], [均匀随机（基线）。],
    [`least_loaded`], [选择负载率最低的站点（基线）。],
    [`nearest`], [按 RTT 从近到远，选第一个估计排队时间不超过 `max_wait_s` 的站点（原因 `nearest`）；都超过时按溢出规则处理（`wait_exceeded`）。模拟"就近接入"。],
    [`cost_aware`], [在有电网信号、满足 TTFT 预算（`ttft_budget` × SLO）且负载率低于 `max_load` 的站点中，选单位请求边际成本最低者（`min_cost`）。边际成本 = 等效电价（电价 + 碳价 × 碳强度）× PUE × 单请求边际能耗。无站点有信号（`no_signal`）或无站点满足条件（`over_budget`）时按溢出规则处理。演示"算随电走"。],
    [`weighted`], [按控制器下发的权重随机分流（`weighted`）；该来源没有可用站点的正权重（`no_weights`）或抽中的站点负载率不低于 `max_load`（`target_full`）时按溢出规则处理。用于实现"控制器算计划、网关按计划分流"的两级调度。],
  ),
  caption: [参考路由策略],
)

`nearest`、`cost_aware`、`weighted` 的参数 `overflow` 给出首选条件不满足时的溢出规则：`least_loaded`（默认）选负载率最低的可用站点，
原因记为 `overflow_<条件>`（如 `overflow_no_signal`）；`reject` 拒绝请求，原因记为触发条件本身。
溢出是策略定义的一部分，以原因显式记录，便于在实验结果中统计各条件出现的比例（仿真结果的 `route_reasons`）。

#note[这些策略只用于演示接口与数据通路，没有经过优化，也不代表推荐的研究方法。例如 `cost_aware` 对每个请求独立贪心，
在多个网关实例或高并发下容易造成"羊群效应"；`weighted` 的效果完全取决于控制器给出的权重。]

== 运行时切换

路由策略可在运行时切换，不需要重启网关：

```bash
curl -X PUT http://localhost:8080/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "cost_aware", "params": {"max_load": 0.8}}'
```

设置了 `CPC_ADMIN_TOKEN` 时需附带 `-H "Authorization: Bearer <令牌>"`。
