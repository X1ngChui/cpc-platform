"""站点代理与调度策略：并发槽/排队/容量指令的状态机、故障记录，以及参考策略的决策与原因。"""

import asyncio
import math
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from cpc.config import CST
from cpc.grid.signals import GridModel
from cpc.models import rtt_matrix
from cpc.schemas import CapacityTarget, DemandSnapshot
from cpc.sched import ClusterView, ControlView, RequestInfo, SiteView, make_capacity, make_routing
from cpc.sched.capacity import cap_ratio_for, max_nodes_under_cap
from cpc.site.core import NoopHooks, Overloaded, SiteCore
from cpc.site.power import ModelPower
from cpc.site.service import create_app


def make_core(scn, site="sh", qlps=1.0, power=None, hooks=None):
    cfg = scn.site(site)
    prof = scn.profile_of(site)
    return SiteCore(cfg, prof, power or ModelPower(prof), hooks, queue_limit_per_slot=qlps)


def test_slots_queue_and_overload(scn):
    async def run():
        core = make_core(scn, qlps=0.25)  # 4 节点 × 16 槽，排队上限 = 64 × 0.25 = 16
        nodes = [await core.acquire() for _ in range(64)]
        assert core.snapshot().busy == 64
        waiters = [asyncio.create_task(core.acquire()) for _ in range(16)]
        await asyncio.sleep(0)
        assert core.snapshot().queue_len == 16
        with pytest.raises(Overloaded):
            await core.acquire()
        core.release(nodes[0], 1.0)  # 释放一个槽 → 队首请求获得节点
        await asyncio.sleep(0)
        assert waiters[0].done() and core.snapshot().queue_len == 15
        for w in waiters[1:]:
            w.cancel()

    asyncio.run(run())


def test_capacity_apply_drain_and_boot(scn):
    async def run():
        core = make_core(scn)
        n = await core.acquire()
        core.apply(CapacityTarget(active_nodes=1))
        s = core.snapshot()
        assert s.nodes_on == 1 and s.nodes_draining == 0  # 优先关闭空闲节点，保留有在途请求的节点
        assert [x.busy for x in core.nodes if x.state == "on"] == [1]
        core.apply(CapacityTarget(active_nodes=0))
        assert core.snapshot().nodes_draining == 1  # 有在途请求的节点先排空
        core.release(n, 1.0)
        s = core.snapshot()
        assert s.nodes_draining == 0 and s.nodes_on == 0 and s.it_power_w == 0.0
        core.apply(CapacityTarget(active_nodes=3, cap_ratio=0.7))
        s = core.snapshot()
        assert s.nodes_on == 0 and s.nodes_booting == 3 and s.cap_ratio == 0.7

    asyncio.run(run())


class FailingHooks(NoopHooks):
    async def power_on(self, node):
        raise RuntimeError("container not found")


def test_actuation_failure_is_recorded(scn):
    async def run():
        core = make_core(scn, hooks=FailingHooks())
        core.apply(CapacityTarget(active_nodes=0))
        core.apply(CapacityTarget(active_nodes=1))
        await asyncio.sleep(0.01)
        n = core.nodes[0]
        # 开机失败：节点回到 off，错误记入节点状态与失败计数，不假装成功
        assert n.state == "off" and "container not found" in n.last_error
        assert core.actuation_errors[(n.id, "power_on")] == 1
        assert core.snapshot().nodes[0].last_error.startswith("power_on")

    asyncio.run(run())


class GappyPower(ModelPower):
    """前 missing 次采样功率缺失的功率来源。"""

    def __init__(self, profile, missing):
        super().__init__(profile)
        self.missing = missing

    def sample(self, nodes):
        super().sample(nodes)
        if self.missing > 0:
            self.missing -= 1
            for n in nodes:
                n.it_power_w = None
                n.power_missing = ["host"]


def test_energy_coverage_excludes_missing_power(scn):
    prof = scn.profile_of("sh")
    core = make_core(scn, power=GappyPower(prof, missing=2))  # 构造时采样一次（缺失），之后再缺失一次
    s = core.snapshot()
    assert s.it_power_w is None and s.facility_power_w is None and s.energy_coverage is None
    core._last_t -= 1.0
    core.resample()  # [t-1, t) 的左端点功率缺失 → 不积分；本次采样仍缺失
    core._last_t -= 1.0
    core.resample()  # 左端点仍缺失；本次采样恢复
    core._last_t -= 2.0
    core.resample()  # 左端点有效 → 积分 2 s
    s = core.snapshot()
    assert s.energy_coverage == pytest.approx(0.5, abs=1e-3)
    assert s.energy_it_j == pytest.approx(2.0 * s.it_power_w, rel=1e-3)


def test_site_service_api_and_readiness(scn):
    scn.node_profiles["default"].decode_s_per_token = 0.001
    with TestClient(create_app(scn, "gz")) as c:
        assert c.get("/healthz").json()["status"] == "alive"
        t0 = time.time()
        while c.get("/readyz").status_code != 200 and time.time() - t0 < 3:
            time.sleep(0.05)
        assert c.get("/readyz").status_code == 200
        r = c.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi " * 50}], "max_tokens": 8})
        assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 8
        assert r.headers["x-cpc-site"] == "gz" and "x-cpc-energy-est-j" in r.headers
        # 仿真后端要求请求给出输出上限：缺失时返回 400，而不是按缺省长度计时
        r = c.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
        with c.stream("POST", "/v1/completions", json={"prompt": "a b c", "max_tokens": 20, "stream": True}) as s:
            body = b"".join(s.iter_bytes())
        assert body.endswith(b"data: [DONE]\n\n")
        assert c.post("/admin/capacity", json={"active_nodes": 2}).json()["nodes_on"] == 2
        m = c.get("/metrics").text
        assert "DCGM_FI_DEV_POWER_USAGE{" in m and "cpc_site_facility_power_watts" in m and "cpc_site_energy_coverage_ratio" in m
        st = c.get("/state").json()
        assert st["power_scope"] == "node" and st["energy_coverage"] == pytest.approx(1.0)


# ------------------------------------------------------------------ 路由策略


def _view(scn, loads: dict[str, int], weights=None, signals=True, drop=()):
    g = GridModel(scn)
    now = datetime(2026, 7, 15, 11, 30, tzinfo=CST)
    sv = {}
    for s in scn.sites:
        if s.id in drop:
            continue
        snap = make_core(scn, s.id).snapshot().model_copy(update={"busy": loads.get(s.id, 0)})
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
    sites = {s.id: make_core(scn, s.id).snapshot() for s in scn.sites}
    return ControlView(now=now, interval_s=300, sites=sites, signals=g.snapshot(now)[0],
                       demand=DemandSnapshot(rates={"east": 4, "south": 4, "north": 2}),
                       rtt_ms=rtt_matrix(scn), slo=scn.slo, min_active={s.id: 1 for s in scn.sites})


def test_capacity_policies(scn):
    view = _control_view(scn)
    assert make_capacity("static").requires == frozenset()
    assert make_capacity("reactive").requires == {"signals"}
    assert make_capacity("grid_aware").requires == {"signals", "demand"}
    plan = make_capacity("reactive").decide(view)
    assert plan.targets["gd"].active_nodes <= 1  # 8 kW / 1.25 / 4.7 kW ≈ 1 个节点
    plan = make_capacity("grid_aware").decide(view)
    assert plan.targets["gd"].active_nodes <= 1
    assert all(abs(sum(w.values()) - 1) < 1e-3 for w in plan.weights.values())
    assert plan.notes["demand_source"] == "gateway"


def test_grid_aware_cap_ratio_under_tight_cap(scn):
    view = _control_view(scn)
    s, sig = view.sites["gd"], view.signals["gd"].model_copy(update={"power_cap_kw": 4.0})
    # 4 kW / 1.25 = 3.2 kW IT，低于单节点满载 4.7 kW：求使 1 个节点满足上限的最大封顶比例
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
