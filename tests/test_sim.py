"""离散事件仿真器：请求守恒、能耗非负、策略差异方向正确。"""

from cpc.sched import make_capacity, make_routing
from cpc.sim.des import Simulator


def run(scn, routing, capacity, hours=2.0, rate_scale=0.25):
    scn.workload.rate_scale = rate_scale  # 合成负载 12 请求/秒 × 0.25
    return Simulator(scn, make_routing(routing), make_capacity(capacity), hours=hours).run().summary


def test_conservation_and_energy(scn):
    s = run(scn, "nearest", "reactive")
    assert s["arrived"] == s["served"] + s["rejected"] + s["unrouted"] + s["unfinished"]
    assert s["served"] > 0 and s["facility_kwh"] > s["it_kwh"] > 0
    assert 0.0 <= s["slo_attainment"] <= 1.0


def test_static_uses_more_energy_than_reactive(scn):
    a = run(scn, "least_loaded", "static")
    b = run(scn, "least_loaded", "reactive")
    assert a["it_kwh"] > b["it_kwh"]  # 低负载时关闭空闲节点应节能


def test_route_reasons_reported(scn):
    s = run(scn, "nearest", "reactive", hours=1.0)
    assert sum(s["route_reasons"].values()) == s["arrived"]
    assert set(s["route_reasons"]) <= {"nearest", "overflow_wait_exceeded", "no_available_site"}
    assert s["energy_coverage"] == 1.0 and s["signal_coverage"] == 1.0
    assert s["workload"]["ref"] == "synthetic-poisson@1" and s["grid"]["price"] == "cn-tou-tariffs@2026-11"


def test_horizon_longer_than_workload_is_rejected(scn):
    import pytest

    scn.workload.duration_s = 1800
    with pytest.raises(ValueError, match="短于仿真时长"):
        run(scn, "nearest", "static", hours=1.0)
