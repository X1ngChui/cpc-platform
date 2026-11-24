"""站点代理与调度策略：并发槽/排队/容量指令的状态机，以及参考策略的基本行为。"""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from cpc.config import CST
from cpc.grid.signals import GridModel
from cpc.schemas import CapacityTarget, DemandSnapshot
from cpc.sched import ClusterView, ControlView, RequestInfo, SiteView, make_capacity, make_routing
from cpc.site.core import Overloaded, SiteCore
from cpc.site.power import ModelPower
from cpc.site.service import create_app


def make_core(scn, site="sh", qlps=1.0):
    cfg = scn.site(site)
    prof = scn.profile_of(site)
    return SiteCore(cfg, prof, ModelPower(prof), queue_limit_per_slot=qlps)


def test_slots_queue_and_overload(scn):
    async def run():
        core = make_core(scn, qlps=0.25)  # 4 节点 × 16 槽，排队上限 = 64 × 0.25 = 16
        nodes = [await core.acquire() for _ in range(64)]
        assert core.snapshot().busy == 64
        waiters = [asyncio.create_task(core.acquire()) for _ in range(16)]
        await asyncio.sleep(0)
        assert core.snapshot().queue_len == 16
        try:
            await core.acquire()
            raise AssertionError("应当拒绝")
        except Overloaded:
            pass
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


def test_site_service_openai_api(scn):
    scn.node_profiles["default"].decode_s_per_token = 0.001
    with TestClient(create_app(scn, "gz")) as c:
        r = c.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi " * 50}], "max_tokens": 8})
        assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 8
        assert r.headers["x-cpc-site"] == "gz"
        with c.stream("POST", "/v1/completions", json={"prompt": "a b c", "max_tokens": 20, "stream": True}) as s:
            body = b"".join(s.iter_bytes())
        assert body.endswith(b"data: [DONE]\n\n")
        assert c.post("/admin/capacity", json={"active_nodes": 2}).json()["nodes_on"] == 2
        m = c.get("/metrics").text
        assert "DCGM_FI_DEV_POWER_USAGE{" in m and "cpc_site_facility_power_watts" in m


def _view(scn, loads: dict[str, int], weights=None):
    g = GridModel(scn)
    now = datetime(2026, 7, 15, 11, 30, tzinfo=CST)
    sv = {}
    for s in scn.sites:
        core = make_core(scn, s.id)
        snap = core.snapshot().model_copy(update={"busy": loads.get(s.id, 0)})
        sv[s.id] = SiteView(snap=snap, signal=g.signal(s.id, now))
    from cpc.models import rtt_matrix

    return ClusterView(now=now, sites=sv, rtt_ms=rtt_matrix(scn), slo=scn.slo, weights=weights or {})


def test_routing_policies(scn):
    req = RequestInfo(source="east", input_tokens=800, output_tokens=200)
    assert make_routing("nearest").choose(req, _view(scn, {})) == "sh"
    # 上海站点已满（64 槽全忙且排队超阈值）→ 就近策略溢出到其他站点
    busy = _view(scn, {"sh": 200})
    assert make_routing("nearest").choose(req, busy) != "sh"
    # 11:30 广东尖峰、上海高峰、贵州高峰、蒙西低谷 → 成本感知选蒙西（RTT 在预算内）
    assert make_routing("cost_aware").choose(req, _view(scn, {})) == "nm"
    w = make_routing("weighted").choose(req, _view(scn, {}, {"east": {"gz": 1.0}}))
    assert w == "gz"


def test_capacity_policies(scn):
    g = GridModel(scn)
    now = datetime(2026, 7, 15, 15, 30, tzinfo=CST)  # 广东需求响应事件期间（上限 8 kW）
    sites = {s.id: make_core(scn, s.id).snapshot() for s in scn.sites}
    view = ControlView(now=now, interval_s=300, sites=sites, signals=g.snapshot(now),
                       demand=DemandSnapshot(rates={"east": 4, "south": 4, "north": 2}),
                       rtt_ms={}, slo=scn.slo, min_active={s.id: 1 for s in scn.sites})
    plan = make_capacity("reactive").decide(view)
    assert plan.targets["gd"].active_nodes <= 1  # 8 kW / 1.25 / 4.7 kW ≈ 1 个节点
    plan = make_capacity("grid_aware").decide(view)
    assert plan.targets["gd"].active_nodes <= 1
    assert all(abs(sum(w.values()) - 1) < 1e-3 for w in plan.weights.values())
