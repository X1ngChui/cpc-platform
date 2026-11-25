"""Site-agent service: one per data centre, driving the data-centre runtime (shared with the simulator)
on real time.

- POST /v1/chat/completions, /v1/completions   OpenAI-compatible inference (streaming supported)
- GET  /v1/models
- GET  /state            site snapshot (SiteSnapshot) with missing power components and sensor health
- POST /admin/capacity   instruction to the data centre (CapacityTarget)
- GET  /healthz          liveness (process and event loop respond)
- GET  /readyz           readiness (startup checks passed, periodic sampling running)
- GET  /metrics          Prometheus metrics: DCGM_FI_DEV_POWER_USAGE, DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION,
                         DCGM_FI_DEV_GPU_UTIL (accelerators of real servers, named as by NVIDIA
                         dcgm-exporter), cpc_site_*, cpc_node_*, cpc_sensor_up, cpc_sensor_errors_total;
                         missing power readings are NaN
The gateway sets the headers x-cpc-source (source region) and x-cpc-attr-<field> (request attributes,
JSON values) for the local dispatch policy.

Startup checks (the process exits on failure): the power sensors of real servers work and cover the
declared accelerators; configured containers are reachable; with power capping the sensors support it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ..common import SSECheck, metrics_response, request_attrs, require_admin
from ..config import Scenario
from ..sched.routing import RequestInfo
from ..schemas import CapacityTarget
from .core import Overloaded, SiteAgent
from .executors import ExecResult, InvalidRequest, RealNodeHooks, SiteExecutor, approx_prompt_tokens, requested_output_tokens
from .power import SensorPower, node_devices

log = logging.getLogger("cpc.site")

STATE_CODE = {"off": 0, "booting": 1, "on": 2, "draining": 3}
NAN = float("nan")


def _v(x: float | None) -> float:
    return NAN if x is None else x


def upstream_error_body(node_id: str, first: bytes) -> dict:
    """Error body when the upstream fails before a stream starts: its JSON error as is, other content as the message."""
    text = first.decode("utf-8", errors="replace").strip().removeprefix("data: ")
    with suppress(ValueError):
        if isinstance(body := json.loads(text), dict) and "error" in body:
            return body
    return {"error": {"message": f"upstream {node_id} error: {text[:500] or 'empty response'}", "type": "upstream_error"}}


def build_agent(scn: Scenario, site_id: str, registry: CollectorRegistry | None = None, enable_power_cap: bool = False,
                initial_active: int | None = None) -> tuple[SiteAgent, SiteExecutor]:
    """Emulated servers use the server model; real servers use the sensors of measurement and the executor hooks."""
    cfg = scn.site(site_id)
    if not cfg.real:
        if enable_power_cap:
            raise ValueError(f"site {cfg.id} has no real servers, so hardware power capping does not apply")
        agent = SiteAgent(scn, site_id, None, initial_active)
    else:
        assert cfg.measurement is not None  # ensured by the scenario validation
        meter = SensorPower(cfg.id, cfg.measurement, registry)
        hooks = RealNodeHooks(cfg.upstreams, power=meter, enable_power_cap=enable_power_cap)
        agent = SiteAgent(scn, site_id, hooks, initial_active)
        real = [s for s in agent.servers if not s.model_power]
        meter.start(real)
        hooks.check_power_cap(cfg.upstreams)
        meter.sample(real)
        agent.meter = meter
        agent.dc.power_scope = meter.scope
    return agent, SiteExecutor(agent.servers, scn.scheduler.stream_idle_timeout_s)


class _Metrics:
    """Prometheus metrics of a site agent."""

    def __init__(self, reg: CollectorRegistry):
        node, site = ["site", "pool", "node"], ["site"]
        gpu = ["gpu", "UUID", "modelName", "Hostname", "site"]
        g = lambda name, doc, labels: Gauge(name, doc, labels, registry=reg)  # noqa: E731
        self.gpu_p = g("DCGM_FI_DEV_POWER_USAGE", "GPU power draw (W)", gpu)
        self.gpu_e = g("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "GPU energy since start (mJ)", gpu)
        self.gpu_u = g("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (%)", gpu)
        self.node_state = g("cpc_node_state", "server state: 0 off, 1 booting, 2 on, 3 draining", node)
        self.node_it = g("cpc_node_it_power_watts", "server IT power (W; NaN when missing)", [*node, "source"])
        self.node_host = g("cpc_node_host_power_watts", "host part of server power (W; NaN when missing)", node)
        self.node_valid = g("cpc_node_power_valid", "whether the server power reading is complete", node)
        self.node_busy = g("cpc_node_busy_slots", "requests in service on the server", node)
        self.node_cap = g("cpc_node_power_cap_ratio", "power-cap ratio", node)
        self.node_e = g("cpc_node_energy_joules_total", "server IT energy (J, integrated over valid power)", node)
        self.node_miss = g("cpc_node_energy_missing_seconds_total", "time with missing server power (s)", node)
        self.act_err = g("cpc_node_actuation_errors_total", "failed power and power-cap actions", ["site", "node", "action"])
        self.queue = g("cpc_site_queue_length", "queued requests", site)
        self.busy = g("cpc_site_busy_slots", "requests in service", site)
        self.slots = g("cpc_site_slots_on", "online concurrency slots", site)
        self.nodes_on = g("cpc_site_nodes_on", "online servers", site)
        self.nodes_total = g("cpc_site_nodes_total", "servers", site)
        self.it = g("cpc_site_it_power_watts", "site IT power (W; NaN when any server is missing)", site)
        self.fac = g("cpc_site_facility_power_watts", "site facility power (W) = IT x PUE; NaN when missing", site)
        self.pue = g("cpc_site_pue_ratio", "site PUE", site)
        self.cov = g("cpc_site_energy_coverage_ratio", "share of server-seconds with valid power", site)
        self.tick_err = Counter("cpc_site_tick_errors", "failed periodic work (state, power sampling)", site, registry=reg)
        self.req = Counter("cpc_site_requests", "requests handled by the site", ["site", "status"], registry=reg)
        self.tok = Counter("cpc_site_tokens", "tokens handled by the site", ["site", "kind"], registry=reg)
        self.wait = Histogram("cpc_site_queue_wait_seconds", "queueing time in the site", site, registry=reg,
                              buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
        self.svc = Histogram("cpc_site_service_seconds", "service time in the site", site, registry=reg,
                             buckets=(0.5, 1, 2, 4, 8, 16, 32, 64, 128))

    def export(self, agent: SiteAgent) -> None:
        sid = agent.cfg.id
        for n in agent.servers:
            lv = (sid, n.pool, n.id)
            self.node_state.labels(*lv).set(STATE_CODE[n.state])
            self.node_it.labels(*lv, "model" if n.model_power else "sensors").set(_v(n.it_power_w))
            accel = n.gpu_power_w if node_devices(n) else 0.0
            self.node_host.labels(*lv).set(_v(None if n.it_power_w is None or accel is None else n.it_power_w - accel))
            self.node_valid.labels(*lv).set(not n.power_missing)
            self.node_busy.labels(*lv).set(n.busy)
            self.node_cap.labels(*lv).set(n.cap_ratio)
            self.node_e.labels(*lv).set(n.energy_j)
            self.node_miss.labels(*lv).set(n.missing_s)
            if n.model_power:
                continue  # accelerator metrics only for measured servers
            d = n.detail
            for dev, p, u, model, e in zip(node_devices(n), d.get("power_w", []), d.get("util_pct", []),
                                           d.get("model", []), d.get("energy_mj", [])):
                g = dev.removeprefix("gpu")
                lbl = (g, f"GPU-{sid}-{n.id}-{g}", model, n.id, sid)
                self.gpu_p.labels(*lbl).set(_v(p))
                self.gpu_u.labels(*lbl).set(_v(u))
                self.gpu_e.labels(*lbl).set(_v(e))
        for (node, action), c in agent.dc.actuation_errors.items():
            self.act_err.labels(sid, node, action).set(c)
        s = agent.snapshot()
        for gauge, value in ((self.queue, s.queue_len), (self.busy, s.busy), (self.slots, s.slots_on),
                             (self.nodes_on, s.nodes_on), (self.nodes_total, s.nodes_total), (self.it, _v(s.it_power_w)),
                             (self.fac, _v(s.facility_power_w)), (self.pue, s.pue), (self.cov, _v(s.energy_coverage))):
            gauge.labels(sid).set(value)

    def finished(self, sid: str, ok: bool, svc: float, res: ExecResult) -> None:
        self.svc.labels(sid).observe(svc)
        self.req.labels(sid, "ok" if ok else "error").inc()
        self.tok.labels(sid, "prompt").inc(res.prompt_tokens)
        self.tok.labels(sid, "completion").inc(res.completion_tokens)


def create_app(scn: Scenario, site_id: str, tick_s: float = 0.5, enable_power_cap: bool = False,
               initial_active: int | None = None) -> FastAPI:
    reg = CollectorRegistry()
    agent, executor = build_agent(scn, site_id, reg, enable_power_cap, initial_active)
    cfg = agent.cfg
    m = _Metrics(reg)
    status = {"last_tick": None, "last_tick_error": None}

    async def ticker() -> None:
        while True:
            try:
                await agent.tick()
                m.export(agent)
                status["last_tick"] = time.time()
            except Exception as e:  # noqa: BLE001 - one failed period does not stop the service; counted, shown in /readyz
                m.tick_err.labels(cfg.id).inc()
                status["last_tick_error"] = repr(e)
                log.exception("site %s: periodic work failed", cfg.id, extra={"event": "site_tick_failed", "site": cfg.id})
            await asyncio.sleep(tick_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title=f"cpc site agent [{cfg.id}]", lifespan=lifespan)
    app.state.agent = agent
    max_tick_age = max(5.0, 10 * tick_s)

    def error(status_code: int, message: str, typ: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse({"error": {"message": message, "type": typ}}, status_code=status_code,
                            headers=headers or {"x-cpc-site": cfg.id})

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive", "site": cfg.id}

    @app.get("/readyz")
    def readyz():
        age = None if status["last_tick"] is None else time.time() - status["last_tick"]
        ready = age is not None and age <= max_tick_age
        return JSONResponse({"ready": ready, "site": cfg.id, "last_tick_age_s": None if age is None else round(age, 3),
                             "last_tick_error": status["last_tick_error"]}, status_code=200 if ready else 503)

    @app.get("/state")
    def state():
        return agent.snapshot().model_dump()

    @app.post("/admin/capacity", dependencies=[Depends(require_admin)])
    async def capacity(target: CapacityTarget):
        agent.apply(target)  # async on purpose: apply() creates power tasks on the event loop
        snap = agent.snapshot()
        log.info("capacity target %s -> on=%d booting=%d draining=%d", target.model_dump(), snap.nodes_on,
                 snap.nodes_booting, snap.nodes_draining, extra={"event": "capacity_applied", "site": cfg.id})
        return snap.model_dump()

    @app.get("/v1/models")
    async def models():
        code, body = await executor.models()
        return JSONResponse(body, status_code=code)

    async def handle(request: Request, path: str):
        body = await request.json()
        try:
            executor.validate(body)
        except InvalidRequest as e:
            m.req.labels(cfg.id, "invalid").inc()
            return error(400, str(e), "invalid_request_error")
        info = RequestInfo(source=request.headers.get("x-cpc-source", ""), input_tokens=approx_prompt_tokens(body),
                           output_tokens=requested_output_tokens(body), id=request.headers.get("x-request-id", ""),
                           attrs=request_attrs(request.headers))
        t0 = time.monotonic()
        try:
            node = await agent.acquire(info)
        except Overloaded:
            m.req.labels(cfg.id, "rejected").inc()
            return error(503, f"site {cfg.id} overloaded", "overloaded")
        wait = time.monotonic() - t0
        m.wait.labels(cfg.id).observe(wait)
        hdr = {"x-cpc-site": cfg.id, "x-cpc-node": node.id, "x-cpc-queue-s": f"{wait:.4f}"}
        t1 = time.monotonic()
        if body.get("stream"):
            res = ExecResult(200, None)
            agen = executor.of(node).stream(node, path, body, res)
            try:  # wait for the first chunk: connection failures and upstream errors return their HTTP status, not a 200 stream
                first = await anext(agen)
            except StopAsyncIteration:
                first, res.status = b"", 502
            except BaseException:
                agent.release(node, None)
                raise
            if res.status != 200:
                await agen.aclose()
                agent.release(node, None)  # failed requests do not enter the mean service time
                m.req.labels(cfg.id, "error").inc()
                return JSONResponse(upstream_error_body(node.id, first), status_code=res.status, headers=hdr)

            async def gen():
                check = SSECheck()
                try:
                    check.feed(first)
                    yield first
                    async for chunk in agen:
                        check.feed(chunk)
                        yield chunk
                finally:
                    svc = time.monotonic() - t1
                    ok = res.status == 200 and check.problem is None
                    agent.release(node, svc if ok else None)
                    m.finished(cfg.id, ok, svc, res)

            return StreamingResponse(gen(), media_type="text/event-stream", headers=hdr)
        res = None
        try:
            res = await executor.of(node).complete(node, path, body)
        finally:
            svc = time.monotonic() - t1
            agent.release(node, svc if res is not None and res.status == 200 else None)
        m.finished(cfg.id, res.status == 200, svc, res)
        hdr["x-cpc-service-s"] = f"{svc:.4f}"
        return JSONResponse(res.body, status_code=res.status, headers=hdr)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await handle(request, "/v1/chat/completions")

    @app.post("/v1/completions")
    async def completions(request: Request):
        return await handle(request, "/v1/completions")

    @app.get("/metrics")
    def metrics():
        return metrics_response(reg)

    return app
