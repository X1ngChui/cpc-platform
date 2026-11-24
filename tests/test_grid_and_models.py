"""电网信号模型（场景层：站点 → 区域、需求响应、扩展信号、启动检查）与服务器、网络模型。"""

from datetime import datetime

import pytest

from cpc.config import CST, DREvent, ExtraSignal
from cpc.datasets import DatasetError
from cpc.grid.signals import GridModel
from cpc.models import node_power_w, rtt_matrix, service_s, speed_factor


def t(h, m=30, day=15):
    return datetime(2026, 7, day, h, m, tzinfo=CST)


def test_tou_prices_and_labels(scn):
    g = GridModel(scn)
    assert g.currency == "CNY"
    # 广东（粤发改价格〔2021〕331号）：7 月 11–12 点为尖峰 = 平段 × 1.7 × 1.25；0–8 点低谷 × 0.38
    assert g.signal("gd", t(11)).price_label == "sharp"
    assert g.signal("gd", t(11)).price == pytest.approx(0.65 * 1.7 * 1.25)
    assert g.signal("gd", t(3)).price == pytest.approx(0.65 * 0.38)
    # 贵州（黔发改价格〔2023〕481号）：峰 10–13、17–22，谷 0–8
    assert [g.signal("gz", t(h)).price_label for h in (9, 10, 13, 17, 22, 7)] == ["flat", "peak", "flat", "peak", "flat", "valley"]
    # 蒙西：小风季（7 月）13–15 点深谷 = 平段 × 0.44 × 0.8
    assert g.signal("nm", t(14)).price == pytest.approx(0.40 * 0.44 * 0.8)
    assert g.signal("sh", t(12)).price_label == "sharp"


def test_carbon_events_and_extras(scn):
    scn.grid.extras = [ExtraSignal(name="carbon_again", dataset=scn.grid.carbon.dataset, signal="carbon_intensity")]
    g = GridModel(scn)
    s = g.signal("gz", t(12))
    assert s.carbon_intensity == pytest.approx(0.5683) and s.extra == {"carbon_again": pytest.approx(0.5683)}
    assert s.effective_price == pytest.approx(s.price + 0.1 * 0.5683)
    assert g.signal("gd", t(15)).power_cap_kw == 8  # 场景中的需求响应事件
    assert g.signal("gd", t(18)).power_cap_kw is None
    g.add_event(DREvent(site="nm", start=t(0, 0), end=t(1, 0), cap_kw=5))
    assert g.signal("nm", t(0)).dr_event == "peak_shaving"
    sig, missing = g.snapshot(datetime(2026, 8, 2, tzinfo=CST))  # 数据覆盖范围之外：全部站点缺失
    assert not sig and set(missing) == {"sh", "gd", "gz", "nm"}


def test_grid_startup_checks(scn):
    scn.sites[0].region = "atlantis"
    with pytest.raises(DatasetError, match="atlantis"):
        GridModel(scn)
    scn.sites[0].region = "shanghai"
    scn.grid.carbon.signal = "price"  # 单位不是 kgCO2/kWh
    with pytest.raises(DatasetError, match="单位"):
        GridModel(scn)
    scn.grid.carbon.signal = "carbon_intensity"
    scn.clock.start = datetime(2026, 9, 1, tzinfo=CST)
    with pytest.raises(DatasetError, match="时钟起点"):
        GridModel(scn)


def test_power_and_perf_models(scn):
    p = scn.node_profiles["a100x8"]
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


def test_table_driven_server_model(scn):
    from cpc.config import NodeProfile
    from cpc.models import gpu_power_w, tpot_s

    p = NodeProfile(gpus=4, gpu_idle_w=100, gpu_power_curve=[(0.1, 800), (0.5, 900), (1.0, 960)],
                    tpot_curve=[(0.1, 0.03), (1.0, 0.06)])
    assert gpu_power_w(p, 0.0) == 100 and gpu_power_w(p, 0.3) == pytest.approx(850)
    assert gpu_power_w(p, 1.0, cap_ratio=0.9) < 960 and gpu_power_w(p, 0.05) == 800  # 低于首点取端点值
    assert tpot_s(p, 0.55) == pytest.approx(0.045)
    with pytest.raises(ValueError, match="递增"):
        NodeProfile(gpu_power_curve=[(0.5, 1.0), (0.2, 2.0)])
