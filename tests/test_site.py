"""Site agent: the data-centre runtime on real time (asynchronous dispatch, cancellation, executor
failures) and the service interface."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from cpc.schemas import CapacityTarget
from cpc.sched import RequestInfo
from cpc.site.core import Overloaded, SiteAgent
from cpc.site.service import create_app


def info(**attrs):
    return RequestInfo(source="east", input_tokens=10, output_tokens=5, attrs=attrs)


def test_agent_acquire_release_and_cancel(scn):
    scn.scheduler.queue_limit_per_slot = 0.25  # 4 servers x 16 slots; queue limit 64 x 0.25 = 16

    async def run():
        agent = SiteAgent(scn, "sh")
        servers = [await agent.acquire(info()) for _ in range(64)]
        waiters = [asyncio.create_task(agent.acquire(info())) for _ in range(16)]
        await asyncio.sleep(0)
        assert agent.snapshot().queue_len == 16
        with pytest.raises(Overloaded):
            await agent.acquire(info())
        waiters[0].cancel()
        await asyncio.sleep(0)
        assert agent.snapshot().queue_len == 15  # the cancelled request leaves the queue
        agent.release(servers[0], 1.0)
        await asyncio.sleep(0)
        assert waiters[1].done() and agent.snapshot().queue_len == 14
        for w in waiters[2:]:
            w.cancel()
        await asyncio.sleep(0)
        assert agent.snapshot().queue_len == 0

    asyncio.run(run())


class FailingHooks:
    async def power_on(self, server):
        raise RuntimeError("container not found")

    async def power_off(self, server):
        return None

    async def set_cap(self, server, ratio):
        return None

    async def is_ready(self, server):
        return True


def test_actuation_failure_is_recorded(scn):
    def real(scn):
        from cpc.config import MeasurementConfig, PoolConfig, UpstreamConfig

        site = scn.site("sh")
        site.pools = [PoolConfig(id="gpu", profile="a100x8", backend="openai",
                                 upstreams=[UpstreamConfig(id="u0", url="http://127.0.0.1:9", devices=["gpu0"])])]
        site.measurement = MeasurementConfig(sensors=[], host="excluded")

    real(scn)

    async def run():
        agent = SiteAgent(scn, "sh", hooks=FailingHooks())
        s = agent.servers[0]
        assert s.state == "booting"  # a real server passes its health check first
        agent.apply(CapacityTarget(active_nodes=0))
        agent.apply(CapacityTarget(active_nodes=1))
        await asyncio.sleep(0.01)
        # failed power-on: the server returns to off, the error goes to state and counters, nothing pretends success
        assert s.state == "off" and "container not found" in s.last_error
        assert agent.dc.actuation_errors[("u0", "power_on")] == 1
        assert agent.snapshot().nodes[0].last_error.startswith("power_on")

    asyncio.run(run())


class SlowOffHooks:
    """Power-off takes a while (like docker stop); records the order of actions and whether the container runs."""

    def __init__(self):
        self.running, self.log = True, []

    async def power_on(self, server):
        self.log.append("on")
        self.running = True

    async def power_off(self, server):
        self.log.append("off-begin")
        await asyncio.sleep(0.05)
        self.running = False
        self.log.append("off-end")

    async def set_cap(self, server, ratio):
        return None

    async def is_ready(self, server):
        return self.running


def _one_real_server(scn):
    from cpc.config import MeasurementConfig, PoolConfig, UpstreamConfig

    site = scn.site("sh")
    site.pools = [PoolConfig(id="cpu", profile="a100x8", backend="openai",
                             upstreams=[UpstreamConfig(id="u0", url="http://127.0.0.1:9")])]
    site.measurement = MeasurementConfig(sensors=[], host="server")


def test_power_actions_are_serialized_per_server(scn):
    """A power-on issued while a power-off is running waits until it has finished, so the server ends up
    serving (otherwise the start could precede the stop and leave the container exited)."""
    _one_real_server(scn)

    async def run():
        hooks = SlowOffHooks()
        agent = SiteAgent(scn, "sh", hooks=hooks)
        s = agent.servers[0]
        await agent.tick()
        assert s.state == "on"
        agent.apply(CapacityTarget(active_nodes=0))
        agent.apply(CapacityTarget(active_nodes=1))
        await asyncio.sleep(0.1)
        assert hooks.log == ["off-begin", "off-end", "on"] and hooks.running
        await agent.tick()
        assert s.state == "on"

    asyncio.run(run())


def test_boot_timeout_marks_power_on_failed(scn, monkeypatch):
    """A real server not ready within the boot deadline fails to boot: back to off with the reason, instead of
    staying in booting."""
    import cpc.site.core as core

    _one_real_server(scn)
    monkeypatch.setattr(core, "boot_timeout_s", lambda boot_s: 0.05)

    async def run():
        hooks = SlowOffHooks()
        hooks.running = False
        agent = SiteAgent(scn, "sh", hooks=hooks)
        s = agent.servers[0]
        await agent.tick()
        assert s.state == "booting"
        await asyncio.sleep(0.1)
        await agent.tick()
        assert s.state == "off" and "boot timeout" in s.last_error
        assert agent.dc.actuation_errors[("u0", "power_on")] == 1

    asyncio.run(run())


def test_site_service_api_and_readiness(scn):
    scn.node_profiles["a100x8"].tpot_s = [(0, 0.001), (1, 0.002)]
    scn.site("gz").scheduler.dispatch.policy = "affinity"
    with TestClient(create_app(scn, "gz")) as c:
        assert c.get("/healthz").json()["status"] == "alive"
        t0 = time.time()
        while c.get("/readyz").status_code != 200 and time.time() - t0 < 3:
            time.sleep(0.05)
        assert c.get("/readyz").status_code == 200
        body = {"model": "x", "messages": [{"role": "user", "content": "hi " * 50}], "max_tokens": 8}
        hdr = {"x-cpc-source": "east", "x-cpc-attr-session_id": '"u7"'}
        nodes = {c.post("/v1/chat/completions", json=body, headers=hdr).headers["x-cpc-node"] for _ in range(3)}
        assert len(nodes) == 1  # request attributes reach the dispatch policy via headers: one session, one server
        r = c.post("/v1/chat/completions", json=body)
        assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 8
        assert r.headers["x-cpc-site"] == "gz" and r.headers["x-cpc-node"].startswith("gz-main-")
        # emulated servers require an output limit: 400 without one, rather than timing a default length
        r = c.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
        with c.stream("POST", "/v1/completions", json={"prompt": "a b c", "max_tokens": 20, "stream": True}) as s:
            data = b"".join(s.iter_bytes())
        assert data.endswith(b"data: [DONE]\n\n")
        assert c.post("/admin/capacity", json={"active_nodes": 2}).json()["nodes_on"] == 2
        m = c.get("/metrics").text
        assert "cpc_node_it_power_watts{" in m and "cpc_site_facility_power_watts" in m and 'pool="main"' in m
        assert "DCGM_FI_DEV_POWER_USAGE{" not in m  # accelerator metrics only for measured servers
        st = c.get("/state").json()
        assert st["power_scope"] == "node" and st["energy_coverage"] == pytest.approx(1.0) and st["pools"][0]["total"] == 6


def test_replay_prompt_length_matches_trace():
    """Replayed prompts: the input length estimated by the site agent equals the input tokens of the trace
    (as in the simulator)."""
    import random

    from cpc.live import Arrival, make_body
    from cpc.site.executors import approx_prompt_tokens

    rng = random.Random(0)
    for n in (1, 7, 256, 3000):
        body = make_body(Arrival(0.0, "east", n, 16, {}), rng, stream=False)
        assert approx_prompt_tokens(body) == n


def test_failed_upstream_is_taken_out_and_restarted(scn, monkeypatch):
    """An online real server failing consecutive health checks leaves service (no more dispatch), is powered
    on again to restart its inference instance, and is online again once healthy."""
    import cpc.site.core as core

    _one_real_server(scn)
    monkeypatch.setattr(core, "HEALTH_PROBE_S", 0.0)

    async def run():
        hooks = SlowOffHooks()
        agent = SiteAgent(scn, "sh", hooks=hooks)
        s = agent.servers[0]
        await agent.tick()
        assert s.state == "on"
        hooks.running = False  # the inference instance crashed
        hooks.log.clear()
        await agent.tick()
        assert s.state == "on"  # one failure is not enough
        await agent.tick()
        assert s.state == "booting" and "failed health checks" in s.last_error
        assert agent.dc.actuation_errors[("u0", "health")] == 1
        assert agent.snapshot().slots_on == 0  # the failed instance takes no requests (new ones queue until it recovers)
        await asyncio.sleep(0.01)
        assert hooks.log == ["on"] and hooks.running  # the executor restarted the inference instance
        await agent.tick()
        assert s.state == "on"

    asyncio.run(run())
