"""Global routing gateway.

Users (or the load replay of cpc live) send OpenAI-compatible requests to the gateway, which chooses
a site with the routing policy and forwards them. The header x-cpc-source names the source region;
without it the request belongs to network.default_source of the scenario, or is rejected with 400.
Headers x-cpc-attr-<field> (JSON values) carry request attributes to the routing policy
(RequestInfo.attrs) and on to the site agent.

Wide-area latency between regions is emulated by delay injection: RTT/2 before forwarding and RTT/2
before the response (or its first streamed chunk). Disable it (network.emulate_wan: false) in real
multi-region deployments, where the network produces the latency.

Failure handling:
- startup: the signal clock must be synchronised from the grid-signal service in time;
- site states and grid signals have their own health (cpc_upstream_up{component="gateway",upstream});
  sites whose state failed or was not refreshed within scheduler.state_stale_s leave the routing view,
  and missing signals make SiteView.signal None;
- routing policies return decisions with a reason (cpc_gateway_route_decisions_total{policy,reason});
- forwarding failures follow scheduler.retry: retryable failures (overloaded, connect_error) exclude
  the site and decide again, at most max_attempts times; failures after the request was delivered are
  not retried (502). Every attempt counts in cpc_gateway_attempts_total{site,outcome}.

- POST /v1/chat/completions, /v1/completions   route and forward
- GET  /v1/models
- GET  /state            demand statistics (DemandSnapshot), site view and upstream health
- GET  /healthz, /readyz liveness; readiness (at least one site state usable)
- PUT  /admin/policy     {"policy": "...", "params": {...}}  switch the routing policy at run time
- PUT  /admin/weights    {"weights": {source: {site: w}}}    routing weights from the controller
- GET  /metrics
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter as Tally
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from pydantic import BaseModel, Field, ValidationError

from ..common import JsonlWriter, SSECheck, attr_headers, metrics_response, request_attrs, require_admin, sync_clock
from ..config import ConfigError, Endpoints, Scenario
from ..grid.signals import GridSignal
from ..health import HealthBoard, describe
from ..models import SimClock, rtt_matrix
from ..schemas import DemandSnapshot, SiteSnapshot
from ..sched import ClusterView, RequestInfo, RouteDecision, SiteView, make_routing
from ..site.executors import approx_prompt_tokens, requested_output_tokens

log = logging.getLogger("cpc.gateway")

CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128)
RATE_EWMA = 0.2  # weight of the last second in the arrival-rate estimates


class PolicyBody(BaseModel):
    policy: str
    params: dict = Field(default_factory=dict)


class WeightsBody(BaseModel):
    """Routing weights: {source: {site: non-negative finite weight}}."""

    weights: dict[str, dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)]]]


def _ewma(rates: dict[str, float], counts: Tally) -> None:
    for k in rates:
        rates[k] += RATE_EWMA * (counts[k] - rates[k])
    counts.clear()


class GatewayState:
    def __init__(self, scn: Scenario, ep: Endpoints, board: HealthBoard):
        self.scn = scn
        self.ep = ep
        self.board = board
        self.sites: dict[str, SiteSnapshot] = {}
        self.signals: dict[str, GridSignal] = {}
        self.pending: Tally[str] = Tally()
        self.weights: dict[str, dict[str, float]] = {}
        self.rtt = rtt_matrix(scn)
        self.clock: SimClock | None = None
        self.src_count: Tally[str] = Tally()
        self.site_count: Tally[str] = Tally()
        self.src_rate = dict.fromkeys((s.id for s in scn.sources), 0.0)
        self.site_rate = dict.fromkeys((s.id for s in scn.sites), 0.0)

    def _fresh(self, key: str) -> bool:
        return self.board.is_up(key) and not self.board.stale(key, self.scn.scheduler.state_stale_s)

    def site_usable(self, sid: str) -> bool:
        return sid in self.sites and self._fresh(f"site:{sid}")

    def signals_usable(self) -> bool:
        return self._fresh("grid")

    def usable_sites(self) -> list[str]:
        return sorted(sid for sid in self.ep.sites if self.site_usable(sid))

    def view(self) -> ClusterView:
        assert self.clock is not None
        sig_ok = self.signals_usable()
        sv = {sid: SiteView(snap=s, signal=self.signals.get(sid) if sig_ok else None, pending=self.pending[sid])
              for sid, s in self.sites.items() if self.site_usable(sid)}
        return ClusterView(now=self.clock.now(), sites=sv, rtt_ms=self.rtt, slo=self.scn.slo, weights=self.weights)

    def tick_rates(self) -> None:
        _ewma(self.src_rate, self.src_count)
        _ewma(self.site_rate, self.site_count)


def _error(message: str, typ: str, **extra) -> dict:
    return {"error": {"message": message, "type": typ, **extra}}


def create_app(scn: Scenario, ep: Endpoints, policy: str | None = None, params: dict | None = None,
               log_path: str | None = None, startup_timeout_s: float = 30.0,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """transport injects faults in tests (httpx.MockTransport); default: the real network."""
    reg = CollectorRegistry()
    board = HealthBoard("upstream", reg, {"component": "gateway"})
    board.register("grid")
    for sid in ep.sites:
        board.register(f"site:{sid}")
    st = GatewayState(scn, ep, board)
    routing = scn.scheduler.routing
    pol = {"policy": make_routing(policy or routing.policy, params if policy else routing.params)}
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0), trust_env=False, transport=transport,
                               limits=httpx.Limits(max_connections=4000, max_keepalive_connections=1000))
    writer = JsonlWriter(log_path)
    emulate, retry, slo = scn.network.emulate_wan, scn.scheduler.retry, scn.slo
    refresh_s, idle_s = scn.scheduler.state_refresh_s, scn.scheduler.stream_idle_timeout_s

    m_req = Counter("cpc_gateway_requests", "gateway requests", ["source", "site", "status"], registry=reg)
    m_att = Counter("cpc_gateway_attempts", "forwarding attempts (outcome: ok / overloaded / connect_error / upstream_error)",
                    ["site", "outcome"], registry=reg)
    m_dec = Counter("cpc_gateway_route_decisions", "routing decisions (reason given by the policy)", ["policy", "reason"],
                    registry=reg)
    m_slo = Counter("cpc_gateway_slo", "SLO attainment", ["source", "site", "result"], registry=reg)
    m_e2e = Histogram("cpc_gateway_e2e_seconds", "end-to-end latency (with injected WAN delay)", ["source", "site"],
                      buckets=LAT_BUCKETS, registry=reg)
    m_ttft = Histogram("cpc_gateway_ttft_seconds", "time to first token (streaming requests)", ["source", "site"],
                       buckets=LAT_BUCKETS, registry=reg)
    m_tok = Counter("cpc_gateway_tokens", "tokens", ["source", "site", "kind"], registry=reg)
    m_rtt = Gauge("cpc_gateway_wan_rtt_ms", "round-trip time from source to site (ms)", ["source", "site"], registry=reg)
    m_w = Gauge("cpc_gateway_route_weight", "routing weight from the controller", ["source", "site"], registry=reg)
    m_rate = Gauge("cpc_gateway_source_rate", "arrival rate per source region (requests/s)", ["source"], registry=reg)
    m_policy = Gauge("cpc_gateway_policy_info", "current routing policy", ["policy"], registry=reg)
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
        except (httpx.HTTPError, ValueError) as e:  # ValueError includes invalid bodies (pydantic ValidationError)
            board.fail(f"site:{sid}", e)

    async def poll_signals() -> None:
        try:
            r = await client.get(ep.grid + "/v1/signals", timeout=2.0)
            r.raise_for_status()
            st.signals = {k: GridSignal.model_validate(v) for k, v in r.json()["signals"].items()}
            board.ok("grid")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            board.fail("grid", e)

    async def poll_all() -> None:
        await asyncio.gather(poll_signals(), *(poll_site(sid, url) for sid, url in ep.sites.items()))

    async def every(period_s: float, fn) -> None:
        while True:
            await asyncio.sleep(period_s)
            await fn()

    async def rates() -> None:
        st.tick_rates()
        for k, v in st.src_rate.items():
            m_rate.labels(k).set(v)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st.clock = await sync_clock(client, ep.grid, startup_timeout_s)  # startup fails if unreachable
        board.ok("grid")
        await poll_all()
        writer.start()
        tasks = [asyncio.create_task(every(refresh_s, poll_all)), asyncio.create_task(every(1.0, rates))]
        yield
        for t in tasks:
            t.cancel()
        await writer.stop()
        await client.aclose()

    app = FastAPI(title="cpc gateway", lifespan=lifespan)
    app.state.gw = st
    m_policy.labels(pol["policy"].name).set(1)

    def finish(rec: dict, status: int, src: str, sid: str, t0: float, n_in: int, n_out: int | None, ttft: float | None) -> None:
        e2e = time.monotonic() - t0
        m_req.labels(src, sid, str(status)).inc()
        if status == 200:
            m_e2e.labels(src, sid).observe(e2e)
            m_tok.labels(src, sid, "prompt").inc(n_in)
            if ttft is not None:
                m_ttft.labels(src, sid).observe(ttft)
            if n_out is not None:
                m_tok.labels(src, sid, "completion").inc(n_out)
                if ttft is not None:
                    ok = ttft <= slo.ttft_s and (e2e - ttft) / max(n_out - 1, 1) <= slo.tpot_s
                else:
                    ok = e2e <= slo.ttft_s + n_out * slo.tpot_s
                m_slo.labels(src, sid, "met" if ok else "violated").inc()
                rec["slo_ok"] = ok
        rec.update(status=status, e2e_s=round(e2e, 4), ttft_s=None if ttft is None else round(ttft, 4))
        writer.write(rec)

    def reject(rec: dict, src: str, status: int, body: dict) -> JSONResponse:
        m_req.labels(src, "none", str(status)).inc()
        rec.update(site=None, status=status, error=body["error"]["type"])
        writer.write(rec)
        return JSONResponse(body, status_code=status)

    async def relay(r: httpx.Response, rec: dict, req: RequestInfo, sid: str, rtt_s: float, t0: float):
        """Relay a streaming response; stalls, transport errors and incomplete streams fail the request (502)."""
        ttft = None
        check = SSECheck()
        chunks = aiter(r.aiter_raw())
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(chunks), idle_s)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    rec["stream_error"] = f"stream_idle: no data in {idle_s:g} s"
                    break
                check.feed(chunk)
                if ttft is None:
                    if rtt_s:
                        await asyncio.sleep(rtt_s / 2)
                    ttft = time.monotonic() - t0
                yield chunk
        except httpx.HTTPError as e:
            rec["stream_error"] = describe(e)
        finally:
            await r.aclose()
            if "stream_error" not in rec and check.problem is not None:
                rec["stream_error"] = check.problem
            if err := rec.get("stream_error"):
                log.warning("stream from site %s interrupted: %s", sid, err,
                            extra={"event": "stream_interrupted", "site": sid, "request": req.id})
            finish(rec, 502 if err else r.status_code, req.source, sid, t0, req.input_tokens, req.output_tokens, ttft)

    async def route(request: Request, path: str):
        body = await request.json()
        if (src := request.headers.get("x-cpc-source") or scn.network.default_source) is None:
            raise HTTPException(400, "the request has no x-cpc-source header and the scenario sets no network.default_source")
        if src not in st.src_rate:
            raise HTTPException(400, f"unknown source region {src}; available: {sorted(st.src_rate)}")
        st.src_count[src] += 1
        n_in, n_out = approx_prompt_tokens(body), requested_output_tokens(body)
        attrs = request_attrs(request.headers)
        req = RequestInfo(source=src, input_tokens=n_in, output_tokens=n_out, id=uuid.uuid4().hex[:12], attrs=attrs)
        fwd = {"x-cpc-source": src, "x-request-id": req.id, **attr_headers(attrs)}
        t0, tried, attempts = time.monotonic(), [], []
        stream = bool(body.get("stream"))
        policy = pol["policy"]
        base = {"id": req.id, "t_wall": time.time(), "source": src, "policy": policy.name, "input_tokens_approx": n_in,
                "output_tokens": n_out, "stream": stream}
        decision: RouteDecision | None = None
        for _ in range(retry.max_attempts):
            view = st.view()
            if not isinstance(decision := policy.choose(req, view, exclude=tried), RouteDecision):
                raise TypeError(f"choose() of routing policy {policy.name} must return a RouteDecision, "
                                f"got {type(decision).__name__}")
            m_dec.labels(policy.name, decision.reason).inc()
            if (sid := decision.site) is None:
                break
            st.pending[sid] += 1
            st.site_count[sid] += 1
            rtt_s = st.rtt[src][sid] / 1000.0 if emulate else 0.0
            sig = view.sites[sid].signal
            rec = {**base, "t_signal": view.now.isoformat(), "site": sid, "route_reason": decision.reason,
                   "rtt_ms": st.rtt[src][sid], "price": sig.price if sig else None,
                   "carbon_intensity": sig.carbon_intensity if sig else None, "attempts": attempts}
            if rtt_s:
                await asyncio.sleep(rtt_s / 2)
            url = ep.sites[sid] + path
            try:
                if stream:
                    r = await client.send(client.build_request("POST", url, json=body, headers=fwd), stream=True)
                else:
                    r = await client.post(url, json=body, headers=fwd)
            except CONNECT_ERRORS as e:
                m_att.labels(sid, "connect_error").inc()
                board.fail(f"site:{sid}", e)
                attempts.append({"site": sid, "outcome": "connect_error", "error": describe(e)})
                if "connect_error" in retry.retry_on:
                    tried.append(sid)
                    continue
                return reject(rec, src, 502, _error(f"connect to site {sid} failed", "upstream_error", attempts=attempts))
            except httpx.HTTPError as e:
                m_att.labels(sid, "upstream_error").inc()
                attempts.append({"site": sid, "outcome": "upstream_error", "error": describe(e)})
                return reject(rec, src, 502, _error(f"site {sid} failed after the request was sent", "upstream_error",
                                                    attempts=attempts))
            if r.status_code == 503 and r.headers.get("x-cpc-site") == sid:
                if stream:
                    await r.aclose()
                m_att.labels(sid, "overloaded").inc()
                attempts.append({"site": sid, "outcome": "overloaded"})
                if "overloaded" in retry.retry_on:
                    tried.append(sid)
                    continue
                return reject(rec, src, 503, _error(f"site {sid} overloaded", "overloaded", attempts=attempts))
            m_att.labels(sid, "ok").inc()
            attempts.append({"site": sid, "outcome": "ok", "status": r.status_code})
            rec.update(node=r.headers.get("x-cpc-node"), queue_s=r.headers.get("x-cpc-queue-s"))
            hdr = {k: v for k, v in r.headers.items() if k.startswith("x-cpc-")} | {
                "x-cpc-source": src, "x-cpc-route-reason": decision.reason}
            if stream and r.status_code != 200:  # the site answered a streaming request with an error: return it as such
                await r.aread()
                await r.aclose()
                stream = False
            if stream:
                return StreamingResponse(relay(r, rec, req, sid, rtt_s, t0), status_code=r.status_code,
                                         media_type="text/event-stream", headers=hdr)
            if rtt_s:
                await asyncio.sleep(rtt_s / 2)
            finish(rec, r.status_code, src, sid, t0, n_in, n_out, None)
            try:
                content = r.json()
            except ValueError:
                content = _error(r.text[:500], "upstream_error")
            return JSONResponse(content, status_code=r.status_code, headers=hdr)
        rec = {**base, "attempts": attempts, "route_reason": decision.reason if decision else None}
        if decision is not None and decision.site is None:
            return reject(rec, src, 503, _error(f"route rejected: {decision.reason}", "route_rejected",
                                                reason=decision.reason, attempts=attempts))
        return reject(rec, src, 503, _error(f"retries exhausted after {len(attempts)} attempts", "retries_exhausted",
                                            attempts=attempts))

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await route(request, "/v1/chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await route(request, "/v1/completions")

    @app.get("/v1/models")
    async def models():
        if not (usable := st.usable_sites()):
            raise HTTPException(503, "no site available")
        try:
            r = await client.get(ep.sites[usable[0]] + "/v1/models", timeout=3.0)
            return JSONResponse(r.json(), status_code=r.status_code)
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"site {usable[0]} error: {describe(e)}") from e

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        usable = st.usable_sites()
        return JSONResponse({"ready": bool(usable), "usable_sites": usable, "signals": st.signals_usable(),
                             "upstreams": board.snapshot()}, status_code=200 if usable else 503)

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
        except (ConfigError, KeyError, TypeError, ValueError, ValidationError) as e:
            raise HTTPException(400, str(e)) from e
        m_policy.clear()
        m_policy.labels(pol["policy"].name).set(1)
        log.info("routing policy -> %s %s", body.policy, body.params, extra={"event": "routing_policy_changed"})
        return {"policy": pol["policy"].name}

    @app.put("/admin/weights", dependencies=[Depends(require_admin)])
    def set_weights(body: WeightsBody) -> dict:
        for s, row in body.weights.items():
            if s not in st.src_rate or not row.keys() <= ep.sites.keys():
                raise HTTPException(400, f"unknown source or site in weights: {s} {sorted(row)}")
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
