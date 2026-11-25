"""命令行与 Python 接口：场景检查、仿真与结果读取、作图、实验运行（cpc live）。"""

from __future__ import annotations

import pytest

import cpc
from cpc.cli import main as cli_main

from conftest import TEST_SCENARIO


def test_check_prints_overview(capsys):
    cli_main(["check", "-s", str(TEST_SCENARIO), "--step", "6"])
    out = capsys.readouterr().out
    assert "场景检查通过" in out and "a100x8" in out and "往返时延" in out and "workload-synthetic@1" in out
    assert "1.105/peak/" in out and "/sharp/" in out  # 12:30 广东高峰（0.65 × 1.7），上海尖峰
    cli_main(["check", "-s", str(TEST_SCENARIO), "--date", "2026-09-01"])
    assert "缺失" in capsys.readouterr().out  # 电网数据覆盖范围之外的时刻标为缺失


def test_simulate_api_and_report(tmp_path):
    res = cpc.simulate(TEST_SCENARIO, routing="least_loaded", capacity="reactive", hours=1, rate_scale=0.1, out=tmp_path / "r")
    s = res.summary
    assert s["routing"] == "least_loaded" and s["served"] > 0 and s["arrived"] == len(res.requests["arrival_s"])
    run = cpc.read_run(tmp_path / "r")
    assert run["summary"]["served"] == s["served"] and run["requests"].num_rows == s["arrived"]
    assert run["run"]["mode"] == "simulation" and run["timeseries"]
    with pytest.raises(TypeError, match="rate"):
        cpc.simulate(TEST_SCENARIO, hours=1, rates=1)
    cli_main(["report", str(tmp_path / "r")])
    assert (tmp_path / "r" / "run.png").is_file()


def test_live_experiment_writes_result_package(tmp_path, capsys):
    out = tmp_path / "live"
    cli_main(["live", "-s", str(TEST_SCENARIO), "--duration", "8", "--rate-scale", "0.1", "--sample-s", "2",
              "--speedup", "60", "--interval", "3", "--out", str(out)])
    text = capsys.readouterr().out
    assert "实验完成" in text
    run = cpc.read_run(out)
    s = run["summary"]
    assert s["mode"] == "live" and s["arrived"] > 0 and s["served"] == s["arrived"] and s["failed"] == 0
    assert s["it_kwh"] > 0 and s["facility_kwh"] > s["it_kwh"] and s["cost"] > 0 and s["signal_coverage"] == 1.0
    assert s["routing"] == "nearest" and s["capacity"] == "reactive" and run["decisions"]
    assert run["requests"].num_rows == s["arrived"] and set(run["requests"].column("status").to_pylist()) == {"served"}
    assert len(run["timeseries"]) >= 3 and float(run["timeseries"][0]["sh_it_kw"]) > 0
    assert (out / "logs" / "gateway.jsonl").is_file()
