"""站点代理：以真实时间驱动数据中心运行时（异步派发、撤销、执行器失败）与服务接口。"""

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
    scn.scheduler.queue_limit_per_slot = 0.25  # 4 台 × 16 槽；队列上限 = 64 × 0.25 = 16

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
        assert agent.snapshot().queue_len == 15  # 撤销的请求离开队列
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
        assert s.state == "booting"  # 真实服务器先经就绪检查
        agent.apply(CapacityTarget(active_nodes=0))
        agent.apply(CapacityTarget(active_nodes=1))
        await asyncio.sleep(0.01)
        # 开机失败：服务器回到 off，错误记入状态与失败计数，不假装成功
        assert s.state == "off" and "container not found" in s.last_error
        assert agent.dc.actuation_errors[("u0", "power_on")] == 1
        assert agent.snapshot().nodes[0].last_error.startswith("power_on")

    asyncio.run(run())


def test_site_service_api_and_readiness(scn):
    scn.node_profiles["a100x8"].decode_s_per_token = 0.001
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
        assert len(nodes) == 1  # 请求属性经请求头传给站内派发策略：同一会话落在同一服务器
        r = c.post("/v1/chat/completions", json=body)
        assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 8
        assert r.headers["x-cpc-site"] == "gz" and "x-cpc-energy-est-j" in r.headers
        # 仿真服务器要求请求给出输出上限：缺失时返回 400，而不是按缺省长度计时
        r = c.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
        with c.stream("POST", "/v1/completions", json={"prompt": "a b c", "max_tokens": 20, "stream": True}) as s:
            data = b"".join(s.iter_bytes())
        assert data.endswith(b"data: [DONE]\n\n")
        assert c.post("/admin/capacity", json={"active_nodes": 2}).json()["nodes_on"] == 2
        m = c.get("/metrics").text
        assert "DCGM_FI_DEV_POWER_USAGE{" in m and "cpc_site_facility_power_watts" in m and 'pool="main"' in m
        st = c.get("/state").json()
        assert st["power_scope"] == "node" and st["energy_coverage"] == pytest.approx(1.0) and st["pools"][0]["total"] == 6
