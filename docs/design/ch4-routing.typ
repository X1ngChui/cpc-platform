#import "../template/cpc-doc.typ": *

= 请求路由子系统 <ch-routing>

== 网关的职责

路由网关是平台对外的唯一推理入口，职责包括：

+ 提供 OpenAI 兼容接口（`/v1/chat/completions`、`/v1/completions`，支持流式输出），因此任何 OpenAI 客户端、
  vLLM 自带的压测工具都可以直接对网关发请求；
+ 根据请求头 `x-cpc-source` 确定来源区域，构造集群视图，调用路由策略选择站点；
+ 模拟跨数据中心的广域网时延；
+ 站点返回 503（排队已满）或连接失败时，排除该站点后重试，最多 3 次；
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

路由策略是一个带 `choose()` 方法的类：

```python
from cpc.sched import RoutingPolicy, register_routing

@register_routing("my_policy")            # 注册名，在场景文件或命令行中引用
class MyPolicy(RoutingPolicy):
    def __init__(self, alpha: float = 1.0, **params):   # 参数来自场景文件的 params
        super().__init__(**params)
        self.alpha = alpha

    def choose(self, req, view, exclude=()):
        # req:  RequestInfo(source, input_tokens, output_tokens, id)
        # view: ClusterView(now, sites, rtt_ms, slo, weights)
        cands = self.candidates(view, exclude)      # 健康、有在线容量、排队未满的站点
        if not cands:
            return None                              # None 表示拒绝
        return min(cands, key=lambda sv: sv.load + self.alpha * view.rtt(req.source, sv.id) / 100).id
```

集群视图中每个站点为一个 `SiteView`，提供以下便捷属性（均基于站点快照与网关本地的 pending 计数）：

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("属性", "含义"),
    [`sv.snap`], [站点快照 `SiteSnapshot`（字段见 @ch-api）],
    [`sv.signal`], [站点当前电网信号：电价、时段、碳排放因子、功率上限、PUE、等效电价 `effective_price`],
    [`sv.load`], [负载率 =（在服务 + 排队 + 在途）/ 在线并发槽数],
    [`sv.est_wait_s`], [估计排队等待时间],
    [`sv.available`], [是否可接收请求（健康、有在线槽、排队未满）],
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
    [`nearest`], [按 RTT 从近到远，选第一个估计排队时间低于 `max_wait_s` 的站点；都超过时选负载最低者。模拟"就近接入"。],
    [`cost_aware`], [在满足 TTFT 预算（`ttft_budget` × SLO）且负载率低于 `max_load` 的站点中，选单位请求边际成本最低者。边际成本 = 等效电价（电价 + 碳价 × 碳强度）× PUE × 单请求边际能耗。演示"算随电走"。],
    [`weighted`], [按控制器下发的权重随机分流；目标满载时改投负载最低的站点。用于实现"控制器算计划、网关按计划分流"的两级调度。],
  ),
  caption: [参考路由策略],
)

#note[这些策略只用于演示接口与数据通路，没有经过优化，也不代表推荐的研究方法。例如 `cost_aware` 对每个请求独立贪心，
在多个网关实例或高并发下容易造成"羊群效应"；`weighted` 的效果完全取决于控制器给出的权重。]

== 运行时切换

路由策略可在运行时切换，不需要重启网关：

```bash
curl -X PUT http://localhost:8080/admin/policy -H "Content-Type: application/json" \
     -d '{"policy": "cost_aware", "params": {"max_load": 0.8}}'
```

设置了 `CPC_ADMIN_TOKEN` 时需附带 `-H "Authorization: Bearer <令牌>"`。
