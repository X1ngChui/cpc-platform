#import "../../template/cpc-doc.typ": *

= 编写调度策略 <g-policy>

调度策略是 Python 类。平台在需要决策时调用策略的方法，传入当前状态（视图），策略返回决策。同一个策略类在仿真与实验中都能使用。
平台定义四类策略：

#figure(
  tbl(
    columns: (7em, 8em, 1fr),
    header: ("类别", "调用时机", "职责"),
    [路由策略], [每个请求到达网关], [为请求选择站点，或拒绝（`RoutingPolicy.choose`）],
    [容量策略], [每个决策周期], [给出各站点开启的服务器数、功率封顶比例，以及可选的路由权重（`CapacityPolicy.decide`）],
    [派发策略], [每个请求到达站点], [在站内选择服务器；决定排队顺序（`DispatchPolicy`）],
    [电源管理策略], [站点收到新的开启服务器数], [决定开关哪几台服务器（`PowerPolicy.plan`）],
  ),
  caption: [策略的类别],
)

== 内置策略

#figure(
  tbl(
    columns: (8em, 11em, 1fr),
    header: ("名称", "参数（缺省值）", "规则"),
    [*路由*], [], [],
    [`round_robin`], [—], [在可用站点间轮流选择],
    [`least_loaded`], [—], [选负载最低的站点],
    [`nearest`], [`max_wait_s`（0.5）、`overflow`], [按往返时延从近到远，选第一个估计排队时间不超过 `max_wait_s` 的站点],
    [`cost_aware`], [`max_load`（0.9）、`ttft_budget`（0.5）、`latency_weight`（0）、`overflow`], [在时延预算内选"等效电价 × PUE × 单请求能耗"最低的站点],
    [`weighted`], [`seed`（0）、`max_load`（1.0）、`overflow`], [按容量策略下发的权重随机分流],
    [*容量*], [], [],
    [`static`], [—], [全部服务器常开],
    [`reactive`], [`target_util`（0.6）、`scale_down_step`（1）], [按到达率与平均服务时间估计所需服务器数，保持目标利用率],
    [`grid_aware`], [`target_util`（0.6）、`rtt_budget_ms`（60）、`headroom`（1.1）、`scale_down_step`（1）], [按等效电价从低到高分配服务器，并给出路由权重（配合 `weighted` 路由）],
    [*派发*], [], [],
    [`least_loaded`], [—], [占用率最低的服务器（缺省）],
    [`packing`], [—], [占用率最高的可用服务器，使空闲服务器便于关机],
    [`efficient_first`], [—], [单位并发槽满载功率最低的服务器优先],
    [`affinity`], [`key`（`session_id`）], [同一请求属性值（如会话）的请求落在同一服务器上],
    [`priority`], [`key`（`priority`）], [排队时按请求属性的数值从大到小出队],
    [*电源管理*], [], [],
    [`ordered`], [—], [按配置顺序开机；关机时先停在服务请求最少的服务器（缺省）],
    [`efficient`], [—], [优先保留单位并发槽满载功率低的服务器],
  ),
  caption: [内置策略],
)

路由策略的 `overflow` 参数决定首选条件不满足时的处理：`least_loaded`（缺省，转向负载最低的可用站点）或 `reject`（拒绝请求）。
内置策略用于演示接口与数据通路，作为比较的基线。

== 策略能看到什么

*请求（`RequestInfo`）。* `req.source`（来源区域）、`req.input_tokens`、`req.output_tokens`、`req.id`、`req.attrs`
（场景 `workload.attributes` 选定的扩展字段，如 `req.attrs["session_id"]`）。

*集群视图（`ClusterView`，路由策略）。* `view.now`（信号时间）、`view.sites`（站点标识 → 站点视图，只含状态可用的站点）、
`view.rtt(source, site)`（往返时延，毫秒）、`view.slo`、`view.weights`（容量策略下发的路由权重）、
`view.est_ttft_s(req, site_view)`（估计的首 token 时延，不含预填充）。

*站点视图（`SiteView`）。* `sv.id`；`sv.signal`（电网信号，缺失时为 `None`）；`sv.load`（负载率：（在服务 + 排队 + 在途）/
在线并发槽数）；`sv.est_wait_s`（估计排队时间）；`sv.available`（有在线服务器且队列未满）；`sv.snap`（站点状态摘要）。

*站点状态摘要（`sv.snap`，容量策略的 `view.sites[站点]`）。* 常用字段：`nodes_total`、`nodes_on`、`nodes_booting`（服务器数）；
`slots_on`、`busy`、`queue_len`（在线并发槽数、正在处理的请求数、排队数）；`arrival_rate`、`est_service_s`（到达率与平均服务时间的估计）；
`it_power_w`、`facility_power_w`、`pue`；`node_idle_w`、`node_peak_w`、`slots_per_node`（服务器的空载、满载功率与并发槽数，
站内有多种服务器时为平均值）；`pools`（各服务器池的明细）。

*电网信号（`GridSignal`）。* `price`、`price_label`（时段名，如 `peak`）、`carbon_intensity`、`currency`、`power_cap_kw`
（设施功率上限，无上限时为 `None`）、`dr_event`、`pue`、`carbon_price`、`extra`（扩展信号）、`effective_price`
（电价 + 碳价 × 碳强度）。

*容量策略的视图（`ControlView`）。* `view.now`、`view.interval_s`、`view.sites`、`view.signals`（站点 → 电网信号）、
`view.demand.rates`（来源区域 → 到达率）、`view.rtt_ms`、`view.min_active`、`view.excluded`（本周期状态不可用的站点及原因）。

*服务器视图（`ServerView`，本地策略）。* `s.id`、`s.pool`、`s.state`（`off`、`booting`、`on`、`draining`）、`s.busy`、`s.slots`、
`s.cap_ratio`、`s.accepting`（是否可以接收新请求）、`s.peak_w`、`s.idle_w`。

== 第一个策略：选择碳强度最低的站点

以下步骤把一个路由策略写成独立的 Python 包，在仿真与实验中使用。

*第一步：创建包。* 在仓库之外新建目录：

```text
my-policies/
├── pyproject.toml
└── my_policies/
    ├── __init__.py          （空文件）
    └── routing.py
```

`my_policies/routing.py`：

```python
from cpc.sched import RouteDecision, RoutingPolicy


class Greenest(RoutingPolicy):
    """在有电网信号、负载低于 max_load 的可用站点中，选碳强度最低者。"""

    def __init__(self, max_load: float = 0.9, **params):
        super().__init__(**params)          # 场景中写错的参数名会在这里报错
        self.max_load = max_load

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)     # 可用、且本请求尚未尝试过的站点
        if not cands:
            return RouteDecision.reject("no_available_site")
        ok = [sv for sv in cands if sv.signal is not None and sv.load < self.max_load]
        if not ok:                                 # 没有满足条件的站点：转向负载最低者
            return self.overflow("least_loaded", cands, "no_candidate")
        best = min(ok, key=lambda sv: (sv.signal.carbon_intensity, sv.id))
        return RouteDecision.to(best.id, "min_carbon")
```

`RouteDecision.to(站点, 原因)` 表示选中某站点，`RouteDecision.reject(原因)` 表示拒绝。原因是简短的标识，
会出现在结果的 `route_reasons` 统计与指标中，便于分析策略在什么情况下做了什么决定。

*第二步：注册。* `pyproject.toml`：

```toml
[project]
name = "my-policies"
version = "0.1.0"
dependencies = ["cpc-platform"]

[project.entry-points."cpc.routing"]
greenest = "my_policies.routing:Greenest"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

`[project.entry-points."cpc.routing"]` 下的每一行把一个名称对应到一个策略类。容量、派发、电源管理策略分别使用组名
`cpc.capacity`、`cpc.dispatch`、`cpc.power`。

*第三步：安装并使用。* 在平台的虚拟环境中：

```bash
pip install -e ./my-policies
cpc sim -s examples/quickstart/scenario.yaml --hours 0.9 --routing greenest \
    --out results/greenest
```

或在场景中设置 `scheduler.routing: {policy: greenest, params: {max_load: 0.8}}`；在实验描述的变体中同样可以使用。
实验中使用同一个策略：`cpc live -s ... --routing greenest`，或 `cpc gateway --policy greenest`。修改策略代码后不需要重新安装
（`-e` 表示以可编辑方式安装）。

== 容量策略

容量策略在每个决策周期返回 `CapacityPlan`：各站点的 `CapacityTarget(active_nodes=开启的服务器数, cap_ratio=功率封顶比例)`，
可选的路由权重 `weights`（来源区域 → 站点 → 权重），以及写入决策记录的说明 `notes`。类属性 `requires` 声明需要的输入，
平台保证调用时这些输入齐全（缺少时跳过本周期并记录）：

```python
import math

from cpc.schemas import CapacityTarget
from cpc.sched import CapacityPlan, CapacityPolicy


class ByPrice(CapacityPolicy):
    """电价高于 threshold 的站点只保留 min_active 台服务器，其余站点按负载开启。"""

    # 需要电网信号；另一种输入为 "demand"（各来源区域的到达率）
    requires = frozenset({"signals"})

    def __init__(self, threshold: float = 1.0, target_util: float = 0.6, **params):
        super().__init__(**params)
        self.threshold, self.target_util = threshold, target_util

    def decide(self, view):
        targets = {}
        for sid, s in view.sites.items():
            busy = s.arrival_rate * s.est_service_s             # 估计的并发请求数
            need = math.ceil(busy / (s.slots_per_node * self.target_util))
            if view.signals[sid].price > self.threshold:
                need = 0
            n = max(need, view.min_active.get(sid, 1))
            targets[sid] = CapacityTarget(active_nodes=n)
        return CapacityPlan(targets)
```

注册到组 `cpc.capacity`。开启的服务器数会被截断到 0 与站点服务器总数之间；具体开关哪几台由站点的电源管理策略决定。

== 本地策略

派发策略的缺省流程是：用 `filter(req, server)` 筛出可以接收该请求的服务器，用 `score(req, server)` 打分，选分数最小者；
没有可选服务器时请求排队，`priority(req)` 决定排队顺序。通常只需重写 `score`：

```python
from cpc.sched import DispatchPolicy


class TenantPools(DispatchPolicy):
    """租户 a 的请求只使用服务器池 fast，其他请求在全部服务器中按占用率选择。"""

    def filter(self, req, s):
        return s.accepting and (req.attrs.get("tenant") != "a" or s.pool == "fast")

    def score(self, req, s):
        return s.busy / s.slots
```

注册到组 `cpc.dispatch`，在站点的 `scheduler.dispatch` 中引用（需要场景 `workload.attributes: [tenant]`）。
电源管理策略实现 `plan(want, servers)`，返回需要改变状态的服务器及其目标状态，如 `{"nm-h100-01": "off"}`；
注册到组 `cpc.power`。

== 编写策略的注意事项

- 只使用视图中的时间 `view.now`，不读取系统时钟，策略在仿真与实验中的行为才一致；
- 不修改视图中的对象；需要记忆的信息（如上一周期的决策）保存在策略对象自身的属性中；
- 电网信号可能缺失（`sv.signal is None`），路由策略应给出明确的处理方式与原因；
- 路由策略在每个请求上调用，应避免耗时的计算；
- 先在仿真中用较短的时长调试，再用 `cpc run` 与基线策略比较，最后在实验中验证。
