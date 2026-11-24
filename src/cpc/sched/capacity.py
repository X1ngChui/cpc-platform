"""节点容量管理策略（插件）。

控制器每个控制周期调用 policy.decide(view)，得到各站点的目标在线节点数、功率封顶比例，
以及可选的路由权重（下发给网关，配合 weighted 路由策略使用）。

新增策略：继承 CapacityPolicy，实现 decide()，并用 @register_capacity("名字") 注册。
这里给出的是简单的参考实现，用来演示接口与数据通路。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..config import SLOConfig
from ..grid.signals import GridSignal
from ..schemas import CapacityTarget, DemandSnapshot, SiteSnapshot


@dataclass
class ControlView:
    now: datetime
    interval_s: float
    sites: dict[str, SiteSnapshot]
    signals: dict[str, GridSignal]
    demand: DemandSnapshot
    rtt_ms: dict[str, dict[str, float]]
    slo: SLOConfig
    min_active: dict[str, int] = field(default_factory=dict)


@dataclass
class CapacityPlan:
    targets: dict[str, CapacityTarget]
    weights: dict[str, dict[str, float]] | None = None
    notes: dict = field(default_factory=dict)


class CapacityPolicy:
    name = "base"

    def __init__(self, **params):
        self.params = params

    def decide(self, view: ControlView) -> CapacityPlan:
        raise NotImplementedError


CAPACITY_POLICIES: dict[str, type[CapacityPolicy]] = {}


def register_capacity(name: str) -> Callable[[type[CapacityPolicy]], type[CapacityPolicy]]:
    def deco(cls: type[CapacityPolicy]) -> type[CapacityPolicy]:
        cls.name = name
        CAPACITY_POLICIES[name] = cls
        return cls

    return deco


def make_capacity(name: str, params: dict | None = None) -> CapacityPolicy:
    if name not in CAPACITY_POLICIES:
        raise KeyError(f"未知容量策略 {name}，可选：{sorted(CAPACITY_POLICIES)}")
    return CAPACITY_POLICIES[name](**(params or {}))


def max_nodes_under_cap(s: SiteSnapshot, sig: GridSignal | None, cap_ratio: float = 1.0) -> int:
    """在设施功率上限下最多可开启的节点数（按节点满载功率保守估计）。"""
    if sig is None or sig.power_cap_kw is None or s.node_peak_w <= 0:
        return s.nodes_total
    it_cap_w = sig.power_cap_kw * 1000.0 / s.pue
    peak = s.node_idle_w + (s.node_peak_w - s.node_idle_w) * cap_ratio
    return max(0, min(s.nodes_total, int(it_cap_w // peak)))


@register_capacity("static")
class Static(CapacityPolicy):
    """全部节点常开、不封顶（基线）。"""

    def decide(self, view):
        return CapacityPlan({sid: CapacityTarget(active_nodes=s.nodes_total, cap_ratio=1.0) for sid, s in view.sites.items()})


@register_capacity("reactive")
class Reactive(CapacityPolicy):
    """阈值式自动伸缩（类似 Kubernetes HPA）：按 Little 定律估计所需并发，保持目标利用率。

    所需节点 = ceil(到达率 × 平均服务时间 / (每节点槽数 × 目标利用率))；
    扩容立即执行，缩容每周期最多减少 scale_down_step 个节点（防抖）。
    同时遵守电网下发的设施功率上限。
    """

    def __init__(self, target_util: float = 0.6, scale_down_step: int = 1, **params):
        super().__init__(**params)
        self.target_util = target_util
        self.step = scale_down_step

    def needed(self, s: SiteSnapshot) -> int:
        offered = max(s.arrival_rate * s.est_service_s, float(s.busy + s.queue_len))
        return math.ceil(offered / (max(s.slots_per_node, 1) * self.target_util))

    def decide(self, view):
        targets = {}
        for sid, s in view.sites.items():
            cur = s.nodes_on + s.nodes_booting
            want = self.needed(s)
            if want < cur:
                want = max(want, cur - self.step)
            lo = view.min_active.get(sid, 1)
            hi = max_nodes_under_cap(s, view.signals.get(sid))
            targets[sid] = CapacityTarget(active_nodes=max(min(want, hi), min(lo, hi)), cap_ratio=1.0)
        return CapacityPlan(targets)


@register_capacity("grid_aware")
class GridAware(CapacityPolicy):
    """电网信号感知的容量分配示例（配合 weighted 路由）。

    1. 由网关统计的总到达率估计全局所需节点数；
    2. 按"等效电价 × PUE"从低到高把节点分配给各站点（受节点总数、功率上限、RTT 预算约束），
       各站点至少保留 min_active 个节点；
    3. 各来源区域的路由权重 ∝ 其 RTT 预算内各站点分到的容量；
    4. 需求响应事件期间若功率上限不足以支撑最少节点，则降低 GPU 功率封顶比例。
    """

    def __init__(self, target_util: float = 0.6, rtt_budget_ms: float = 60.0, headroom: float = 1.1,
                 scale_down_step: int = 1, **params):
        super().__init__(**params)
        self.target_util = target_util
        self.rtt_budget_ms = rtt_budget_ms
        self.headroom = headroom
        self.step = scale_down_step

    def decide(self, view):
        sites, sig = view.sites, view.signals
        if not sites:
            return CapacityPlan({})
        any_site = next(iter(sites.values()))
        total_rate = max(sum(view.demand.rates.values()), sum(s.arrival_rate for s in sites.values()))
        svc = sum(s.est_service_s for s in sites.values()) / len(sites)
        offered = max(total_rate * svc, float(sum(s.busy + s.queue_len for s in sites.values())))  # Little 定律
        need = math.ceil(offered * self.headroom / (max(any_site.slots_per_node, 1) * self.target_util))

        alloc: dict[str, int] = {}
        caps: dict[str, float] = {}
        limit: dict[str, int] = {}
        for sid, s in sites.items():
            lo = view.min_active.get(sid, 1)
            hi = max_nodes_under_cap(s, sig.get(sid))
            cap_ratio = 1.0
            if hi < lo:  # 功率上限连最少节点都撑不住：降低封顶比例再算
                for r in (0.9, 0.8, 0.7, 0.6, 0.5):
                    cap_ratio = r
                    hi = max_nodes_under_cap(s, sig.get(sid), r)
                    if hi >= lo:
                        break
            caps[sid] = cap_ratio
            limit[sid] = hi
            alloc[sid] = min(lo, hi)
        remaining = max(0, need - sum(alloc.values()))

        def cost(sid: str) -> float:
            g = sig.get(sid)
            return (g.effective_price if g else 0.0) * sites[sid].pue

        for sid in sorted(sites, key=cost):
            add = min(remaining, limit[sid] - alloc[sid])
            alloc[sid] += add
            remaining -= add
        for sid, s in sites.items():  # 缩容防抖：每周期每站点最多减少 step 个节点（功率上限优先）
            cur = s.nodes_on + s.nodes_booting
            if alloc[sid] < cur - self.step:
                alloc[sid] = min(cur - self.step, limit[sid])

        weights: dict[str, dict[str, float]] = {}
        for src, row in view.rtt_ms.items():
            ok = {sid: alloc[sid] for sid in sites if row.get(sid, 1e9) <= self.rtt_budget_ms and alloc[sid] > 0}
            if not ok:  # RTT 预算内没有站点：退化为所有已分配站点
                ok = {sid: alloc[sid] for sid in sites if alloc[sid] > 0}
            tot = sum(ok.values()) or 1
            weights[src] = {sid: round(v / tot, 4) for sid, v in ok.items()}
        targets = {sid: CapacityTarget(active_nodes=alloc[sid], cap_ratio=caps[sid]) for sid in sites}
        return CapacityPlan(targets, weights, {"need_nodes": need, "unplaced": remaining})
