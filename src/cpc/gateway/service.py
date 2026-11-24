"""全局请求路由网关（gateway）。

用户（或负载生成器）把 OpenAI 兼容请求发到网关，网关根据路由策略选择站点并转发。
请求头 x-cpc-source 指明请求来源区域（缺省为场景中的第一个来源）。

跨数据中心的广域网时延通过"时延注入"模拟：转发前等待 RTT/2，返回（或首个流式片段）前再等待 RTT/2。
在真实多地部署时应关闭注入（network.emulate_wan=false），由真实网络产生时延。

接口：
- POST /v1/chat/completions, /v1/completions    路由并转发
- GET  /v1/models
- GET  /state                                    需求统计（DemandSnapshot）与站点视图
- PUT  /admin/policy   {"policy": "...", "params": {...}}  运行时切换路由策略
- PUT  /admin/weights  {"weights": {source: {site: w}}}    控制器下发路由权重
- GET  /metrics
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ..common import JsonlWriter, metrics_response
from ..config import Endpoints, Scenario
from ..grid.signals import GridSignal
from ..models import SimClock, rtt_matrix
from ..schemas import DemandSnapshot, SiteSnapshot
from ..sched import ClusterView, RequestInfo, SiteView, make_routing
from ..site.executors import estimate_tokens

log = logging.getLogger("cpc.gateway")

LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128)


class GatewayState:
    def __init__(self, scn: Scenario, ep: Endpoints):
        self.scn = scn
        self.ep = ep
        self.sites: dict[str, SiteSnapshot] = {}
        self.signals: dict[str, GridSignal] = {}
        self.pending: dict[str, int] = {s.id: 0 for s in scn.sites}
        self.weights: dict[str, dict[str, float]] = {}
        self.rtt = rtt_matrix(scn)
        self.clock = SimClock(start=scn.clock.start, speedup=scn.clock.speedup)
        self.clock_synced = False
        self._src_count: dict[str, int] = {s.id: 0 for s in scn.sources}
        self._site_count: dict[str, int] = {s.id: 0 for s in scn.sites}
        self.src_rate: dict[str, float] = {s.id: 0.0 for s in scn.sources}
        self.site_rate: dict[str, float] = {s.id: 0.0 for s in scn.sites}

    def view(self) -> ClusterView:
        sv = {sid: SiteView(snap=s, signal=self.signals.get(sid), pending=self.pending.get(sid, 0)) for sid, s in self.sites.items()}
        return ClusterView(now=self.clock.now(), sites=sv, rtt_ms=self.rtt, slo=self.scn.slo, weights=self.weights)


def create_app(scn: Scenario, ep: Endpoints, policy: str | None = None, params: dict | None = None,
               log_path: str | None = None, refresh_s: float = 1.0) -> FastAPI:
    st = GatewayState(scn, ep)
    pol = {"policy": make_routing(policy or scn.scheduler.routing.policy, params if policy else scn.scheduler.routing.params)}
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0), limits=httpx.Limits(max_connections=4000, max_keepalive_connections=1000))
    writer = JsonlWriter(log_path)
    default_src = scn.sources[0].id
    emulate = scn.network.emulate_wan

    reg = CollectorRegistry()
    m_req = Counter("cpc_gateway_requests", "网关请求数", ["source", "site", "status"], registry=reg)
    m_slo = Counter("cpc_gateway_slo", "SLO 达成情况", ["source", "site", "result"], registry=reg)
    m_e2e = Histogram("cpc_gateway_e2e_seconds", "端到端时延（含注入的广域网时延）", ["source", "site"], buckets=LAT_BUCKETS, registry=reg)
    m_ttft = Histogram("cpc_gateway_ttft_seconds", "首 token 时延（流式请求）", ["source", "site"], buckets=LAT_BUCKETS, registry=reg)
    m_tok = Counter("cpc_gateway_tokens", "token 数", ["source", "site", "kind"], registry=reg)
    m_energy = Counter("cpc_gateway_energy_joules", "站点估计的请求能耗（设施侧）", ["source", "site"], registry=reg)
    m_rtt = Gauge("cpc_gateway_wan_rtt_ms", "来源到站点的往返时延（ms）", ["source", "site"], registry=reg)
    m_w = Gauge("cpc_gateway_route_weight", "控制器下发的路由权重", ["source", "site"], registry=reg)
    m_rate = Gauge("cpc_gateway_source_rate", "来源区域到达率（请求/秒）", ["source"], registry=reg)
    m_policy = Gauge("cpc_gateway_policy_info", "当前路由策略", ["policy"], registry=reg)
    for s, row in st.rtt.items():
        for d, v in row.items():
            m_rtt.labels(s, d).set(v)

    async def poll_sites() -> None:
        async def one(sid: str, url: str) -> None:
            try:
                r = await client.get(url + "/state", timeout=2.0)
                st.sites[sid] = SiteSnapshot.model_validate(r.json())
                st.pending[sid] = 0
            except Exception as e:  # noqa: BLE001
                if sid in st.sites:
                    st.sites[sid].healthy = False
                log.debug("poll %s failed: %s", sid, e)

        while True:
            await asyncio.gather(*(one(sid, url) for sid, url in ep.sites.items()))
            await asyncio.sleep(refresh_s)

    async def poll_grid() -> None:
        while True:
            try:
                if not st.clock_synced:
                    r = await client.get(ep.grid + "/v1/clock", timeout=2.0)
                    st.clock = SimClock.from_dict(r.json())
                    st.clock_synced = True
                r = await client.get(ep.grid + "/v1/signals", timeout=2.0)
                st.signals = {k: GridSignal.model_validate(v) for k, v in r.json()["signals"].items()}
            except Exception as e:  # noqa: BLE001
                log.debug("poll grid failed: %s", e)
            await asyncio.sleep(2.0)

    async def rates() -> None:
        while True:
            await asyncio.sleep(1.0)
            for k in st.src_rate:
                st.src_rate[k] = 0.8 * st.src_rate[k] + 0.2 * st._src_count[k]
                st._src_count[k] = 0
                m_rate.labels(k).set(st.src_rate[k])
            for k in st.site_rate:
                st.site_rate[k] = 0.8 * st.site_rate[k] + 0.2 * st._site_count[k]
                st._site_count[k] = 0

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        writer.start()
        tasks = [asyncio.create_task(c()) for c in (poll_sites, poll_grid, rates)]
        yield
        for t in tasks:
            t.cancel()
        await writer.stop()
        await client.aclose()

    app = FastAPI(title="cpc gateway", lifespan=lifespan)
    app.state.gw = st
    m_policy.labels(pol["policy"].name).set(1)

    def finish(rec: dict, status: int, src: str, sid: str, t0: float, n_in: int, n_out: int, ttft: float | None, energy: float | None) -> None:
        e2e = time.monotonic() - t0
        m_req.labels(src, sid, str(status)).inc()
        if status == 200:
            m_e2e.labels(src, sid).observe(e2e)
            m_tok.labels(src, sid, "prompt").inc(n_in)
            m_tok.labels(src, sid, "completion").inc(n_out)
            if ttft is not None:
                m_ttft.labels(src, sid).observe(ttft)
            if energy is not None:
                m_energy.labels(src, sid).inc(energy)
            if ttft is not None:
                tpot = (e2e - ttft) / max(n_out - 1, 1)
                ok = ttft <= scn.slo.ttft_s and tpot <= scn.slo.tpot_s
            else:
                ok = e2e <= scn.slo.ttft_s + n_out * scn.slo.tpot_s
            m_slo.labels(src, sid, "met" if ok else "violated").inc()
            rec["slo_ok"] = ok
        rec.update(status=status, e2e_s=round(e2e, 4), ttft_s=None if ttft is None else round(ttft, 4), energy_j=energy)
        writer.write(rec)

    async def route(request: Request, path: str):
        body = await request.json()
        src = request.headers.get("x-cpc-source", default_src)
        if src not in st.src_rate:
            src = default_src
        st._src_count[src] += 1
        n_in, n_out = estimate_tokens(body)
        req = RequestInfo(source=src, input_tokens=n_in, output_tokens=n_out, id=uuid.uuid4().hex[:12])
        t0 = time.monotonic()
        tried: list[str] = []
        stream = bool(body.get("stream"))
        for _ in range(3):
            view = st.view()
            sid = pol["policy"].choose(req, view, exclude=tried)
            if sid is None:
                break
            st.pending[sid] = st.pending.get(sid, 0) + 1
            st._site_count[sid] += 1
            rtt_s = st.rtt[src][sid] / 1000.0 if emulate else 0.0
            sig = st.signals.get(sid)
            rec = {"id": req.id, "t_wall": time.time(), "t_signal": view.now.isoformat(), "source": src, "site": sid,
                   "policy": pol["policy"].name, "input_tokens": n_in, "output_tokens": n_out, "rtt_ms": st.rtt[src][sid],
                   "price": sig.price_yuan_per_kwh if sig else None, "carbon": sig.carbon_kg_per_kwh if sig else None,
                   "stream": stream}
            if rtt_s:
                await asyncio.sleep(rtt_s / 2)
            url = ep.sites[sid] + path
            try:
                if not stream:
                    r = await client.post(url, json=body)
                    if r.status_code == 503:
                        tried.append(sid)
                        continue
                    if rtt_s:
                        await asyncio.sleep(rtt_s / 2)
                    energy = float(r.headers["x-cpc-energy-j"]) if "x-cpc-energy-j" in r.headers else None
                    rec.update(node=r.headers.get("x-cpc-node"), queue_s=r.headers.get("x-cpc-queue-s"), attempts=len(tried) + 1)
                    finish(rec, r.status_code, src, sid, t0, n_in, n_out, None, energy)
                    hdr = {k: v for k, v in r.headers.items() if k.startswith("x-cpc-")}
                    hdr["x-cpc-source"] = src
                    return JSONResponse(r.json(), status_code=r.status_code, headers=hdr)
                r = await client.send(client.build_request("POST", url, json=body), stream=True)
                if r.status_code == 503:
                    await r.aclose()
                    tried.append(sid)
                    continue
                rec.update(node=r.headers.get("x-cpc-node"), queue_s=r.headers.get("x-cpc-queue-s"), attempts=len(tried) + 1)
                hdr = {k: v for k, v in r.headers.items() if k.startswith("x-cpc-")}

                async def gen(r=r, rec=rec, sid=sid, rtt_s=rtt_s):
                    ttft = None
                    try:
                        async for chunk in r.aiter_raw():
                            if ttft is None:
                                if rtt_s:
                                    await asyncio.sleep(rtt_s / 2)
                                ttft = time.monotonic() - t0
                            yield chunk
                    finally:
                        await r.aclose()
                        finish(rec, r.status_code, src, sid, t0, n_in, n_out, ttft, None)

                return StreamingResponse(gen(), status_code=r.status_code, media_type="text/event-stream", headers=hdr)
            except httpx.HTTPError as e:
                log.warning("forward to %s failed: %s", sid, e)
                tried.append(sid)
                continue
        m_req.labels(src, "none", "503").inc()
        writer.write({"id": req.id, "t_wall": time.time(), "source": src, "site": None, "status": 503, "tried": tried})
        return JSONResponse({"error": {"message": "no site available", "type": "overloaded", "tried": tried}}, status_code=503)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await route(request, "/v1/chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await route(request, "/v1/completions")

    @app.get("/v1/models")
    async def models():
        for url in ep.sites.values():
            try:
                return (await client.get(url + "/v1/models", timeout=3.0)).json()
            except httpx.HTTPError:
                continue
        raise HTTPException(503, "no site reachable")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/state")
    def state() -> dict:
        d = DemandSnapshot(rates=st.src_rate, site_rates=st.site_rate, policy=pol["policy"].name, weights=st.weights)
        return {**d.model_dump(), "signal_time": st.clock.now().isoformat(), "rtt_ms": st.rtt,
                "sites": {k: {"load": round(v.load, 3), "est_wait_s": round(v.est_wait_s, 3), "available": v.available}
                          for k, v in st.view().sites.items()}}

    @app.put("/admin/policy")
    def set_policy(body: dict) -> dict:
        try:
            pol["policy"] = make_routing(body["policy"], body.get("params"))
        except KeyError as e:
            raise HTTPException(400, str(e))
        m_policy.clear()
        m_policy.labels(pol["policy"].name).set(1)
        log.info("routing policy -> %s %s", body["policy"], body.get("params"))
        return {"policy": pol["policy"].name}

    @app.put("/admin/weights")
    def set_weights(body: dict) -> dict:
        st.weights = body.get("weights", {})
        m_w.clear()
        for s, row in st.weights.items():
            for d, w in row.items():
                m_w.labels(s, d).set(w)
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        return metrics_response(reg)

    return app
