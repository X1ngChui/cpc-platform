"""节点容量管理策略（插件）。

控制器每个控制周期调用 policy.decide(view)，得到各站点的目标在线节点数、功率封顶比例，
以及可选的路由权重（下发给网关，配合 weighted 路由策略使用）。

输入完整性：策略在类属性 requires 中声明所需的输入（signals：各站点的电网信号；demand：网关的需求统计）。
控制器只在所需输入齐全时调用 decide()，否则跳过本周期并记录原因；状态不可达的站点不出现在 view.sites 中，
其原因见 view.excluded。因此策略可以直接按站点索引 view.signals，无需处理缺失。

新增策略：继承 CapacityPolicy，实现 decide()，声明 requires，并用 @register_capacity("名字") 注册。
这里给出的是简单的参考实现，用来演示接口与数据通路。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, ClassVar, Literal

from ..config import SLOConfig
from ..grid.signals import GridSignal
from ..schemas import CapacityTarget, DemandSnapshot, SiteSnapshot

INPUTS = frozenset({"signals", "demand"})


@dataclass
class ControlView:
    now: datetime
    interval_s: float
    sites: dict[str, SiteSnapshot]  # 本周期状态可用的站点
    signals: dict[str, GridSignal]  # 策略声明需要 signals 时，覆盖 sites 中的全部站点
    demand: DemandSnapshot | None  # 策略声明需要 demand 时不为 None
    rtt_ms: dict[str, dict[str, float]]
    slo: SLOConfig
    min_active: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)  # 本周期被排除的站点及原因


@dataclass
class CapacityPlan:
    targets: dict[str, CapacityTarget]
    weights: dict[str, dict[str, float]] | None = None
    notes: dict = field(default_factory=dict)


class CapacityPolicy:
    name = "base"
    requires: ClassVar[frozenset[str]] = INPUTS

    def __init__(self, **params):
        if params:
            raise TypeError(f"容量策略 {self.name} 不接受参数 {sorted(params)}")
        self.params = params

    def decide(self, view: ControlView) -> CapacityPlan:
        raise NotImplementedError


CAPACITY_POLICIES: dict[str, type[CapacityPolicy]] = {}


def register_capacity(name: str) -> Callable[[type[CapacityPolicy]], type[CapacityPolicy]]:
    def deco(cls: type[CapacityPolicy]) -> type[CapacityPolicy]:
        unknown = set(cls.requires) - INPUTS
        if unknown:
            raise ValueError(f"容量策略 {name} 声明了未知输入 {sorted(unknown)}，可选 {sorted(INPUTS)}")
        cls.name = name
        CAPACITY_POLICIES[name] = cls
        return cls

    return deco


def make_capacity(name: str, params: dict | None = None) -> CapacityPolicy:
    if name not in CAPACITY_POLICIES:
        raise KeyError(f"未知容量策略 {name}，可选：{sorted(CAPACITY_POLICIES)}")
    return CAPACITY_POLICIES[name](**(params or {}))


def node_cap_peak_w(s: SiteSnapshot, cap_ratio: float) -> float:
    """封顶比例 cap_ratio 下单节点的满载 IT 功率（按节点配置参数线性折算）。"""
    return s.node_idle_w + (s.node_peak_w - s.node_idle_w) * cap_ratio


def max_nodes_under_cap(s: SiteSnapshot, sig: GridSignal, cap_ratio: float = 1.0) -> int:
    """在设施功率上限下最多可开启的节点数（按节点满载功率保守估计）。信号中没有上限时为节点总数。"""
    if sig.power_cap_kw is None:
        return s.nodes_total
    peak = node_cap_peak_w(s, cap_ratio)
    if peak <= 0:
        raise ValueError(f"站点 {s.site} 的节点满载功率参数无效：{peak}")
    it_cap_w = sig.power_cap_kw * 1000.0 / s.pue
    return max(0, min(s.nodes_total, int(it_cap_w // peak)))


def cap_ratio_for(s: SiteSnapshot, sig: GridSignal, nodes: int, min_ratio: float) -> float | None:
    """使 nodes 个节点满载时设施功率不超过上限的最大封顶比例；即使取 min_ratio 也不满足时返回 None。"""
    if sig.power_cap_kw is None or nodes <= 0 or max_nodes_under_cap(s, sig, 1.0) >= nodes:
        return 1.0
    per_node_w = sig.power_cap_kw * 1000.0 / s.pue / nodes
    dyn = s.node_peak_w - s.node_idle_w
    if dyn <= 0:
        return None
    r = (per_node_w - s.node_idle_w) / dyn
    return None if r < min_ratio else min(1.0, math.floor(r * 1000) / 1000)


@register_capacity("static")
class Static(CapacityPolicy):
    """全部节点常开、不封顶（基线）。不需要电网信号与需求统计。"""

    requires = frozenset()

    def decide(self, view):
        return CapacityPlan({sid: CapacityTarget(active_nodes=s.nodes_total, cap_ratio=1.0) for sid, s in view.sites.items()})


@register_capacity("reactive")
class Reactive(CapacityPolicy):
    """阈值式自动伸缩（类似 Kubernetes HPA）：按 Little 定律估计所需并发，保持目标利用率。

    所需节点 = ceil(max(站点到达率 × 平均服务时间, 在服务 + 排队) / (每节点槽数 × 目标利用率))；
    扩容立即执行，缩容每周期最多减少 scale_down_step 个节点（防抖）。
    同时遵守电网下发的设施功率上限（需要电网信号）；上限低于 min_active 时以上限为准，并在 notes 中记录。
    """

    requires = frozenset({"signals"})

    def __init__(self, target_util: float = 0.6, scale_down_step: int = 1, **params):
        super().__init__(**params)
        self.target_util = target_util
        self.step = scale_down_step

    def needed(self, s: SiteSnapshot) -> int:
        offered = max(s.arrival_rate * s.est_service_s, float(s.busy + s.queue_len))
        return math.ceil(offered / (max(s.slots_per_node, 1) * self.target_util))

    def decide(self, view):
        targets, capped = {}, {}
        for sid, s in view.sites.items():
            cur = s.nodes_on + s.nodes_booting
            want = self.needed(s)
            if want < cur:
                want = max(want, cur - self.step)
            lo = view.min_active.get(sid, 1)
            hi = max_nodes_under_cap(s, view.signals[sid])
            if hi < lo:
                capped[sid] = {"min_active": lo, "max_under_cap": hi}
            targets[sid] = CapacityTarget(active_nodes=max(min(want, hi), min(lo, hi)), cap_ratio=1.0)
        return CapacityPlan(targets, notes={"cap_below_min_active": capped} if capped else {})


@register_capacity("grid_aware")
class GridAware(CapacityPolicy):
    """电网信号感知的容量分配示例（配合 weighted 路由）。

    1. 由到达率（demand_source=gateway：网关统计的各来源到达率之和；sites：各站点到达率之和）按 Little 定律估计全局所需并发槽数；
    2. 按"等效电价 × PUE"从低到高把节点分配给各站点（按各站点每节点槽数折算；受节点总数、功率上限约束），
       各站点至少保留 min_active 个节点；
    3. 各来源区域的路由权重 ∝ 其 RTT 预算内各站点分到的容量；
    4. 功率上限不足以支撑 min_active 个满载节点时，取使其满足上限的最大封顶比例（不低于站点快照中的 cap_min_ratio）；
       仍不满足时按上限可容纳的节点数开启，并在 notes 中记录。
    预算内没有已分配站点的来源，其权重覆盖全部已分配站点，并在 notes.rtt_budget_exceeded 中记录。
    """

    requires = frozenset({"signals", "demand"})

    def __init__(self, target_util: float = 0.6, rtt_budget_ms: float = 60.0, headroom: float = 1.1,
                 scale_down_step: int = 1, demand_source: Literal["gateway", "sites"] = "gateway", **params):
        super().__init__(**params)
        if demand_source not in ("gateway", "sites"):
            raise ValueError(f"demand_source 只能为 gateway 或 sites，当前为 {demand_source}")
        self.target_util = target_util
        self.rtt_budget_ms = rtt_budget_ms
        self.headroom = headroom
        self.step = scale_down_step
        self.demand_source = demand_source

    def decide(self, view):
        sites, sig = view.sites, view.signals
        if not sites:
            return CapacityPlan({})
        if self.demand_source == "gateway":
            total_rate = sum(view.demand.rates.values())
        else:
            total_rate = sum(s.arrival_rate for s in sites.values())
        svc = sum(s.est_service_s for s in sites.values()) / len(sites)
        offered = max(total_rate * svc, float(sum(s.busy + s.queue_len for s in sites.values())))  # Little 定律
        need_slots = offered * self.headroom / self.target_util  # 所需并发槽数；各站点每节点槽数可以不同

        alloc: dict[str, int] = {}
        caps: dict[str, float] = {}
        limit: dict[str, int] = {}
        infeasible: dict[str, dict] = {}
        for sid, s in sites.items():
            lo = view.min_active.get(sid, 1)
            ratio = cap_ratio_for(s, sig[sid], lo, s.cap_min_ratio)
            if ratio is None:
                ratio = s.cap_min_ratio
                infeasible[sid] = {"min_active": lo, "max_under_cap": max_nodes_under_cap(s, sig[sid], ratio)}
            caps[sid] = ratio
            limit[sid] = max_nodes_under_cap(s, sig[sid], ratio)
            alloc[sid] = min(lo, limit[sid])
        remaining = max(0.0, need_slots - sum(alloc[sid] * max(sites[sid].slots_per_node, 1) for sid in sites))

        def cost(sid: str) -> tuple[float, str]:
            return sig[sid].effective_price * sites[sid].pue, sid

        for sid in sorted(sites, key=cost):
            per = max(sites[sid].slots_per_node, 1)
            add = min(math.ceil(remaining / per), limit[sid] - alloc[sid])
            alloc[sid] += add
            remaining = max(0.0, remaining - add * per)
        for sid, s in sites.items():  # 缩容防抖：每周期每站点最多减少 step 个节点（功率上限优先）
            cur = s.nodes_on + s.nodes_booting
            if alloc[sid] < cur - self.step:
                alloc[sid] = min(cur - self.step, limit[sid])

        weights: dict[str, dict[str, float]] = {}
        over_budget: list[str] = []
        for src, row in view.rtt_ms.items():
            ok = {sid: alloc[sid] for sid in sites if row[sid] <= self.rtt_budget_ms and alloc[sid] > 0}
            if not ok:
                over_budget.append(src)
                ok = {sid: alloc[sid] for sid in sites if alloc[sid] > 0}
            tot = sum(ok.values())
            weights[src] = {sid: round(v / tot, 4) for sid, v in ok.items()} if tot else {}
        targets = {sid: CapacityTarget(active_nodes=alloc[sid], cap_ratio=caps[sid]) for sid in sites}
        notes = {"need_slots": round(need_slots, 2), "unplaced_slots": round(remaining, 2), "demand_source": self.demand_source}
        if infeasible:
            notes["cap_infeasible"] = infeasible
        if over_budget:
            notes["rtt_budget_exceeded"] = over_budget
        return CapacityPlan(targets, weights, notes)
