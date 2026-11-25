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
    row = run["timeseries"][0]
    assert isinstance(row["t_h"], float) and isinstance(row["time"], str)  # 数值列为 float，便于直接计算
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


def test_user_errors_are_one_line(tmp_path, capsys):
    """常见的使用错误输出一行说明并以状态 2 退出，不输出 Python 调用栈。"""
    def err(argv):
        with pytest.raises(SystemExit) as e:
            cli_main(argv)
        assert e.value.code == 2
        return capsys.readouterr().err

    s = str(TEST_SCENARIO)
    assert "是否要写 nearest" in err(["sim", "-s", s, "--routing", "neareast", "--hours", "0.1"])
    assert "是否要写 max_wait_s" in err(["sim", "-s", s, "--routing", "nearest", "--routing-params", '{"max_wait": 1}'])
    assert "不是有效的 JSON" in err(["sim", "-s", s, "--routing-params", "{bad"])
    assert "短于仿真时长" in err(["sim", "-s", s, "--hours", "1000"])
    assert "应大于 0" in err(["sim", "-s", s, "--hours", "-1"])
    bad = tmp_path / "bad.yaml"
    bad.write_text(TEST_SCENARIO.read_text(encoding="utf-8").replace("slots:", "slot:", 1), encoding="utf-8")
    out = err(["check", "-s", str(bad)])
    assert "缺少必填字段" in out and "不认识的字段（是否要写 slots？）" in out and "Traceback" not in out
    bad.write_text("name: x\n  y: [\n", encoding="utf-8")
    assert "不是有效的 YAML" in err(["check", "-s", str(bad)])


def test_policy_params_override_scenario_policy(tmp_path):
    """只给出策略参数时修改场景中策略的参数，策略不变。"""
    cpc.simulate(TEST_SCENARIO, routing_params={"max_wait_s": 2.0}, hours=0.2, rate_scale=0.05, out=tmp_path / "r")
    assert cpc.read_run(tmp_path / "r")["run"]["routing"] == {"policy": "nearest", "params": {"max_wait_s": 2.0}}


def test_measure_writes_power_profile(tmp_path, monkeypatch, capsys):
    """cpc measure：固定并发下的功率与 token 间隔写为 power_profile 数据集，cpc profile 可直接使用其中记录的测量值。"""
    import asyncio
    import json as _json

    import httpx

    from cpc import measure
    from cpc.datasets import load_descriptor
    from cpc.datasets.power import load_points

    monkeypatch.setattr(measure, "SAMPLE_S", 0.05)
    inflight = {"n": 0}

    async def sse(n_out):
        inflight["n"] += 1
        try:
            for i in range(n_out):
                await asyncio.sleep(0.01)
                yield f'data: {_json.dumps({"choices": [{"delta": {"content": "x"}}]})}\n\n'.encode()
            yield f'data: {_json.dumps({"choices": [], "usage": {"completion_tokens": n_out}})}\n\n'.encode()
            yield b"data: [DONE]\n\n"
        finally:
            inflight["n"] -= 1

    def handler(request):
        if request.url.path == "/state":
            busy = inflight["n"]
            node = {"id": "lab-0", "state": "on", "slots": 4, "busy": busy, "power_source": "sensors",
                    "it_power_w": 100.0 + 20 * busy}
            return httpx.Response(200, json={"site": "lab", "nodes_total": 1, "power_scope": "node",
                                             "components": {"sensors": {"rapl": {}}}, "nodes": [node]})
        body = _json.loads(request.content)
        return httpx.Response(200, content=sse(body["max_tokens"]), headers={"content-type": "text/event-stream"})

    meta, res = asyncio.run(measure.run_measure("http://lab", None, [0, 1, 2], 0.1, 0.6, 8, 5, "cpc", False,
                                                log=lambda *a: None, transport=httpx.MockTransport(handler)))
    assert meta["node"] == "lab-0" and meta["slots"] == 4
    assert res[0].power_w == 100.0 and res[0].itl_s is None          # 并发 0：只测功率（在线空载）
    assert res[2].power_w > res[1].power_w > 100.0 and res[1].itl_s > 0 and res[2].requests > 0

    async def fake_run(*args, **kwargs):  # 命令行：写出数据集，cpc profile 使用其中记录的测量值
        return dict(meta, boot_s=12.0, off_w=55.0), res

    monkeypatch.setattr(measure, "run_measure", fake_run)
    out = tmp_path / "lab-cpu"
    cli_main(["measure", "--site", "http://lab", "--concurrency", "0,1,2", "--device", "cpu", "--model-name", "m",
              "--out", str(out)])
    d = load_descriptor(out)
    assert d.kind == "power_profile" and d.cpc["measurement"]["idle_w"] == 100.0
    assert [p.concurrency for p in load_points(d)] == [round(r.busy_mean, 3) for r in res[1:]]
    capsys.readouterr()
    cli_main(["profile", str(out), "--name", "lab"])
    text = capsys.readouterr().out
    assert "- [0.0, 100.0]" in text and "boot_s: 12.0" in text and "在线空载功率取自测量" in text
    assert "100 W 为实测值" in text and "off_w: 55.0" in text and "关机功率取自测量" in text


def test_site_fetches_config_from_control_node(scn, monkeypatch):
    """站点代理从控制节点取得场景与服务地址（GET /v1/config，需管理令牌）。"""
    from fastapi.testclient import TestClient

    from cpc.cli import fetch_control_config
    from cpc.common import StartupError
    from cpc.config import ConfigError, default_local_endpoints
    from cpc.grid.service import create_app

    monkeypatch.setenv("CPC_ADMIN_TOKEN", "t0k")
    text = TEST_SCENARIO.read_text(encoding="utf-8")
    ep = default_local_endpoints(scn)
    with TestClient(create_app(scn, config={"scenario": text, "endpoints": ep.model_dump()})) as c:
        got_scn, got_ep = fetch_control_config("http://testserver", 5, client=c)
        assert [x.id for x in got_scn.sites] == [x.id for x in scn.sites] and got_ep.sites == ep.sites
        import cpc.common

        monkeypatch.setattr(cpc.common, "admin_headers", lambda: {"Authorization": "Bearer wrong"})  # 客户端令牌不一致
        with pytest.raises(ConfigError, match="管理令牌"):
            fetch_control_config("http://testserver", 5, client=c)
    with pytest.raises(StartupError, match="无法连接控制节点"):
        fetch_control_config("http://127.0.0.1:9", 0.1)


def test_compare_result_directories(tmp_path, capsys):
    """cpc compare：并列给出两个结果目录的总体与各站点指标及比值。"""
    cpc.simulate(TEST_SCENARIO, hours=0.2, rate_scale=0.05, out=tmp_path / "a")
    cpc.simulate(TEST_SCENARIO, routing="least_loaded", hours=0.2, rate_scale=0.05, out=tmp_path / "b")
    cli_main(["compare", str(tmp_path / "a"), str(tmp_path / "b")])
    out = capsys.readouterr().out
    assert "完成请求" in out and "IT 电量 (kWh)" in out and "比值" in out and "1.00" in out
    with pytest.raises(SystemExit):
        cli_main(["compare", str(tmp_path / "a")])


def test_measure_warns_on_flat_power():
    """各级功率几乎相同（如 BMC 读数不更新）时给出警告；功率随负载变化时没有警告。"""
    from cpc.measure import Level, flat_power_warning

    flat = [Level(0, power_w=181.0), Level(1, power_w=181.0), Level(4, power_w=181.4)]
    assert "不随负载变化" in flat_power_warning(flat)
    assert flat_power_warning([Level(0, power_w=71.4), Level(1, power_w=203.6)]) is None
