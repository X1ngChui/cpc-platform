"""电网信号与基础模型：检查数据是否按政策文件正确解析、模型是否满足基本单调性。"""

from datetime import datetime

import pytest

from cpc.config import CST, DREvent
from cpc.grid.signals import GridModel
from cpc.models import node_power_w, rtt_matrix, service_s, speed_factor


def t(h, m=30, month=7, day=15):
    return datetime(2026, month, day, h, m, tzinfo=CST)


def test_tou_periods(scn):
    g = GridModel(scn)
    # 广东（粤发改价格〔2021〕331号）：7 月 11–12 点为尖峰 = 平段 × 1.7 × 1.25；0–8 点低谷 × 0.38
    assert g.signal("gd", t(11)).period == "sharp"
    assert g.signal("gd", t(11)).price_yuan_per_kwh == pytest.approx(0.65 * 1.7 * 1.25)
    assert g.signal("gd", t(3)).price_yuan_per_kwh == pytest.approx(0.65 * 0.38)
    assert g.signal("gd", t(11, month=10)).period == "peak"  # 10 月无尖峰
    # 贵州（黔发改价格〔2023〕481号）：峰 10–13、17–22，谷 0–8，上下浮 60%
    assert [g.signal("gz", t(h)).period for h in (9, 10, 13, 17, 22, 7)] == ["flat", "peak", "flat", "peak", "flat", "valley"]
    # 蒙西：小风季（7 月）13–15 点深谷 = 平段 × 0.44 × 0.8
    assert g.signal("nm", t(14)).price_yuan_per_kwh == pytest.approx(0.40 * 0.44 * 0.8)
    # 上海夏季：12–14 点尖峰（7、8 月），9 月无尖峰
    assert g.signal("sh", t(12)).period == "sharp" and g.signal("sh", t(12, month=9)).period == "peak"


def test_carbon_and_events(scn):
    g = GridModel(scn)
    assert g.signal("gz", t(12)).carbon_kg_per_kwh == pytest.approx(0.5683)  # 2023 年贵州省级因子
    assert g.signal("gd", t(15)).power_cap_kw == 8  # 场景中的需求响应事件
    assert g.signal("gd", t(18)).power_cap_kw is None
    g.add_event(DREvent(site="nm", start=t(0, 0), end=t(1, 0), cap_kw=5))
    assert g.signal("nm", t(0)).dr_event == "peak_shaving"


def test_synthetic_carbon_mean_preserved(scn):
    scn.grid.carbon.dataset, scn.grid.carbon.params = "synthetic-diurnal-carbon", {"amplitude": 0.1}
    g = GridModel(scn)
    vals = [g.signal("gz", datetime(2026, 7, 15, h, m, tzinfo=CST)).carbon_kg_per_kwh for h in range(24) for m in range(0, 60, 10)]
    assert sum(vals) / len(vals) == pytest.approx(0.5683, rel=0.01)


def test_power_and_perf_models(scn):
    p = scn.profile_of("sh")
    idle, _ = node_power_w(p, "on", 0.0)
    half, _ = node_power_w(p, "on", 0.5)
    full, _ = node_power_w(p, "on", 1.0)
    capped, _ = node_power_w(p, "on", 1.0, cap_ratio=0.6)
    assert idle < half < full and capped < full
    assert node_power_w(p, "off", 0.0)[0] == p.standby_w
    assert speed_factor(p, 0.6) < 1.0
    assert service_s(p, 800, 200, 1.0) > service_s(p, 800, 200, 0.0)


def test_rtt_matrix(scn):
    m = rtt_matrix(scn)
    assert m["east"]["sh"] < m["east"]["gz"]  # 上海用户访问上海站点最近
    assert all(v > 0 for row in m.values() for v in row.values())
