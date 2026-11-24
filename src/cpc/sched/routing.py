"""请求路由策略（插件）。

网关对每个请求调用 policy.choose(req, view)，返回目标站点 id（或 None 表示拒绝）。
离散事件仿真器调用的是同一个接口，因此策略在仿真中验证后可直接用于在线平台。

新增策略：继承 RoutingPolicy，实现 choose()，并用 @register_routing("名字") 注册。
这里给出的都是简单的参考实现，用来演示接口与数据通路，不代表推荐算法。
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from ..config import SLOConfig
from ..grid.signals import GridSignal
from ..schemas import SiteSnapshot


@dataclass
class RequestInfo:
    source: str
    input_tokens: int = 0
    output_tokens: int = 0
    id: str = ""


@dataclass
class SiteView:
    snap: SiteSnapshot
    signal: GridSignal | None = None
    pending: int = 0  # 网关上次刷新状态后已派发、但站点快照中尚未体现的请求数

    @property
    def id(self) -> str:
        return self.snap.site

    @property
    def outstanding(self) -> int:
        return self.snap.busy + self.snap.queue_len + self.pending

    @property
    def load(self) -> float:
        """负载率 = (在服务 + 排队 + 在途) / 在线并发槽数。"""
        return self.outstanding / max(self.snap.slots_on, 1) if self.snap.slots_on else float("inf")

    @property
    def est_wait_s(self) -> float:
        """估计排队等待时间：超出并发槽的请求数 / 服务速率。"""
        if self.snap.slots_on == 0:
            return float("inf")
        excess = self.outstanding - self.snap.slots_on
        if excess < 0:
            return 0.0
        return (excess + 1) * self.snap.est_service_s / self.snap.slots_on

    @property
    def available(self) -> bool:
        s = self.snap
        return s.healthy and s.slots_on > 0 and (s.queue_len + self.pending) < s.queue_limit


@dataclass
class ClusterView:
    now: datetime
    sites: dict[str, SiteView]
    rtt_ms: dict[str, dict[str, float]]
    slo: SLOConfig
    weights: dict[str, dict[str, float]] = field(default_factory=dict)

    def rtt(self, source: str, site: str) -> float:
        return self.rtt_ms.get(source, {}).get(site, 0.0)

    def est_ttft_s(self, req: RequestInfo, sv: SiteView) -> float:
        """估计 TTFT（不含预填充）：往返时延 + 排队等待。"""
        return self.rtt(req.source, sv.id) / 1000.0 + sv.est_wait_s


class RoutingPolicy:
    name = "base"

    def __init__(self, **params):
        self.params = params

    def choose(self, req: RequestInfo, view: ClusterView, exclude: Iterable[str] = ()) -> str | None:
        raise NotImplementedError

    @staticmethod
    def candidates(view: ClusterView, exclude: Iterable[str] = ()) -> list[SiteView]:
        ex = set(exclude)
        return [sv for sid, sv in view.sites.items() if sid not in ex and sv.available]

    @staticmethod
    def least_loaded(cands: list[SiteView]) -> str | None:
        return min(cands, key=lambda sv: sv.load).id if cands else None


ROUTING_POLICIES: dict[str, type[RoutingPolicy]] = {}


def register_routing(name: str) -> Callable[[type[RoutingPolicy]], type[RoutingPolicy]]:
    def deco(cls: type[RoutingPolicy]) -> type[RoutingPolicy]:
        cls.name = name
        ROUTING_POLICIES[name] = cls
        return cls

    return deco


def make_routing(name: str, params: dict | None = None) -> RoutingPolicy:
    if name not in ROUTING_POLICIES:
        raise KeyError(f"未知路由策略 {name}，可选：{sorted(ROUTING_POLICIES)}")
    return ROUTING_POLICIES[name](**(params or {}))


@register_routing("round_robin")
class RoundRobin(RoutingPolicy):
    """轮询（基线）。"""

    def __init__(self, **params):
        super().__init__(**params)
        self._it = itertools.count()

    def choose(self, req, view, exclude=()):
        cands = sorted(self.candidates(view, exclude), key=lambda sv: sv.id)
        return cands[next(self._it) % len(cands)].id if cands else None


@register_routing("random")
class RandomPolicy(RoutingPolicy):
    """均匀随机（基线）。"""

    def __init__(self, seed: int = 0, **params):
        super().__init__(**params)
        self.rng = random.Random(seed)

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)
        return self.rng.choice(cands).id if cands else None


@register_routing("least_loaded")
class LeastLoaded(RoutingPolicy):
    """最小负载率（基线）。"""

    def choose(self, req, view, exclude=()):
        return self.least_loaded(self.candidates(view, exclude))


@register_routing("nearest")
class Nearest(RoutingPolicy):
    """就近：按 RTT 从近到远，选第一个估计排队时间低于阈值的站点；都超阈值时选负载最低的。"""

    def __init__(self, max_wait_s: float = 0.5, **params):
        super().__init__(**params)
        self.max_wait_s = max_wait_s

    def choose(self, req, view, exclude=()):
        cands = sorted(self.candidates(view, exclude), key=lambda sv: view.rtt(req.source, sv.id))
        for sv in cands:
            if sv.est_wait_s <= self.max_wait_s:
                return sv.id
        return self.least_loaded(cands)


@register_routing("cost_aware")
class CostAware(RoutingPolicy):
    """能耗成本感知的贪心示例：在满足 TTFT 预算与负载上限的站点中，选单位请求边际成本最低者。

    边际成本 = 等效电价（电价 + 碳价×碳强度）× PUE × 单请求边际能耗 + latency_weight × 估计 TTFT。
    单请求边际能耗 ≈ (节点满载功率 − 空载功率) / 并发槽数 × 平均服务时间。
    """

    def __init__(self, max_load: float = 0.9, ttft_budget: float = 0.5, latency_weight: float = 0.0, **params):
        super().__init__(**params)
        self.max_load = max_load
        self.ttft_budget = ttft_budget  # 占 SLO 的比例
        self.latency_weight = latency_weight  # 元/秒

    @staticmethod
    def marginal_cost(sv: SiteView) -> float:
        s, g = sv.snap, sv.signal
        if g is None:
            return 0.0
        e_kwh = (s.node_peak_w - s.node_idle_w) / max(s.slots_per_node, 1) * s.est_service_s / 3.6e6
        return g.effective_price * s.pue * e_kwh

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)
        budget = self.ttft_budget * view.slo.ttft_s
        ok = [sv for sv in cands if sv.load < self.max_load and view.est_ttft_s(req, sv) <= budget]
        if not ok:
            return self.least_loaded(cands)
        return min(ok, key=lambda sv: self.marginal_cost(sv) + self.latency_weight * view.est_ttft_s(req, sv)).id


@register_routing("weighted")
class Weighted(RoutingPolicy):
    """按控制器下发的权重（来源→站点的流量比例）随机分流；目标不可用时退化为最小负载。"""

    def __init__(self, seed: int = 0, max_load: float = 1.0, **params):
        super().__init__(**params)
        self.rng = random.Random(seed)
        self.max_load = max_load

    def choose(self, req, view, exclude=()):
        cands = {sv.id: sv for sv in self.candidates(view, exclude)}
        w = {k: v for k, v in view.weights.get(req.source, {}).items() if k in cands and v > 0}
        if not w:
            return self.least_loaded(list(cands.values()))
        sid = self.rng.choices(list(w), weights=list(w.values()))[0]
        if cands[sid].load >= self.max_load:  # 目标已满载：改投负载最低的站点
            return self.least_loaded(list(cands.values()))
        return sid
