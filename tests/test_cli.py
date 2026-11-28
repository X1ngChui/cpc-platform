"""Command line and Python interface: scenario check, simulation and results, live experiments (cpc live)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import cpc
from cpc.cli import main as cli_main

from conftest import TEST_SCENARIO


def test_check_prints_overview(capsys):
    cli_main(["check", "-s", str(TEST_SCENARIO), "--step", "6"])
    out = capsys.readouterr().out
    assert "Scenario check passed" in out and "a100x8" in out and "Round-trip times" in out and "workload-synthetic@1" in out
    assert "1.105/peak/" in out and "/sharp/" in out  # 12:30: Guangdong peak (0.65 x 1.7), Shanghai sharp peak
    cli_main(["check", "-s", str(TEST_SCENARIO), "--date", "2026-09-01"])
    assert "missing" in capsys.readouterr().out  # times outside the grid data are shown as missing


def test_simulate_api_and_read_run(tmp_path):
    res = cpc.simulate(TEST_SCENARIO, routing="least_loaded", capacity="reactive", hours=1, rate_scale=0.1, out=tmp_path / "r")
    s = res.summary
    assert s["routing"] == "least_loaded" and s["served"] > 0 and s["arrived"] == len(res.requests["arrival_s"])
    run = cpc.read_run(tmp_path / "r")
    assert run["summary"]["served"] == s["served"] and run["requests"].num_rows == s["arrived"]
    row = run["timeseries"][0]
    assert isinstance(row["t_h"], float) and isinstance(row["time"], str)  # numeric columns are floats
    assert run["run"]["mode"] == "simulation" and run["timeseries"]
    with pytest.raises(TypeError, match="rate"):
        cpc.simulate(TEST_SCENARIO, hours=1, rates=1)


def test_live_experiment_writes_result_package(tmp_path, capsys):
    out = tmp_path / "live"
    cli_main(["live", "-s", str(TEST_SCENARIO), "--duration", "8", "--rate-scale", "0.1", "--sample-s", "2",
              "--speedup", "60", "--interval", "3", "--out", str(out)])
    assert "Experiment finished" in capsys.readouterr().out
    run = cpc.read_run(out)
    s = run["summary"]
    assert s["mode"] == "live" and s["arrived"] > 0 and s["served"] == s["arrived"] and s["failed"] == 0
    assert s["it_kwh"] > 0 and s["facility_kwh"] > s["it_kwh"] and s["cost"] > 0 and s["signal_coverage"] == 1.0
    assert s["routing"] == "nearest" and s["capacity"] == "reactive" and run["decisions"]
    assert run["requests"].num_rows == s["arrived"] and set(run["requests"].column("status").to_pylist()) == {"served"}
    assert len(run["timeseries"]) >= 3 and float(run["timeseries"][0]["sh_it_kw"]) > 0
    assert (out / "logs" / "gateway.jsonl").is_file()


def test_user_errors_are_one_line(tmp_path, capsys):
    """Common usage errors print one line and exit with status 2, without a Python traceback."""
    def err(argv):
        with pytest.raises(SystemExit) as e:
            cli_main(argv)
        assert e.value.code == 2
        return capsys.readouterr().err

    s = str(TEST_SCENARIO)
    assert "did you mean 'nearest'" in err(["sim", "-s", s, "--routing", "neareast", "--hours", "0.1"])
    assert "did you mean 'max_wait_s'" in err(["sim", "-s", s, "--routing", "nearest", "--routing-params", '{"max_wait": 1}'])
    assert "is not valid JSON" in err(["sim", "-s", s, "--routing-params", "{bad"])
    assert "less than the simulated" in err(["sim", "-s", s, "--hours", "1000"])
    assert "should be greater than 0" in err(["sim", "-s", s, "--hours", "-1"])
    bad = tmp_path / "bad.yaml"
    bad.write_text(TEST_SCENARIO.read_text(encoding="utf-8").replace("slots:", "slot:", 1), encoding="utf-8")
    out = err(["check", "-s", str(bad)])
    assert "required field is missing" in out and "unknown field (did you mean 'slots'?)" in out and "Traceback" not in out
    bad.write_text("name: x\n  y: [\n", encoding="utf-8")
    assert "is not valid YAML" in err(["check", "-s", str(bad)])
    live = ["live", "-s", s, "--duration", "1"]
    assert "give -e or --local, not both" in err([*live, "--local", "-e", str(tmp_path / "endpoints.yaml")])
    assert "started with" in err([*live, "--local", "--routing", "least_loaded"])  # running services keep their policies


def test_policy_params_override_scenario_policy(tmp_path):
    """Policy parameters alone update the parameters of the scenario's policy; the policy stays."""
    cpc.simulate(TEST_SCENARIO, routing_params={"max_wait_s": 2.0}, hours=0.2, rate_scale=0.05, out=tmp_path / "r")
    assert cpc.read_run(tmp_path / "r")["run"]["routing"] == {"policy": "nearest", "params": {"max_wait_s": 2.0}}


def test_measure_writes_power_profile(tmp_path, monkeypatch, capsys):
    """cpc measure writes power and inter-token latency at fixed concurrency as a power_profile dataset;
    cpc profile uses the measured values recorded in it."""
    from cpc import measure
    from cpc.datasets import load_descriptor
    from cpc.datasets.power import load_points

    monkeypatch.setattr(measure, "SAMPLE_S", 0.05)
    inflight = {"n": 0}

    async def sse(n_out):
        inflight["n"] += 1
        try:
            for _ in range(n_out):
                await asyncio.sleep(0.01)
                yield f'data: {json.dumps({"choices": [{"delta": {"content": "x"}}]})}\n\n'.encode()
            yield f'data: {json.dumps({"choices": [], "usage": {"completion_tokens": n_out}})}\n\n'.encode()
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
        body = json.loads(request.content)
        return httpx.Response(200, content=sse(body["max_tokens"]), headers={"content-type": "text/event-stream"})

    meta, res = asyncio.run(measure.run_measure("http://lab", None, [0, 1, 2], 0.1, 0.6, 8, 5, "cpc", False,
                                                log=lambda *a: None, transport=httpx.MockTransport(handler)))
    assert meta["node"] == "lab-0" and meta["slots"] == 4
    assert res[0].power_w == 100.0 and res[0].itl_s is None  # concurrency 0: power only (online idle)
    assert res[2].power_w > res[1].power_w > 100.0 and res[1].itl_s > 0 and res[2].requests > 0

    async def fake_run(*args, **kwargs):  # command line: write the dataset; cpc profile uses its measured values
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
    assert "- [0.0, 100.0]" in text and "boot_s: 12.0" in text and "online idle power from the measurement" in text
    assert "idle power 100 W measured" in text and "off_w: 55.0" in text and "power while off from the measurement" in text


def test_measure_needs_the_only_server_on(monkeypatch):
    """The site dispatches the measurement requests over all servers that are on: the measured server must be
    the only one; the boot measurement switches all servers on and then restores the number that was on."""
    from cpc import measure
    from cpc.config import ConfigError

    monkeypatch.setattr(measure, "SAMPLE_S", 0.01)
    monkeypatch.setattr(measure, "OFF_WINDOW_S", 0.05)
    states, posts = {"a": "on", "b": "on"}, []

    def handler(request):
        if request.url.path == "/admin/capacity":
            n = json.loads(request.content)["active_nodes"]
            posts.append(n)
            states.update(a="on" if n >= 2 else "off", b="on" if n >= 1 else "off")
            return httpx.Response(200, json={})
        nodes = [{"id": i, "state": st, "slots": 4, "busy": 0, "power_source": "sensors", "it_power_w": 50.0}
                 for i, st in states.items()]
        return httpx.Response(200, json={"site": "lab", "nodes_total": 2, "nodes": nodes,
                                         "nodes_on": sum(st == "on" for st in states.values())})

    async def run(fn, *args):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await fn(c, "http://lab", *args)

    for node in (None, "a"):
        with pytest.raises(ConfigError, match="must be the only one"):
            asyncio.run(run(measure.pick_node, node))
    with pytest.raises(ConfigError, match="no real server c"):
        asyncio.run(run(measure.pick_node, "c"))
    states["a"] = "off"
    assert asyncio.run(run(measure.pick_node, None))[1]["id"] == "b"
    with pytest.raises(ConfigError, match="must be the only one"):
        asyncio.run(run(measure.pick_node, "a"))
    boot_s, off_w = asyncio.run(run(measure.measure_boot, "b", 2, 1))
    assert posts == [0, 2, 1] and off_w == 50.0 and states == {"a": "off", "b": "on"}


def test_site_fetches_config_from_control_node(scn, monkeypatch):
    """A site agent fetches scenario and addresses from the control node (GET /v1/config, admin token)."""
    from fastapi.testclient import TestClient

    import cpc.common
    from cpc.cli import fetch_control_config
    from cpc.common import StartupError
    from cpc.config import ConfigError, default_local_endpoints
    from cpc.grid.service import create_app

    monkeypatch.setenv("CPC_ADMIN_TOKEN", "t0k")
    ep = default_local_endpoints(scn)
    with TestClient(create_app(scn, config={"scenario": TEST_SCENARIO.read_text(encoding="utf-8"),
                                            "endpoints": ep.model_dump()})) as c:
        got_scn, got_ep = fetch_control_config("http://testserver", 5, client=c)
        assert [x.id for x in got_scn.sites] == [x.id for x in scn.sites] and got_ep.sites == ep.sites
        monkeypatch.setattr(cpc.common, "admin_headers", lambda: {"Authorization": "Bearer wrong"})  # client token differs
        with pytest.raises(ConfigError, match="CPC_ADMIN_TOKEN differs"):
            fetch_control_config("http://testserver", 5, client=c)
    with pytest.raises(StartupError, match="cannot connect to the control node"):
        fetch_control_config("http://127.0.0.1:9", 0.1)


def test_compare_result_directories(tmp_path, capsys):
    """cpc compare shows totals, per-site metrics and the ratio of two result directories."""
    cpc.simulate(TEST_SCENARIO, hours=0.2, rate_scale=0.05, out=tmp_path / "a")
    cpc.simulate(TEST_SCENARIO, routing="least_loaded", hours=0.2, rate_scale=0.05, out=tmp_path / "b")
    cli_main(["compare", str(tmp_path / "a"), str(tmp_path / "b")])
    out = capsys.readouterr().out
    assert "served" in out and "IT energy (kWh)" in out and "ratio" in out and "1.00" in out
    with pytest.raises(SystemExit):
        cli_main(["compare", str(tmp_path / "a")])


def test_measure_warns_on_flat_power():
    """Almost equal power at all levels (e.g. a BMC reading that is not updated) gives a warning; power
    that follows the load does not."""
    from cpc.measure import Level, flat_power_warning

    flat = [Level(0, power_w=181.0), Level(1, power_w=181.0), Level(4, power_w=181.4)]
    assert "may not follow the load" in flat_power_warning(flat)
    assert flat_power_warning([Level(0, power_w=71.4), Level(1, power_w=203.6)]) is None
