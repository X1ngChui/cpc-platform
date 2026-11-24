"""全局请求路由网关（gateway）。

用户（或负载生成器）把 OpenAI 兼容请求发到网关，网关根据路由策略选择站点并转发。
请求头 x-cpc-source 指明请求来源区域；未携带时归属场景的 network.default_source，未配置该项则返回 400。

跨数据中心的广域网时延通过"时延注入"模拟：转发前等待 RTT/2，返回（或首个流式片段）前再等待 RTT/2。
在真实多地部署时应关闭注入（network.emulate_wan=false），由真实网络产生时延。

失败处理：
- 启动：须在期限内从电网信号服务同步信号时钟，否则启动失败；
- 站点状态与电网信号各自维护健康状态（cpc_upstream_up{component="gateway",upstream}）；拉取失败或超过
  scheduler.state_stale_s 未成功刷新的站点移出路由视图，信号缺失时视图中该站点的 signal 为 None；
- 路由策略返回带原因的决策（cpc_gateway_route_decisions_total{policy,reason}）；
- 转发失败按 scheduler.retry 处理：可重试类型（overloaded、connect_error）排除该站点后重新决策，至多 max_attempts 次；
  请求送达后的失败不重试，返回 502。每次尝试的结果计入 cpc_gateway_attempts_total{site,outcome}。

接口：
- POST /v1/chat/completions, /v1/completions    路由并转发
- GET  /v1/models
- GET  /state                                    需求统计（DemandSnapshot）、站点视图与上游健康状态
- GET  /healthz、/readyz                         存活检查；就绪检查（至少一个站点状态可用）
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
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from pydantic import BaseModel, Field, ValidationError

from ..common import JsonlWriter, metrics_response, require_admin, sync_clock
from ..config import Endpoints, Scenario
from ..grid.signals import GridSignal
from ..health import HealthBoard, describe
from ..models import SimClock, rtt_matrix
from ..schemas import DemandSnapshot, SiteSnapshot
from ..sched import ClusterView, RequestInfo, RouteDecision, SiteView, make_routing
from ..site.executors import approx_prompt_tokens, requested_output_tokens

log = logging.getLogger("cpc.gateway")

CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class PolicyBody(BaseModel):
    policy: str
    params: dict = Field(default_factory=dict)


class WeightsBody(BaseModel):
    """路由权重：{来源: {站点: 非负有限权重}}。"""

    weights: dict[str, dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)]]]


LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128)


class GatewayState:
    def __init__(self, scn: Scenario, ep: Endpoints, board: HealthBoard):
        self.scn = scn
        self.ep = ep
        self.board = board
        self.sites: dict[str, SiteSnapshot] = {}
        self.signals: dict[str, GridSignal] = {}
        self.pending: dict[str, int] = {s.id: 0 for s in scn.sites}
        self.weights: dict[str, dict[str, float]] = {}
        self.rtt = rtt_matrix(scn)
        self.clock: SimClock | None = None
        self._src_count: dict[str, int] = {s.id: 0 for s in scn.sources}
        self._site_count: dict[str, int] = {s.id: 0 for s in scn.sites}
        self.src_rate: dict[str, float] = {s.id: 0.0 for s in scn.sources}
        self.site_rate: dict[str, float] = {s.id: 0.0 for s in scn.sites}

    def site_usable(self, sid: str) -> bool:
        key = f"site:{sid}"
        return sid in self.sites and self.board.is_up(key) and not self.board.stale(key, self.scn.scheduler.state_stale_s)

    def signals_usable(self) -> bool:
        return self.board.is_up("grid") and not self.board.stale("grid", self.scn.scheduler.state_stale_s)

    def view(self) -> ClusterView:
        assert self.clock is not None
        sig_ok = self.signals_usable()
        sv = {sid: SiteView(snap=s, signal=self.signals.get(sid) if sig_ok else None, pending=self.pending.get(sid, 0))
              for sid, s in self.sites.items() if self.site_usable(sid)}
        return ClusterView(now=self.clock.now(), sites=sv, rtt_ms=self.rtt, slo=self.scn.slo, weights=self.weights)


def create_app(scn: Scenario, ep: Endpoints, policy: str | None = None, params: dict | None = None,
               log_path: str | None = None, startup_timeout_s: float = 30.0,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """transport 用于测试中注入故障（httpx.MockTransport）；缺省为真实网络。"""
    reg = CollectorRegistry()
    board = HealthBoard("upstream", reg, {"component": "gateway"})
    board.register("grid")
    for sid in ep.sites:
        board.register(f"site:{sid}")
    st = GatewayState(scn, ep, board)
    pol = {"policy": make_routing(policy or scn.scheduler.routing.policy, params if policy else scn.scheduler.routing.params)}
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0), limits=httpx.Limits(max_connections=4000, max_keepalive_connections=1000),
                               trust_env=False, transport=transport)  # 内部通信不走系统代理
    writer = JsonlWriter(log_path)
    emulate = scn.network.emulate_wan
    retry = scn.scheduler.retry
    refresh_s = scn.scheduler.state_refresh_s

    m_req = Counter("cpc_gateway_requests", "网关请求数", ["source", "site", "status"], registry=reg)
    m_att = Counter("cpc_gateway_attempts", "转发尝试（outcome：ok / overloaded / connect_error / upstream_error）",
                    ["site", "outcome"], registry=reg)
    m_dec = Counter("cpc_gateway_route_decisions", "路由决策（reason 为策略给出的原因）", ["policy", "reason"], registry=reg)
    m_slo = Counter("cpc_gateway_slo", "SLO 达成情况", ["source", "site", "result"], registry=reg)
    m_e2e = Histogram("cpc_gateway_e2e_seconds", "端到端时延（含注入的广域网时延）", ["source", "site"], buckets=LAT_BUCKETS, registry=reg)
    m_ttft = Histogram("cpc_gateway_ttft_seconds", "首 token 时延（流式请求）", ["source", "site"], buckets=LAT_BUCKETS, registry=reg)
    m_tok = Counter("cpc_gateway_tokens", "token 数", ["source", "site", "kind"], registry=reg)
    m_energy = Counter("cpc_gateway_energy_est_joules", "站点给出的请求能耗估计（设施侧）", ["source", "site"], registry=reg)
    m_rtt = Gauge("cpc_gateway_wan_rtt_ms", "来源到站点的往返时延（ms）", ["source", "site"], registry=reg)
    m_w = Gauge("cpc_gateway_route_weight", "控制器下发的路由权重", ["source", "site"], registry=reg)
    m_rate = Gauge("cpc_gateway_source_rate", "来源区域到达率（请求/秒）", ["source"], registry=reg)
    m_policy = Gauge("cpc_gateway_policy_info", "当前路由策略", ["policy"], registry=reg)
    for s, row in st.rtt.items():
        for d, v in row.items():
            m_rtt.labels(s, d).set(v)

    async def poll_site(sid: str, url: str) -> None:
        try:
            r = await client.get(url + "/state", timeout=2.0)
            r.raise_for_status()
            st.sites[sid] = SiteSnapshot.model_validate(r.json())
            st.pending[sid] = 0
            board.ok(f"site:{sid}")
        except (httpx.HTTPError, ValueError) as e:  # ValueError 含响应体校验失败（pydantic ValidationError）
            board.fail(f"site:{sid}", e)

    async def poll_signals() -> None:
        try:
            r = await client.get(ep.grid + "/v1/signals", timeout=2.0)
            r.raise_for_status()
            st.signals = {k: GridSignal.model_validate(v) for k, v in r.json()["signals"].items()}
            board.ok("grid")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            board.fail("grid", e)

    async def poll_sites_loop() -> None:
        while True:
            await asyncio.sleep(refresh_s)
            await asyncio.gather(*(poll_site(sid, url) for sid, url in ep.sites.items()))

    async def poll_grid_loop() -> None:
        while True:
            await asyncio.sleep(refresh_s)
            await poll_signals()

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
        st.clock = await sync_clock(client, ep.grid, startup_timeout_s)  # 不可达则启动失败
        board.ok("grid")
        await asyncio.gather(poll_signals(), *(poll_site(sid, url) for sid, url in ep.sites.items()))
        writer.start()
        tasks = [asyncio.create_task(c()) for c in (poll_sites_loop, poll_grid_loop, rates)]
        yield
        for t in tasks:
            t.cancel()
        await writer.stop()
        await client.aclose()

    app = FastAPI(title="cpc gateway", lifespan=lifespan)
    app.state.gw = st
    m_policy.labels(pol["policy"].name).set(1)

    def finish(rec: dict, status: int, src: str, sid: str, t0: float, n_in: int, n_out: int | None, ttft: float | None,
               energy: float | None) -> None:
        e2e = time.monotonic() - t0
        m_req.labels(src, sid, str(status)).inc()
        if status == 200:
            m_e2e.labels(src, sid).observe(e2e)
            m_tok.labels(src, sid, "prompt").inc(n_in)
            if n_out is not None:
                m_tok.labels(src, sid, "completion").inc(n_out)
            if ttft is not None:
                m_ttft.labels(src, sid).observe(ttft)
            if energy is not None:
                m_energy.labels(src, sid).inc(energy)
            if n_out is not None:
                if ttft is not None:
                    tpot = (e2e - ttft) / max(n_out - 1, 1)
                    ok = ttft <= scn.slo.ttft_s and tpot <= scn.slo.tpot_s
                else:
                    ok = e2e <= scn.slo.ttft_s + n_out * scn.slo.tpot_s
                m_slo.labels(src, sid, "met" if ok else "violated").inc()
                rec["slo_ok"] = ok
        rec.update(status=status, e2e_s=round(e2e, 4), ttft_s=None if ttft is None else round(ttft, 4), energy_est_j=energy)
        writer.write(rec)

    def reject(rec: dict, src: str, status: int, body: dict) -> JSONResponse:
        m_req.labels(src, "none", str(status)).inc()
        rec.update(site=None, status=status, error=body["error"]["type"])
        writer.write(rec)
        return JSONResponse(body, status_code=status)

    async def route(request: Request, path: str):
        body = await request.json()
        src = request.headers.get("x-cpc-source") or scn.network.default_source
        if src is None:
            raise HTTPException(400, "请求缺少 x-cpc-source 请求头，且场景未配置 network.default_source")
        if src not in st.src_rate:
            raise HTTPException(400, f"未知的来源区域 {src}，可选：{sorted(st.src_rate)}")
        st._src_count[src] += 1
        n_in, n_out = approx_prompt_tokens(body), requested_output_tokens(body)
        req = RequestInfo(source=src, input_tokens=n_in, output_tokens=n_out, id=uuid.uuid4().hex[:12])
        t0 = time.monotonic()
        tried: list[str] = []
        attempts: list[dict] = []
        stream = bool(body.get("stream"))
        policy_name = pol["policy"].name
        base = {"id": req.id, "t_wall": time.time(), "source": src, "policy": policy_name, "input_tokens_approx": n_in,
                "output_tokens": n_out, "stream": stream}
        decision: RouteDecision | None = None
        for _ in range(retry.max_attempts):
            view = st.view()
            decision = pol["policy"].choose(req, view, exclude=tried)
            if not isinstance(decision, RouteDecision):
                raise TypeError(f"路由策略 {policy_name} 的 choose() 须返回 RouteDecision，实际为 {type(decision).__name__}")
            m_dec.labels(policy_name, decision.reason).inc()
            if decision.site is None:
                break
            sid = decision.site
            st.pending[sid] = st.pending.get(sid, 0) + 1
            st._site_count[sid] += 1
            rtt_s = st.rtt[src][sid] / 1000.0 if emulate else 0.0
            sig = view.sites[sid].signal
            rec = {**base, "t_signal": view.now.isoformat(), "site": sid, "route_reason": decision.reason,
                   "rtt_ms": st.rtt[src][sid], "price": sig.price_yuan_per_kwh if sig else None,
                   "carbon": sig.carbon_kg_per_kwh if sig else None}
            if rtt_s:
                await asyncio.sleep(rtt_s / 2)
            url = ep.sites[sid] + path
            try:
                if not stream:
                    r = await client.post(url, json=body)
                else:
                    r = await client.send(client.build_request("POST", url, json=body), stream=True)
            except CONNECT_ERRORS as e:
                m_att.labels(sid, "connect_error").inc()
                board.fail(f"site:{sid}", e)
                attempts.append({"site": sid, "outcome": "connect_error", "error": describe(e)})
                if "connect_error" in retry.retry_on:
                    tried.append(sid)
                    continue
                return reject({**rec, "attempts": attempts}, src, 502,
                              {"error": {"message": f"connect to site {sid} failed", "type": "upstream_error", "attempts": attempts}})
            except httpx.HTTPError as e:
                m_att.labels(sid, "upstream_error").inc()
                attempts.append({"site": sid, "outcome": "upstream_error", "error": describe(e)})
                return reject({**rec, "attempts": attempts}, src, 502,
                              {"error": {"message": f"site {sid} failed after request was sent", "type": "upstream_error",
                                         "attempts": attempts}})
            if r.status_code == 503 and r.headers.get("x-cpc-site") == sid:
                if stream:
                    await r.aclose()
                m_att.labels(sid, "overloaded").inc()
                attempts.append({"site": sid, "outcome": "overloaded"})
                if "overloaded" in retry.retry_on:
                    tried.append(sid)
                    continue
                return reject({**rec, "attempts": attempts}, src, 503,
                              {"error": {"message": f"site {sid} overloaded", "type": "overloaded", "attempts": attempts}})
            m_att.labels(sid, "ok").inc()
            attempts.append({"site": sid, "outcome": "ok", "status": r.status_code})
            rec.update(node=r.headers.get("x-cpc-node"), queue_s=r.headers.get("x-cpc-queue-s"), attempts=attempts)
            hdr = {k: v for k, v in r.headers.items() if k.startswith("x-cpc-")}
            hdr["x-cpc-source"] = src
            hdr["x-cpc-route-reason"] = decision.reason
            if not stream:
                if rtt_s:
                    await asyncio.sleep(rtt_s / 2)
                energy = float(r.headers["x-cpc-energy-est-j"]) if "x-cpc-energy-est-j" in r.headers else None
                finish(rec, r.status_code, src, sid, t0, n_in, n_out, None, energy)
                try:
                    content = r.json()
                except ValueError:
                    content = {"error": {"message": r.text[:500], "type": "upstream_error"}}
                return JSONResponse(content, status_code=r.status_code, headers=hdr)

            async def gen(r=r, rec=rec, sid=sid, rtt_s=rtt_s):
                ttft = None
                try:
                    async for chunk in r.aiter_raw():
                        if ttft is None:
                            if rtt_s:
                                await asyncio.sleep(rtt_s / 2)
                            ttft = time.monotonic() - t0
                        yield chunk
                except httpx.HTTPError as e:
                    rec["stream_error"] = describe(e)
                    log.warning("流式响应中断（站点 %s）：%s", sid, describe(e),
                                extra={"event": "stream_interrupted", "site": sid, "request": req.id})
                finally:
                    await r.aclose()
                    finish(rec, r.status_code if "stream_error" not in rec else 502, src, sid, t0, n_in, n_out, ttft, None)

            return StreamingResponse(gen(), status_code=r.status_code, media_type="text/event-stream", headers=hdr)
        rec = {**base, "attempts": attempts, "route_reason": decision.reason if decision else None}
        if decision is not None and decision.site is None:
            return reject(rec, src, 503, {"error": {"message": f"route rejected: {decision.reason}", "type": "route_rejected",
                                                    "reason": decision.reason, "attempts": attempts}})
        return reject(rec, src, 503, {"error": {"message": f"retries exhausted after {len(attempts)} attempts",
                                                "type": "retries_exhausted", "attempts": attempts}})

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await route(request, "/v1/chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await route(request, "/v1/completions")

    @app.get("/v1/models")
    async def models():
        usable = sorted(sid for sid in ep.sites if st.site_usable(sid))
        if not usable:
            raise HTTPException(503, "no site available")
        try:
            r = await client.get(ep.sites[usable[0]] + "/v1/models", timeout=3.0)
            return JSONResponse(r.json(), status_code=r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"site {usable[0]} error: {describe(e)}")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        usable = sorted(sid for sid in ep.sites if st.site_usable(sid))
        body = {"ready": bool(usable), "usable_sites": usable, "signals": st.signals_usable(), "upstreams": board.snapshot()}
        return JSONResponse(body, status_code=200 if usable else 503)

    @app.get("/state")
    def state() -> dict:
        d = DemandSnapshot(rates=st.src_rate, site_rates=st.site_rate, policy=pol["policy"].name, weights=st.weights)
        v = st.view()
        return {**d.model_dump(), "signal_time": v.now.isoformat(), "rtt_ms": st.rtt,
                "sites": {k: {"load": round(sv.load, 3), "est_wait_s": round(sv.est_wait_s, 3), "available": sv.available,
                              "signal": sv.signal is not None} for k, sv in v.sites.items()},
                "upstreams": board.snapshot()}

    @app.put("/admin/policy", dependencies=[Depends(require_admin)])
    def set_policy(body: PolicyBody) -> dict:
        try:
            pol["policy"] = make_routing(body.policy, body.params)
        except (KeyError, TypeError, ValueError, ValidationError) as e:
            raise HTTPException(400, str(e))
        m_policy.clear()
        m_policy.labels(pol["policy"].name).set(1)
        log.info("routing policy -> %s %s", body.policy, body.params, extra={"event": "routing_policy_changed"})
        return {"policy": pol["policy"].name}

    @app.put("/admin/weights", dependencies=[Depends(require_admin)])
    def set_weights(body: WeightsBody) -> dict:
        known_src, known_site = set(st.src_rate), set(ep.sites)
        for s, row in body.weights.items():
            if s not in known_src or not set(row) <= known_site:
                raise HTTPException(400, f"unknown source/site in weights: {s} {sorted(row)}")
        st.weights = body.weights
        m_w.clear()
        for s, row in st.weights.items():
            for d, w in row.items():
                m_w.labels(s, d).set(w)
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        return metrics_response(reg)

    return app
