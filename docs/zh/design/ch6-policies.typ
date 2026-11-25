#import "../../template/cpc-doc.typ": *

= 调度策略 <ch-policy>

平台定义四类策略接口：全局的路由策略与容量策略，本地的派发策略与电源管理策略。每类接口在仿真与实验中以同一方式调用，
策略只读取传入的视图并返回决策，不修改平台状态。内置策略是用于演示接口与数据通路的参考实现，不代表推荐算法。
策略的参数在场景文件中给出（`{policy: 名称, params: {...}}`），未知参数在构造时报错。

== 路由策略（全局）

网关对每个请求调用 `choose(req, view, exclude)`，得到路由决策 `RouteDecision`：目标站点（`None` 表示拒绝）、
原因（有限取值的短标识，作为指标标签）与补充信息。`exclude` 为本请求已尝试失败的站点。仿真器调用同一接口。

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ("输入", "内容"),
    [`RequestInfo`], [来源区域 `source`；输入 token 数 `input_tokens`（实验中为近似值）；输出 token 上限 `output_tokens`；请求标识 `id`；扩展属性 `attrs`],
    [`ClusterView`], [信号时间 `now`；各站点视图 `sites`；RTT 矩阵；SLO；容量策略下发的路由权重 `weights`；方法 `rtt(source, site)`、`est_ttft_s(req, site_view)`],
    [`SiteView`], [状态摘要 `snap`；电网信号 `signal`（缺失时为 `None`）；已派发而尚未体现在摘要中的请求数 `pending`；派生量 `load`（（在服务 + 排队 + 在途）/ 在线并发槽数）、`est_wait_s`（估计排队时间）、`available`（有在线并发槽且队列未满）],
  ),
  caption: [路由策略的输入],
)

视图中只包含状态可用的站点。策略的首选条件不满足时，按显式的溢出规则处理（`overflow` 参数）：
`least_loaded` 选择负载最低的可用站点，原因记为 `overflow_<触发条件>`；`reject` 拒绝请求，原因记为触发条件。

#figure(
  tbl(
    columns: (7em, 13em, 1fr),
    header: ("名称", "参数（缺省值）", "规则"),
    [`round_robin`], [—], [在可用站点间轮询（基线）。],
    [`least_loaded`], [—], [选负载最低的可用站点（基线）。],
    [`nearest`], [`max_wait_s`（0.5）、`overflow`], [按 RTT 从近到远，选第一个估计排队时间不超过 `max_wait_s` 的站点；都超过时溢出（`wait_exceeded`）。],
    [`cost_aware`], [`max_load`（0.9）、`ttft_budget`（0.5）、`latency_weight`（0）、`overflow`], [在有电网信号、负载低于 `max_load`、估计 TTFT 不超过 `ttft_budget` × SLO 的站点中，选"等效电价 × PUE × 单请求边际能耗 + `latency_weight` × 估计 TTFT"最小者。单请求边际能耗 =（满载 − 空载 IT 功率）/ 并发槽数 × 平均服务时间。没有信号时溢出（`no_signal`），预算或负载不满足时溢出（`over_budget`）。],
    [`weighted`], [`seed`（0）、`max_load`（1.0）、`overflow`], [按容量策略下发的权重（来源 → 站点的流量比例）随机分流；该来源没有可用站点的正权重时溢出（`no_weights`），抽中站点负载不低于 `max_load` 时溢出（`target_full`）。],
  ),
  caption: [参考路由策略],
)

== 容量策略（全局）

控制器每个控制周期调用 `decide(view)`，得到 `CapacityPlan`：各站点的指令（目标在线服务器数、功率封顶比例）、
可选的路由权重（下发给网关，配合 `weighted` 路由）与说明信息 `notes`（写入决策日志）。

*输入完整性。* 策略在类属性 `requires` 中声明所需输入：`signals`（各站点的电网信号）、`demand`（网关的需求统计）。
控制器与仿真器只在所需输入齐全时调用 `decide()`，否则跳过本周期并记录原因；状态不可达的站点不出现在 `view.sites` 中，
其原因见 `view.excluded`。因此策略可以直接按站点索引信号，无需处理缺失。

#figure(
  tbl(
    columns: (10em, 1fr),
    header: ([`ControlView` 字段], [内容]),
    [`now`、`interval_s`], [信号时间与控制周期],
    [`sites`], [本周期状态可用的站点的状态摘要],
    [`signals`], [各站点的电网信号（声明需要时覆盖 `sites` 中的全部站点）],
    [`demand`], [各来源区域的到达率（声明需要时不为 `None`）],
    [`rtt_ms`、`slo`、`min_active`、`excluded`], [RTT 矩阵、SLO、各站点最少在线服务器数、被排除的站点及原因],
  ),
  caption: [容量策略的输入],
)

#figure(
  tbl(
    columns: (7em, 13em, 1fr),
    header: ("名称", "参数（缺省值）", "规则"),
    [`static`], [—], [全部服务器在线、不封顶（基线）。不需要信号与需求统计。],
    [`reactive`], [`target_util`（0.6）、`scale_down_step`（1）], [按 Little 定律估计所需并发：ceil(max(到达率 × 平均服务时间, 在服务 + 排队) / (每台服务器并发槽数 × 目标利用率))；扩容立即执行，缩容每周期最多减少 `scale_down_step` 台；遵守设施功率上限（需要信号），上限低于 `min_active` 时以上限为准并记录。],
    [`grid_aware`], [`target_util`（0.6）、`rtt_budget_ms`（60）、`headroom`（1.1）、`scale_down_step`（1）、`demand_source`（`gateway`）], [由全局到达率估计所需并发槽数，按"等效电价 × PUE"从低到高把服务器分配给各站点（受服务器总数与功率上限约束，各站点至少保留 `min_active` 台）；各来源区域的路由权重正比于其 RTT 预算内各站点分到的容量；功率上限不足以支撑 `min_active` 台满载服务器时，取使其满足上限的最大封顶比例，仍不满足时按上限可容纳的服务器数开启并记录。],
  ),
  caption: [参考容量策略],
)

`cpc.sched.capacity` 提供两个辅助函数：`max_nodes_under_cap(snap, signal, cap_ratio)` 给出设施功率上限下最多可开启的服务器数
（按服务器满载功率保守估计），`cap_ratio_for(snap, signal, nodes, min_ratio)` 给出使给定数量的服务器满载时满足上限的最大封顶比例。

== 本地策略 <sec-local-policy>

=== 派发策略

派发策略对到达站点的请求选择服务器，结构参照 Kubernetes 调度框架的过滤与打分扩展点 @k8s-scheduling-framework：

- `filter(req, server)`：服务器是否可接收该请求（缺省为在线且有空闲并发槽）；
- `score(req, server)`：分数，取最小者；
- `select(req, servers)`：缺省实现为过滤后取分数最小者，可整体重写；
- `priority(req)`：排队优先级，数值大者先出队（缺省为 0，即先到先出）。

服务器视图 `ServerView` 提供标识、所属池、状态、在服务请求数、并发槽数、封顶比例、是否可接收，以及服务器模型给出的
满载与空载 IT 功率（`peak_w`、`idle_w`）。

#figure(
  tbl(
    columns: (8em, 9em, 1fr),
    header: ("名称", "参数（缺省值）", "规则"),
    [`least_loaded`], [—], [占用率最低的服务器（站内负载均衡，缺省）。],
    [`packing`], [—], [占用率最高的可接收服务器（集中负载，便于空闲服务器关机）。],
    [`efficient_first`], [—], [单位并发槽满载功率最低的服务器优先，同类服务器间按占用率均衡（异构服务器池）。],
    [`affinity`], [`key`（`session_id`）], [按请求属性 `key` 的哈希选择首选服务器，使同一会话的请求落在同一服务器上，便于复用 KV 缓存；属性缺失时按占用率选择。],
    [`priority`], [`key`（`priority`）], [服务器选择同 `least_loaded`；排队时按请求属性 `key` 的数值出队。],
  ),
  caption: [参考派发策略],
)

=== 电源管理策略

电源管理策略 `plan(want, servers)` 把目标在线服务器数 `want` 落实到具体服务器，返回需要改变状态的服务器及其目标状态
（`on` 或 `off`）；运行时按状态机执行（@ch-runtime）。

#figure(
  tbl(
    columns: (8em, 1fr),
    header: ("名称", "规则"),
    [`ordered`], [扩容时先恢复排空中的服务器，再按配置顺序开启关机的服务器；缩容时先取消启动中的服务器，再停止在服务请求最少的服务器（缺省）。],
    [`efficient`], [优先保留单位并发槽满载功率低的服务器：扩容时先开启高效服务器，缩容时先停止低效服务器（异构服务器池）。],
  ),
  caption: [参考电源管理策略],
)

== 编写策略

策略是 Python 类：继承相应基类，实现决策方法，构造参数即场景中的 `params`。示例：一个在可用站点中选择碳强度最低者的路由策略。

```python
from cpc.sched import RouteDecision, RoutingPolicy


class Greenest(RoutingPolicy):
    """在有电网信号、负载低于 max_load 的可用站点中选碳强度最低者；
    没有时按负载最低者溢出。"""

    def __init__(self, max_load: float = 0.9, **params):
        super().__init__(**params)       # 未知参数在此报错
        self.max_load = max_load

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)
        if not cands:
            return RouteDecision.reject("no_available_site")
        ok = [sv for sv in cands if sv.signal is not None and sv.load < self.max_load]
        if not ok:
            return self.overflow("least_loaded", cands, "no_candidate")
        best = min(ok, key=lambda sv: (sv.signal.carbon_intensity, sv.id))
        return RouteDecision.to(best.id, "min_carbon")
```

本地策略的写法相同，例如按请求属性 `tenant` 把请求固定到某个服务器池的派发策略只需重写 `filter`。

*注册。* 内置策略以装饰器注册（`ROUTING.register("名称")` 等）。外部策略以 Python 包的 entry points 提供，
组名为 `cpc.routing`、`cpc.capacity`、`cpc.dispatch`、`cpc.power`（功率传感器为 `cpc.sensors`）：

```toml
# 策略包的 pyproject.toml
[project.entry-points."cpc.routing"]
greenest = "my_policies.routing:Greenest"
```

安装该包后，场景文件中即可以 `scheduler.routing: {policy: greenest}` 引用；与内置策略或其他包重名时加载报错。
