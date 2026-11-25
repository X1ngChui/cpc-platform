"""离散事件仿真器：请求守恒、能耗、策略差异方向、异构服务器池、结果数据包与实验描述。"""

import json

import pyarrow.parquet as pq
import pytest
import yaml

from cpc.cli import main as cli_main
from cpc.config import ConfigError, PoolConfig
from cpc.datasets import load_descriptor
from cpc.sched import make_capacity, make_routing
from cpc.sim.des import Simulator


def run(scn, routing, capacity, hours=2.0, rate_scale=0.25):
    scn.workload.rate_scale = rate_scale  # 合成负载 12 请求/秒 × 0.25
    return Simulator(scn, make_routing(routing), make_capacity(capacity), hours=hours).run()


def test_conservation_and_energy(scn):
    s = run(scn, "nearest", "reactive").summary
    assert s["arrived"] == s["served"] + s["rejected"] + s["unrouted"] + s["unfinished"]
    assert s["served"] > 0 and s["facility_kwh"] > s["it_kwh"] > 0
    assert 0.0 <= s["slo_attainment"] <= 1.0 and s["currency"] == "CNY"


def test_static_uses_more_energy_than_reactive(scn):
    a = run(scn, "least_loaded", "static").summary
    b = run(scn, "least_loaded", "reactive").summary
    assert a["it_kwh"] > b["it_kwh"]  # 低负载时关闭空闲服务器应节能


def test_route_reasons_and_provenance(scn):
    s = run(scn, "nearest", "reactive", hours=1.0).summary
    assert sum(s["route_reasons"].values()) == s["arrived"]
    assert set(s["route_reasons"]) <= {"nearest", "overflow_wait_exceeded", "no_available_site"}
    assert s["signal_coverage"] == 1.0
    assert s["workload"]["ref"] == "workload-synthetic@1" and s["grid"]["price"] == "cn-grid-2026-07@2026-07-01_2026-08-01:price"


def test_heterogeneous_pools_with_local_policies(scn):
    scn.node_profiles["old"] = scn.node_profiles["a100x8"].model_copy(update={"power_w": [(0, 1500), (1, 5500)]})
    for s in scn.sites:
        s.pools = [PoolConfig(id="new", profile="a100x8", count=2), PoolConfig(id="old", profile="old", count=2)]
        s.scheduler.dispatch.policy = "efficient_first"
        s.scheduler.power.policy = "efficient"
    res = run(scn, "least_loaded", "reactive", hours=1.0)
    served = res.requests["status"] == "served"
    servers = set(res.requests["server"][served])
    assert servers and all("-new-" in x for x in servers)  # 低负载时只使用高效服务器


def test_horizon_longer_than_workload_is_rejected(scn):
    scn.workload.duration_s = 1800
    with pytest.raises(ConfigError, match="短于仿真时长"):
        run(scn, "nearest", "static", hours=1.0)


def test_experiment_run_writes_result_packages(tmp_path, capsys):
    from conftest import TEST_SCENARIO

    exp = {"name": "t", "scenario": str(TEST_SCENARIO), "hours": 0.5,
           "variants": [{"name": "base", "workload": {"rate_scale": 0.1}},
                        {"name": "cost", "routing": {"policy": "cost_aware"}, "workload": {"rate_scale": 0.1}}]}
    p = tmp_path / "exp.yaml"
    p.write_text(yaml.safe_dump(exp), encoding="utf-8")
    cli_main(["run", str(p), "--jobs", "1", "--out", str(tmp_path / "out")])
    run_dir = tmp_path / "out" / "cost"
    d = yaml.safe_load((run_dir / "datapackage.yaml").read_text(encoding="utf-8"))
    assert d["cpc"]["kind"] == "run_result" and {r["name"] for r in d["resources"]} >= {"requests", "timeseries", "summary"}
    req = pq.read_table(run_dir / "requests.parquet")
    summ = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert req.num_rows == summ["arrived"] and summ["routing"] == "cost_aware"
    meta = yaml.safe_load((run_dir / "run.yaml").read_text(encoding="utf-8"))
    assert meta["routing"]["policy"] == "cost_aware" and meta["scenario"]["workload"]["rate_scale"] == 0.1
    assert (tmp_path / "out" / "comparison.csv").read_text(encoding="utf-8").count("\n") == 3
    with pytest.raises(Exception):
        load_descriptor(run_dir)  # 结果数据包不是平台输入数据集（cpc.kind = run_result）
