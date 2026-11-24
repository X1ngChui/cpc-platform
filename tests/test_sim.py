"""离散事件仿真器：请求守恒、能耗非负、策略差异方向正确。"""

from cpc.sched import make_capacity, make_routing
from cpc.sim.des import Simulator


def run(scn, routing, capacity, hours=2.0, rps=3.0):
    return Simulator(scn, make_routing(routing), make_capacity(capacity), hours=hours, rps=rps).run().summary


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
    assert s["energy_coverage"] == 1.0
