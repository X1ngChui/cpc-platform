"""失败显式化与故障隔离：配置错误在加载时报错；网关、控制器在依赖故障时的隔离行为、状态与指标。

网关与控制器通过 httpx.MockTransport 访问伪造的电网信号服务、站点与网关，按测试需要注入连接失败、503 与超时。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from cpc.common import StartupError
from cpc.config import ConfigError, default_local_endpoints, load_endpoints, load_scenario
from cpc.controller.service import create_app as controller_app
from cpc.gateway.service import create_app as gateway_app
from cpc.grid.service import create_app as grid_app
from cpc.datacenter import Datacenter
from cpc.datasets import DatasetError
from cpc.grid.signals import GridModel
from cpc.sched import make_dispatch, make_power

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 配置：启动期校验


def write_scenario(tmp_path: Path, mutate) -> Path:
    """复制测试场景（数据集路径改为绝对路径）并按 mutate 修改。"""
    raw = yaml.safe_load((ROOT / "tests/data/scenario.yaml").read_text(encoding="utf-8"))
    for src in (raw["grid"]["price"], raw["grid"]["carbon"], raw["workload"]):
        src["dataset"] = str(ROOT / "tests/data" / src["dataset"])
    mutate(raw)
    p = tmp_path / "scenario.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return p


def test_config_rejects_unknown_and_obsolete_fields(tmp_path):
    with pytest.raises(ConfigError, match="sensors"):
        load_scenario(write_scenario(tmp_path, lambda r: r["sites"][0].update(sensors=[{"type": "nvml"}])))
    with pytest.raises(ConfigError, match="rpss"):
        load_scenario(write_scenario(tmp_path, lambda r: r["workload"].update(rpss=3)))


def test_config_pools_and_measurement(tmp_path):
    def real_site_without_measurement(r):
        r["sites"][0]["pools"] = [{"id": "gpu", "profile": "a100x8", "backend": "openai",
                                   "upstreams": [{"id": "u0", "url": "http://x", "gpu_indices": [0]}]}]

    with pytest.raises(ConfigError, match="measurement"):
        load_scenario(write_scenario(tmp_path, real_site_without_measurement))
    with pytest.raises(ConfigError, match="measurement"):
        load_scenario(write_scenario(tmp_path, lambda r: r["sites"][0].update(measurement={"sensors": [], "host": "excluded"})))
    with pytest.raises(ConfigError, match="count"):
        load_scenario(write_scenario(tmp_path, lambda r: r["sites"][0]["pools"][0].update(count=0)))
    with pytest.raises(ConfigError, match="node profile"):
        load_scenario(write_scenario(tmp_path, lambda r: r["sites"][0]["pools"][0].update(profile="h200")))


def test_missing_dataset_is_reported(tmp_path):
    from cpc.sim.des import Simulator
    from cpc.sched import make_capacity, make_routing

    scn = load_scenario(write_scenario(tmp_path, lambda r: r["workload"].update(dataset=str(tmp_path / "nowhere"))))
    with pytest.raises(DatasetError, match="nowhere"):
        Simulator(scn, make_routing("nearest"), make_capacity("static"), hours=1)


def test_endpoints_must_be_explicit(scn, monkeypatch):
    monkeypatch.delenv("CPC_ENDPOINTS", raising=False)
    with pytest.raises(ConfigError, match="--local"):
        load_endpoints(None, scn)
    assert load_endpoints(None, scn, local=True) == default_local_endpoints(scn)
    with pytest.raises(ConfigError):
        load_endpoints("configs/endpoints.compose-host.yaml", scn, local=True)


def test_grid_data_must_cover_sites(scn):
    scn.sites[0].region = "atlantis"
    with pytest.raises(DatasetError, match="atlantis"):
        GridModel(scn)


# ------------------------------------------------------------------ 伪造的上游服务


class Upstreams:
    """按 URL 主机名分派：grid、gateway、site-<id>。faults 指定注入的故障。"""

    def __init__(self, scn):
        self.scn = scn
        self.grid = GridModel(scn)
        self.snaps = {s.id: Datacenter(s, scn.node_profiles, dispatch=make_dispatch("least_loaded"), power=make_power("ordered"))
                      .summary(0.0).model_dump() for s in scn.sites}
        self.faults: dict[str, str] = {}  # 目标 → connect / http500 / overloaded / read_timeout
        self.calls: list[str] = []
        self.capacity: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append(f"{host}{path}")
        target = "grid" if host == "grid" else "gateway" if host == "gateway" else host.removeprefix("site-")
        fault = self.faults.get(f"{target}{path}") or self.faults.get(target)
        if fault == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if fault == "read_timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if fault == "http500":
            return httpx.Response(500, text="internal error")
        if host == "grid":
            if path == "/v1/clock":
                return httpx.Response(200, json={"start": self.scn.clock.start.isoformat(), "speedup": 1.0, "epoch_wall": time.time()})
            if path == "/v1/signals":
                sig, missing = self.grid.snapshot(self.scn.clock.start)
                return httpx.Response(200, json={"signals": {k: v.model_dump(mode="json") for k, v in sig.items()}, "missing": missing})
        if host == "gateway" and path == "/state":
            return httpx.Response(200, json={"rates": {"east": 1.0, "south": 1.0, "north": 1.0}})
        sid = host.removeprefix("site-")
        if path == "/state":
            return httpx.Response(200, json=self.snaps[sid])
        if path == "/admin/capacity":
            self.capacity[sid] = json.loads(request.content)
            return httpx.Response(200, json=self.snaps[sid])
        if path.startswith("/v1/"):
            if fault == "overloaded":
                return httpx.Response(503, json={"error": {"type": "overloaded"}}, headers={"x-cpc-site": sid})
            return httpx.Response(200, json={"choices": [], "usage": {}}, headers={"x-cpc-site": sid, "x-cpc-node": f"{sid}-n00"})
        return httpx.Response(404)


def endpoints(scn):
    return load_endpoints(None, scn, local=True).model_copy(update={
        "grid": "http://grid", "gateway": "http://gateway", "controller": "http://controller",
        "sites": {s.id: f"http://site-{s.id}" for s in scn.sites}})


def metric(c: TestClient, name: str, **labels) -> float | None:
    from prometheus_client.parser import text_string_to_metric_families

    for fam in text_string_to_metric_families(c.get("/metrics").text):
        for s in fam.samples:
            if s.name == name and all(s.labels.get(k) == v for k, v in labels.items()):
                return s.value
    return None


CHAT = {"model": "cpc", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}


@pytest.fixture()
def gw(scn):
    scn.network.emulate_wan = False
    up = Upstreams(scn)

    def make(**kw):
        return TestClient(gateway_app(scn, endpoints(scn), "nearest", {}, transport=httpx.MockTransport(up.handler), **kw))

    return scn, up, make


# ------------------------------------------------------------------ 网关


def test_gateway_isolates_unreachable_site(gw):
    scn, up, make = gw
    up.faults["sh"] = "connect"
    with make() as c:
        r = c.post("/v1/chat/completions", json=CHAT, headers={"x-cpc-source": "east"})
        # 上海站点不可达：移出路由视图，east 的请求由次近站点服务
        assert r.status_code == 200 and r.headers["x-cpc-site"] != "sh"
        assert metric(c, "cpc_upstream_up", component="gateway", upstream="site:sh") == 0
        assert metric(c, "cpc_upstream_up", component="gateway", upstream="site:gd") == 1
        st = c.get("/state").json()
        assert "sh" not in st["sites"] and st["upstreams"]["site:sh"]["up"] is False
        assert c.get("/readyz").status_code == 200


def test_gateway_retries_configured_causes_only(gw):
    scn, up, make = gw
    up.faults["sh/v1/chat/completions"] = "overloaded"
    with make() as c:
        r = c.post("/v1/chat/completions", json=CHAT, headers={"x-cpc-source": "east"})
        assert r.status_code == 200 and r.headers["x-cpc-site"] != "sh"
        assert metric(c, "cpc_gateway_attempts_total", site="sh", outcome="overloaded") == 1
    scn.scheduler.retry.retry_on = []  # 不重试：站点 503 原样以 overloaded 返回
    with make() as c:
        r = c.post("/v1/chat/completions", json=CHAT, headers={"x-cpc-source": "east"})
        assert r.status_code == 503 and r.json()["error"]["type"] == "overloaded"


def test_gateway_does_not_retry_after_request_sent(gw):
    scn, up, make = gw
    up.faults["sh/v1/chat/completions"] = "read_timeout"
    with make() as c:
        r = c.post("/v1/chat/completions", json=CHAT, headers={"x-cpc-source": "east"})
        assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_error"
        assert len(r.json()["error"]["attempts"]) == 1


def test_gateway_signals_missing_is_explicit(gw):
    scn, up, make = gw
    up.faults["grid/v1/signals"] = "http500"
    with TestClient(gateway_app(scn, endpoints(scn), "cost_aware", {}, transport=httpx.MockTransport(up.handler))) as c:
        r = c.post("/v1/chat/completions", json=CHAT, headers={"x-cpc-source": "east"})
        assert r.status_code == 200 and r.headers["x-cpc-route-reason"] == "overflow_no_signal"
        assert metric(c, "cpc_gateway_route_decisions_total", policy="cost_aware", reason="overflow_no_signal") == 1
        assert metric(c, "cpc_upstream_up", component="gateway", upstream="grid") == 0


def test_gateway_source_must_be_known(gw):
    scn, up, make = gw
    scn.network.default_source = None
    with make() as c:
        assert c.post("/v1/chat/completions", json=CHAT).status_code == 400
        assert c.post("/v1/chat/completions", json=CHAT, headers={"x-cpc-source": "mars"}).status_code == 400


def test_gateway_fails_to_start_without_grid(gw):
    scn, up, make = gw
    up.faults["grid"] = "connect"
    with pytest.raises(StartupError, match="不可达"):
        with make(startup_timeout_s=0.3):
            pass


# ------------------------------------------------------------------ 控制器


def ctl(scn, up, policy):
    return TestClient(controller_app(scn, endpoints(scn), policy, {}, transport=httpx.MockTransport(up.handler)))


def test_controller_excludes_unreachable_site(scn):
    up = Upstreams(scn)
    up.faults["nm"] = "connect"
    with ctl(scn, up, "reactive") as c:
        rec = c.post("/admin/step").json()
        assert rec["result"] == "applied" and "nm" in rec["excluded"]
        assert set(rec["targets"]) == {"sh", "gd", "gz"} and "nm" not in up.capacity
        assert metric(c, "cpc_controller_collect_errors_total", target="site:nm") == 1
        assert metric(c, "cpc_upstream_up", component="controller", upstream="site:nm") == 0


@pytest.mark.parametrize("policy,fault,reason", [
    ("reactive", "grid", "missing_signals"),
    ("grid_aware", "gateway", "missing_demand"),
    ("static", "all_sites", "no_site_available"),
])
def test_controller_skips_cycle_on_missing_input(scn, policy, fault, reason):
    up = Upstreams(scn)
    with ctl(scn, up, policy) as c:
        if fault == "all_sites":
            up.faults.update({s.id: "connect" for s in scn.sites})
        else:
            up.faults[fault] = "http500"
        rec = c.post("/admin/step").json()
        assert rec["result"] == "skipped" and rec["reason"] == reason
        assert not up.capacity  # 输入不完整时不下发任何指令
        assert metric(c, "cpc_controller_cycles_total", result="skipped", reason=reason) == 1
        assert c.get("/state").json()["last"]["reason"] == reason


def test_controller_static_needs_no_signals(scn):
    up = Upstreams(scn)
    with ctl(scn, up, "static") as c:
        up.faults["grid"] = "http500"
        up.faults["gateway"] = "http500"
        rec = c.post("/admin/step").json()
        assert rec["result"] == "applied" and set(up.capacity) == {s.id for s in scn.sites}


# ------------------------------------------------------------------ 电网信号服务


def test_grid_service_health_endpoints(scn):
    with TestClient(grid_app(scn)) as c:
        assert c.get("/healthz").json()["status"] == "alive"
        assert c.get("/readyz").json()["ready"] is True


# ------------------------------------------------------------------ 真实后端站点：传感器运行期故障


from cpc.sensors import Reading, Sensor, register_sensor  # noqa: E402
from cpc.site.service import create_app as site_app  # noqa: E402


@register_sensor("test-meter")
class FakeMeter(Sensor):
    """测试用传感器：类属性 broken 为 True 时读取失败。"""

    broken = False

    def read(self):
        if FakeMeter.broken:
            raise TimeoutError("meter timeout")
        return [Reading(self.id, "gpu0", "gpu", "nvidia", "T", power_w=100.0)]


def test_real_site_reports_sensor_failure(tmp_path):
    def real(r):
        r["sites"][0].update(pools=[{"id": "gpu", "profile": "a100x8", "backend": "openai",
                                     "upstreams": [{"id": "u0", "url": "http://127.0.0.1:9", "devices": ["gpu0"]}]}],
                             measurement={"sensors": [{"type": "test-meter"}], "host": "excluded"})

    scn = load_scenario(write_scenario(tmp_path, real))
    FakeMeter.broken = False
    with TestClient(site_app(scn, "sh", tick_s=0.05)) as c:
        st = c.get("/state").json()
        assert st["power_scope"] == "accelerators" and st["nodes"][0]["it_power_w"] == 100.0
        FakeMeter.broken = True
        time.sleep(0.3)
        st = c.get("/state").json()
        # 传感器故障：功率标记为缺失（None），不以模型值或 0 替代；站点仍可响应，故障在状态与指标中可见
        assert st["it_power_w"] is None and st["nodes"][0]["power_missing"] == ["device:gpu0"]
        assert st["components"]["sensors"]["test-meter"]["up"] is False
        assert metric(c, "cpc_sensor_up", site="sh", sensor="test-meter") == 0
        assert metric(c, "cpc_sensor_errors_total", site="sh", sensor="test-meter") >= 1
        assert c.get("/readyz").status_code == 200
        FakeMeter.broken = False
        time.sleep(0.3)
        st = c.get("/state").json()
        assert st["it_power_w"] == 100.0 and 0 < st["energy_coverage"] < 1


def test_real_site_fails_to_start_when_sensor_unavailable(tmp_path):
    from cpc.sensors import SensorError

    def real(r):
        r["sites"][0].update(pools=[{"id": "gpu", "profile": "a100x8", "backend": "openai",
                                     "upstreams": [{"id": "u0", "url": "http://127.0.0.1:9", "devices": ["gpu7"]}]}],
                             measurement={"sensors": [{"type": "test-meter"}], "host": "excluded"})

    FakeMeter.broken = False
    with pytest.raises(SensorError, match="gpu7"):
        site_app(load_scenario(write_scenario(tmp_path, real)), "sh")
