"""Grid-signal model (scenario level: site -> region, demand response, extra signals, startup checks),
server and network models."""

from datetime import datetime

import pytest

from cpc.config import CST, DREvent, ExtraSignal
from cpc.datasets import DatasetError
from cpc.grid.signals import GridModel
from cpc.models import power_w, rtt_matrix, service_s, speed_factor


def t(h, m=30, day=15):
    return datetime(2026, 7, day, h, m, tzinfo=CST)


def test_tou_prices_and_labels(scn):
    g = GridModel(scn)
    assert g.currency == "CNY"
    # Guangdong (Yue Fa Gai Jia Ge [2021] No. 331): in July 11-12 h is the sharp peak = flat x 1.7 x 1.25; 0-8 h valley x 0.38
    assert g.signal("gd", t(11)).price_label == "sharp"
    assert g.signal("gd", t(11)).price == pytest.approx(0.65 * 1.7 * 1.25)
    assert g.signal("gd", t(3)).price == pytest.approx(0.65 * 0.38)
    # Guizhou (Qian Fa Gai Jia Ge [2023] No. 481): peak 10-13 h and 17-22 h, valley 0-8 h
    assert [g.signal("gz", t(h)).price_label for h in (9, 10, 13, 17, 22, 7)] == ["flat", "peak", "flat", "peak", "flat", "valley"]
    # western Inner Mongolia: low-wind season (July) 13-15 h deep valley = flat x 0.44 x 0.8
    assert g.signal("nm", t(14)).price == pytest.approx(0.40 * 0.44 * 0.8)
    assert g.signal("sh", t(12)).price_label == "sharp"


def test_carbon_events_and_extras(scn):
    scn.grid.extras = [ExtraSignal(name="carbon_again", dataset=scn.grid.carbon.dataset, signal="carbon_intensity")]
    g = GridModel(scn)
    s = g.signal("gz", t(12))
    assert s.carbon_intensity == pytest.approx(0.5683) and s.extra == {"carbon_again": pytest.approx(0.5683)}
    assert s.effective_price == pytest.approx(s.price + 0.1 * 0.5683)
    assert g.signal("gd", t(15)).power_cap_kw == 8  # demand-response event of the scenario
    assert g.signal("gd", t(18)).power_cap_kw is None
    g.add_event(DREvent(site="nm", start=t(0, 0), end=t(1, 0), cap_kw=5))
    assert g.signal("nm", t(0)).dr_event == "peak_shaving"
    sig, missing = g.snapshot(datetime(2026, 8, 2, tzinfo=CST))  # outside the data: all sites missing
    assert not sig and set(missing) == {"sh", "gd", "gz", "nm"}


def test_grid_startup_checks(scn):
    scn.sites[0].region = "atlantis"
    with pytest.raises(DatasetError, match="atlantis"):
        GridModel(scn)
    scn.sites[0].region = "shanghai"
    scn.grid.carbon.signal = "price"  # unit is not kgCO2/kWh
    with pytest.raises(DatasetError, match="unit"):
        GridModel(scn)
    scn.grid.carbon.signal = "carbon_intensity"
    scn.clock.start = datetime(2026, 9, 1, tzinfo=CST)
    with pytest.raises(DatasetError, match="clock start"):
        GridModel(scn)


def test_power_and_perf_models(scn):
    p = scn.node_profiles["a100x8"]
    idle = power_w(p, "on", 0.0)
    half = power_w(p, "on", 0.5)
    full = power_w(p, "on", 1.0)
    capped = power_w(p, "on", 1.0, cap_ratio=0.6)
    assert idle < half < full and capped < full
    assert power_w(p, "off", 0.0) == p.off_w and power_w(p, "booting", 0.0) == idle == 1280
    assert capped == pytest.approx(1280 + 0.6 * (4700 - 1280)) and half == pytest.approx(3304.4)
    assert speed_factor(p, 0.6) < 1.0
    assert service_s(p, 800, 200, 1.0) > service_s(p, 800, 200, 0.0)


def test_rtt_matrix(scn):
    m = rtt_matrix(scn)
    assert m["east"]["sh"] < m["east"]["gz"]  # users in Shanghai are closest to the Shanghai site
    assert all(v > 0 for row in m.values() for v in row.values())


def test_table_driven_server_model(scn):
    from cpc.config import NodeProfile
    from cpc.models import tpot_s

    kw = dict(slots=8, prefill_tokens_per_s=1e4, boot_s=10)
    p = NodeProfile(power_w=[(0, 100), (0.1, 800), (0.5, 900), (1.0, 960)], tpot_s=[(0.1, 0.03), (1.0, 0.06)], **kw)
    assert power_w(p, "on", 0.0) == 100 and power_w(p, "on", 0.3) == pytest.approx(850)
    assert power_w(p, "on", 1.0, cap_ratio=0.9) == pytest.approx(100 + 0.9 * 860) and power_w(p, "on", 0.2, 0.9) == 825
    assert tpot_s(p, 0.55) == pytest.approx(0.045) and tpot_s(p, 0.0) == 0.03  # end points outside the table
    with pytest.raises(ValueError, match="increase strictly"):
        NodeProfile(power_w=[(0, 1.0), (0.5, 2.0), (0.2, 3.0)], tpot_s=[(1, 0.1)], **kw)
    with pytest.raises(ValueError, match="occupancy 0"):
        NodeProfile(power_w=[(0.1, 1.0), (1, 2.0)], tpot_s=[(1, 0.1)], **kw)
