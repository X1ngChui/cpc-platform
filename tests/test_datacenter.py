"""数据中心运行时（仿真与实验共用）：排队与派发、容量指令与电源管理、能耗计量、站内策略。"""

from __future__ import annotations

import pytest

from cpc.config import NodeProfile, PoolConfig, SiteConfig
from cpc.datacenter import Datacenter, Job, Overloaded
from cpc.models import power_w
from cpc.schemas import CapacityTarget
from cpc.sched import RequestInfo, make_dispatch, make_power

FAST = NodeProfile(slots=2, power_w=[(0, 150), (1, 500)], tpot_s=[(0, 0.02), (1, 0.03)], prefill_tokens_per_s=1e4, boot_s=10)
SLOW = NodeProfile(slots=2, power_w=[(0, 180), (1, 800)], tpot_s=[(0, 0.02), (1, 0.03)], prefill_tokens_per_s=1e4, boot_s=10)


def make_dc(pools=(("a", "fast", 2),), dispatch="least_loaded", power="ordered", qlps=1.0, **kw):
    cfg = SiteConfig(id="s", name="s", region="r", lat=0, lon=0, pools=[PoolConfig(id=i, profile=p, count=n) for i, p, n in pools])
    started, actions = [], []
    dc = Datacenter(cfg, {"fast": FAST, "slow": SLOW}, dispatch=make_dispatch(dispatch, kw.pop("dparams", None)),
                    power=make_power(power), queue_limit_per_slot=qlps, on_start=lambda j, s, t: started.append((j, s, t)),
                    on_action=actions.append, **kw)
    return dc, started, actions


def job(k=0, **attrs):
    return Job(RequestInfo(source="x", input_tokens=10, output_tokens=5, id=str(k), attrs=attrs))


def test_dispatch_queue_and_overload():
    dc, started, _ = make_dc(qlps=1.0)  # 2 台 × 2 槽；队列上限 = 4 × 1
    jobs = [job(k) for k in range(8)]
    for j in jobs[:4]:
        dc.submit(j, 0.0)
    assert len(started) == 4 and dc.summary(0.0).busy == 4
    assert sorted(s.busy for s in dc.servers) == [2, 2]  # least_loaded 均衡
    for j in jobs[4:]:
        dc.submit(j, 1.0)
    assert dc.queue_len == 4
    with pytest.raises(Overloaded):
        dc.submit(job(9), 1.0)
    dc.cancel(jobs[4])
    assert dc.queue_len == 3
    dc.finish(started[0][1], 2.0, service_s=2.0)
    assert started[-1][0] is jobs[5] and jobs[5].t_queued == 1.0 and dc.queue_len == 2  # 撤销的请求被跳过


def test_priority_queue_order():
    dc, started, _ = make_dc(pools=(("a", "fast", 1),), dispatch="priority")
    for k in range(2):
        dc.submit(job(k), 0.0)
    low, high = job(10, priority=1), job(11, priority=5)
    dc.submit(low, 0.0)
    dc.submit(high, 0.0)
    dc.finish(started[0][1], 1.0)
    assert started[-1][0] is high


def test_capacity_drain_boot_and_power():
    dc, started, actions = make_dc()
    dc.submit(job(), 0.0)
    busy = started[0][1]
    dc.apply(CapacityTarget(active_nodes=1), 1.0)
    assert [s.state for s in dc.servers].count("on") == 1 and busy.state == "on"  # 先停空闲服务器
    assert [a.kind for a in actions] == ["power_off"]
    dc.apply(CapacityTarget(active_nodes=0), 2.0)
    assert busy.state == "draining"
    dc.finish(busy, 3.0)
    assert busy.state == "off" and actions[-1].kind == "power_off" and dc.summary(3.0).it_power_w == 0.0
    dc.apply(CapacityTarget(active_nodes=2, cap_ratio=0.7), 4.0)
    assert {s.state for s in dc.servers} == {"booting"} and all(s.boot_until == 14.0 for s in dc.servers)
    assert sorted(a.kind for a in actions[-4:]) == ["power_on", "power_on", "set_cap", "set_cap"]
    dc.boot_complete(dc.servers[0], 14.0)
    assert dc.servers[0].state == "on" and dc.summary(14.0).cap_ratio == 0.7
    dc.actuation_failed(dc.servers[1], "power_on", "RuntimeError: no container", 14.0)
    assert dc.servers[1].state == "off" and dc.actuation_errors[(dc.servers[1].id, "power_on")] == 1


def test_energy_metering_and_missing_power():
    dc, _, _ = make_dc(pools=(("a", "fast", 1),))
    s = dc.servers[0]
    idle = power_w(FAST, "on", 0.0)
    m = dc.meter(10.0)
    assert m.energy_j == pytest.approx(idle * 10) and m.live_s == 10.0
    s.model_power = False  # 改为实测：驱动方在 advance 之后写入读数
    dc.advance(20.0)
    s.it_power_w = None
    m = dc.meter(25.0)
    assert m.missing_s == 5.0 and m.energy_j == pytest.approx(idle * 20)
    snap = dc.summary(25.0)
    assert snap.it_power_w is None and snap.energy_coverage == pytest.approx(20 / 25)


def test_heterogeneous_pools_and_local_policies():
    dc, started, _ = make_dc(pools=(("old", "slow", 2), ("new", "fast", 2)), dispatch="efficient_first", power="efficient")
    dc.submit(job(), 0.0)
    assert started[0][1].pool == "new"  # 单位并发槽满载功率低的服务器优先
    dc.apply(CapacityTarget(active_nodes=2), 1.0)
    assert {s.pool for s in dc.servers if s.state == "on"} == {"new"}  # 缩容时先停低效服务器
    snap = dc.summary(1.0)
    assert [p.id for p in snap.pools] == ["old", "new"] and snap.pools[1].on == 2
    assert snap.node_peak_w == pytest.approx((dc.servers[0].peak_w + dc.servers[2].peak_w) / 2)


def test_affinity_dispatch_uses_request_attributes():
    dc, started, _ = make_dc(pools=(("a", "fast", 4),), dispatch="affinity", dparams={"key": "session_id"})
    for _ in range(2):
        dc.submit(job(session_id="u1"), 0.0)
    assert started[0][1] is started[1][1]  # 同一会话落在同一服务器
    with pytest.raises(TypeError):
        make_dispatch("least_loaded", {"bogus": 1})


def test_rate_estimate_is_cadence_independent():
    a, _, _ = make_dc(rate_window_s=60.0, qlps=100.0)
    b, _, _ = make_dc(rate_window_s=60.0, qlps=100.0)
    for k in range(600):  # 10 个/秒，持续 60 秒
        t = k / 10
        for dc in (a, b):
            dc._arrivals += 1
        if k % 10 == 9:
            a.update_rate(t + 0.1)
        if k % 300 == 299:
            b.update_rate(t + 0.1)
    assert a.arrival_rate == pytest.approx(b.arrival_rate, rel=0.05) and 5 < a.arrival_rate < 10
