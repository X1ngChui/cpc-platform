"""跨站点路由策略（全局调度，插件）。

网关对每个请求调用 policy.choose(req, view, exclude)，得到结构化的路由决策 RouteDecision：
选择的站点（或 None 表示拒绝）及原因。原因写入请求日志与指标 cpc_gateway_route_decisions_total{policy,reason}。
离散事件仿真器调用的是同一个接口，因此策略在仿真中验证后可直接用于在线平台。

视图中只包含状态可用的站点（网关在站点状态拉取失败或过期时将其移出视图）；
电网信号缺失时 SiteView.signal 为 None，依赖信号的策略须把这类站点排除在外，而不是按缺省值计算。

新增策略：继承 RoutingPolicy，实现 choose()；在自己的包中以 entry point（组 cpc.routing）提供，见 cpc.registry。
内置策略是用于演示接口与数据通路的参考实现。
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal

from ..config import SLOConfig
from ..grid.signals import GridSignal
from ..registry import Registry
from ..schemas import SiteSnapshot


@dataclass
class RequestInfo:
    """策略可见的请求信息。attrs 为负载数据集的扩展字段（场景 workload.attributes 选定），实验中由 x-cpc-attr-* 请求头携带。"""

    source: str
    input_tokens: int = 0  # 实验中为近似值（见 site.executors.approx_prompt_tokens）
    output_tokens: int | None = None  # 请求给出的输出上限；未给出时为 None
    id: str = ""
    attrs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    """路由决策：site 为 None 表示拒绝。reason 为有限取值的短标识（用作指标标签），detail 为补充信息。"""

    site: str | None
    reason: str
    detail: dict = field(default_factory=dict)

    @classmethod
    def to(cls, site: str, reason: str, **detail) -> "RouteDecision":
        return cls(site, reason, detail)

    @classmethod
    def reject(cls, reason: str, **detail) -> "RouteDecision":
        return cls(None, reason, detail)


@dataclass
class SiteView:
    snap: SiteSnapshot
    signal: GridSignal | None = None  # 电网信号缺失时为 None
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
        return s.slots_on > 0 and (s.queue_len + self.pending) < s.queue_limit


@dataclass
class ClusterView:
    now: datetime
    sites: dict[str, SiteView]
    rtt_ms: dict[str, dict[str, float]]
    slo: SLOConfig
    weights: dict[str, dict[str, float]] = field(default_factory=dict)

    def rtt(self, source: str, site: str) -> float:
        return self.rtt_ms[source][site]

    def est_ttft_s(self, req: RequestInfo, sv: SiteView) -> float:
        """估计 TTFT（不含预填充）：往返时延 + 排队等待。"""
        return self.rtt(req.source, sv.id) / 1000.0 + sv.est_wait_s


Overflow = Literal["least_loaded", "reject"]


class RoutingPolicy:
    name = "base"

    def __init__(self, **params):
        if params:
            raise TypeError(f"路由策略 {self.name} 不接受参数 {sorted(params)}")
        self.params = params

    def choose(self, req: RequestInfo, view: ClusterView, exclude: Iterable[str] = ()) -> RouteDecision:
        raise NotImplementedError

    @staticmethod
    def candidates(view: ClusterView, exclude: Iterable[str] = ()) -> list[SiteView]:
        """视图中有在线并发槽且排队未满、且不在 exclude 中的站点。"""
        ex = set(exclude)
        return [sv for sid, sv in view.sites.items() if sid not in ex and sv.available]

    @staticmethod
    def least_loaded(cands: list[SiteView]) -> str | None:
        return min(cands, key=lambda sv: (sv.load, sv.id)).id if cands else None

    def overflow(self, mode: Overflow, cands: list[SiteView], cause: str) -> RouteDecision:
        """策略的首选条件不满足时的显式溢出规则：选负载最低的可用站点，或拒绝。原因中注明触发条件。"""
        if mode == "reject":
            return RouteDecision.reject(cause)
        sid = self.least_loaded(cands)
        return RouteDecision.to(sid, f"overflow_{cause}") if sid else RouteDecision.reject("no_available_site")


ROUTING: Registry[RoutingPolicy] = Registry("routing")
register_routing = ROUTING.register


def make_routing(name: str, params: dict | None = None) -> RoutingPolicy:
    return ROUTING.get(name)(**(params or {}))


def _check_overflow(v: str) -> Overflow:
    if v not in ("least_loaded", "reject"):
        raise ValueError(f"overflow 只能为 least_loaded 或 reject，当前为 {v}")
    return v  # type: ignore[return-value]


@register_routing("round_robin")
class RoundRobin(RoutingPolicy):
    """轮询（基线）。"""

    def __init__(self, **params):
        super().__init__(**params)
        self._it = itertools.count()

    def choose(self, req, view, exclude=()):
        cands = sorted(self.candidates(view, exclude), key=lambda sv: sv.id)
        if not cands:
            return RouteDecision.reject("no_available_site")
        return RouteDecision.to(cands[next(self._it) % len(cands)].id, "round_robin")


@register_routing("random")
class RandomPolicy(RoutingPolicy):
    """均匀随机（基线）。"""

    def __init__(self, seed: int = 0, **params):
        super().__init__(**params)
        self.rng = random.Random(seed)

    def choose(self, req, view, exclude=()):
        cands = sorted(self.candidates(view, exclude), key=lambda sv: sv.id)
        if not cands:
            return RouteDecision.reject("no_available_site")
        return RouteDecision.to(self.rng.choice(cands).id, "random")


@register_routing("least_loaded")
class LeastLoaded(RoutingPolicy):
    """最小负载率（基线）。"""

    def choose(self, req, view, exclude=()):
        sid = self.least_loaded(self.candidates(view, exclude))
        return RouteDecision.to(sid, "least_loaded") if sid else RouteDecision.reject("no_available_site")


@register_routing("nearest")
class Nearest(RoutingPolicy):
    """就近：按 RTT 从近到远，选第一个估计排队时间不超过 max_wait_s 的站点；
    都超过时按 overflow 处理（least_loaded：选负载最低者；reject：拒绝）。"""

    def __init__(self, max_wait_s: float = 0.5, overflow: str = "least_loaded", **params):
        super().__init__(**params)
        self.max_wait_s = max_wait_s
        self.mode = _check_overflow(overflow)

    def choose(self, req, view, exclude=()):
        cands = sorted(self.candidates(view, exclude), key=lambda sv: view.rtt(req.source, sv.id))
        if not cands:
            return RouteDecision.reject("no_available_site")
        for sv in cands:
            if sv.est_wait_s <= self.max_wait_s:
                return RouteDecision.to(sv.id, "nearest")
        return self.overflow(self.mode, cands, "wait_exceeded")


@register_routing("cost_aware")
class CostAware(RoutingPolicy):
    """能耗成本感知的贪心示例：在有电网信号、满足 TTFT 预算与负载上限的站点中，选单位请求边际成本最低者。

    边际成本 = 等效电价（电价 + 碳价×碳强度）× PUE × 单请求边际能耗 + latency_weight × 估计 TTFT。
    单请求边际能耗 ≈ (服务器满载功率 − 空载功率) / 并发槽数 × 平均服务时间（取站点内服务器的平均值）。
    没有站点满足条件时按 overflow 处理；原因区分"无电网信号"（no_signal）与"预算或负载不满足"（over_budget）。
    """

    def __init__(self, max_load: float = 0.9, ttft_budget: float = 0.5, latency_weight: float = 0.0,
                 overflow: str = "least_loaded", **params):
        super().__init__(**params)
        self.max_load = max_load
        self.ttft_budget = ttft_budget  # 占 SLO 的比例
        self.latency_weight = latency_weight  # 货币单位/秒
        self.mode = _check_overflow(overflow)

    @staticmethod
    def marginal_cost(sv: SiteView) -> float:
        s, g = sv.snap, sv.signal
        assert g is not None
        e_kwh = (s.node_peak_w - s.node_idle_w) / max(s.slots_per_node, 1) * s.est_service_s / 3.6e6
        return g.effective_price * s.pue * e_kwh

    def choose(self, req, view, exclude=()):
        cands = self.candidates(view, exclude)
        if not cands:
            return RouteDecision.reject("no_available_site")
        signed = [sv for sv in cands if sv.signal is not None]
        if not signed:
            return self.overflow(self.mode, cands, "no_signal")
        budget = self.ttft_budget * view.slo.ttft_s
        ok = [sv for sv in signed if sv.load < self.max_load and view.est_ttft_s(req, sv) <= budget]
        if not ok:
            return self.overflow(self.mode, cands, "over_budget")
        best = min(ok, key=lambda sv: (self.marginal_cost(sv) + self.latency_weight * view.est_ttft_s(req, sv), sv.id))
        return RouteDecision.to(best.id, "min_cost", unsigned=len(cands) - len(signed))


@register_routing("weighted")
class Weighted(RoutingPolicy):
    """按控制器下发的权重（来源→站点的流量比例）随机分流。

    该来源没有可用站点的正权重（no_weights），或抽中的站点负载率不低于 max_load（target_full）时，按 overflow 处理。
    """

    def __init__(self, seed: int = 0, max_load: float = 1.0, overflow: str = "least_loaded", **params):
        super().__init__(**params)
        self.rng = random.Random(seed)
        self.max_load = max_load
        self.mode = _check_overflow(overflow)

    def choose(self, req, view, exclude=()):
        cands = {sv.id: sv for sv in self.candidates(view, exclude)}
        if not cands:
            return RouteDecision.reject("no_available_site")
        w = {k: v for k, v in sorted(view.weights.get(req.source, {}).items()) if k in cands and v > 0}
        if not w:
            return self.overflow(self.mode, list(cands.values()), "no_weights")
        sid = self.rng.choices(list(w), weights=list(w.values()))[0]
        if cands[sid].load >= self.max_load:
            return self.overflow(self.mode, list(cands.values()), "target_full")
        return RouteDecision.to(sid, "weighted")
