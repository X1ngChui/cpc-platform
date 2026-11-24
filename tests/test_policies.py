"""全局调度策略（路由、容量）的参考实现：决策、原因与输入完整性。"""

import math
from datetime import datetime, timedelta, timezone

import pytest

from cpc.datacenter import Datacenter
from cpc.grid.signals import GridModel
from cpc.models import rtt_matrix
from cpc.schemas import DemandSnapshot
from cpc.sched import ClusterView, ControlView, RequestInfo, SiteView, make_capacity, make_dispatch, make_power, make_routing
from cpc.sched.capacity import cap_ratio_for, max_nodes_under_cap

CST = timezone(timedelta(hours=8))


def summary(scn, site):
    cfg = scn.site(site)
    dc = Datacenter(cfg, scn.node_profiles, dispatch=make_dispatch("least_loaded"), power=make_power("ordered"))
    return dc.summary(0.0)


# ------------------------------------------------------------------ 路由策略


def _view(scn, loads: dict[str, int], weights=None, signals=True, drop=()):
    g = GridModel(scn)
    now = datetime(2026, 7, 15, 11, 30, tzinfo=CST)
    sv = {}
    for s in scn.sites:
        if s.id in drop:
            continue
        snap = summary(scn, s.id).model_copy(update={"busy": loads.get(s.id, 0)})
        sv[s.id] = SiteView(snap=snap, signal=g.signal(s.id, now) if signals else None)
    return ClusterView(now=now, sites=sv, rtt_ms=rtt_matrix(scn), slo=scn.slo, weights=weights or {})


def test_routing_decisions_and_reasons(scn):
    req = RequestInfo(source="east", input_tokens=800, output_tokens=200)
    d = make_routing("nearest").choose(req, _view(scn, {}))
    assert (d.site, d.reason) == ("sh", "nearest")
    # 所有站点估计排队都超过阈值 → 显式溢出：选负载最低者（原因注明），或按配置拒绝
    busy = {s.id: 300 for s in scn.sites}
    d = make_routing("nearest").choose(req, _view(scn, busy))
    assert d.reason == "overflow_wait_exceeded" and d.site is not None
    d = make_routing("nearest", {"overflow": "reject"}).choose(req, _view(scn, busy))
    assert d.site is None and d.reason == "wait_exceeded"
    # 11:30 广东尖峰、上海高峰、贵州高峰、蒙西低谷 → 成本感知选蒙西（RTT 在预算内）
    d = make_routing("cost_aware").choose(req, _view(scn, {}))
    assert (d.site, d.reason) == ("nm", "min_cost")
    # 没有电网信号：成本无从计算，不按 0 处理；按溢出规则处理并给出原因
    d = make_routing("cost_aware").choose(req, _view(scn, {}, signals=False))
    assert d.reason == "overflow_no_signal"
    assert make_routing("cost_aware", {"overflow": "reject"}).choose(req, _view(scn, {}, signals=False)).site is None
    d = make_routing("weighted").choose(req, _view(scn, {}, {"east": {"gz": 1.0}}))
    assert (d.site, d.reason) == ("gz", "weighted")
    assert make_routing("weighted").choose(req, _view(scn, {})).reason == "overflow_no_weights"
    # 视图中没有站点（全部不可用）
    d = make_routing("least_loaded").choose(req, _view(scn, {}, drop=[s.id for s in scn.sites]))
    assert d.site is None and d.reason == "no_available_site"
    with pytest.raises(TypeError):
        make_routing("nearest", {"max_wait": 1})  # 参数拼写错误在构造时报错


# ------------------------------------------------------------------ 容量策略


def _control_view(scn, hour=15, minute=30):
    g = GridModel(scn)
    now = datetime(2026, 7, 15, hour, minute, tzinfo=CST)  # 15:30 在广东需求响应事件期间（上限 8 kW）
    sites = {s.id: summary(scn, s.id) for s in scn.sites}
    return ControlView(now=now, interval_s=300, sites=sites, signals=g.snapshot(now)[0],
                       demand=DemandSnapshot(rates={"east": 4, "south": 4, "north": 2}),
                       rtt_ms=rtt_matrix(scn), slo=scn.slo, min_active={s.id: 1 for s in scn.sites})


def test_capacity_policies(scn):
    view = _control_view(scn)
    assert make_capacity("static").requires == frozenset()
    assert make_capacity("reactive").requires == {"signals"}
    assert make_capacity("grid_aware").requires == {"signals", "demand"}
    plan = make_capacity("reactive").decide(view)
    assert plan.targets["gd"].active_nodes <= 1  # 8 kW / 1.25 / 4.7 kW ≈ 1 台服务器
    plan = make_capacity("grid_aware").decide(view)
    assert plan.targets["gd"].active_nodes <= 1
    assert all(abs(sum(w.values()) - 1) < 1e-3 for w in plan.weights.values())
    assert plan.notes["demand_source"] == "gateway"


def test_grid_aware_cap_ratio_under_tight_cap(scn):
    view = _control_view(scn)
    s, sig = view.sites["gd"], view.signals["gd"].model_copy(update={"power_cap_kw": 4.0})
    # 4 kW / 1.25 = 3.2 kW IT，低于单台服务器满载 4.7 kW：求使 1 个节点满足上限的最大封顶比例
    r = cap_ratio_for(s, sig, 1, s.cap_min_ratio)
    assert r is not None and s.cap_min_ratio <= r < 1.0
    assert max_nodes_under_cap(s, sig, r) >= 1 and max_nodes_under_cap(s, sig, 1.0) == 0
    tight = sig.model_copy(update={"power_cap_kw": 1.0})  # 低于空载功率：任何封顶比例都不可行
    assert cap_ratio_for(s, tight, 1, s.cap_min_ratio) is None
    view.signals["gd"] = tight
    plan = make_capacity("grid_aware").decide(view)
    assert plan.targets["gd"].active_nodes == 0 and "gd" in plan.notes["cap_infeasible"]


def test_rtt_budget_violation_is_reported(scn):
    plan = make_capacity("grid_aware", {"rtt_budget_ms": 1.0}).decide(_control_view(scn))
    assert set(plan.notes["rtt_budget_exceeded"]) == {s.id for s in scn.sources}
    assert not math.isnan(sum(sum(w.values()) for w in plan.weights.values()))
