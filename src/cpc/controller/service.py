"""Capacity controller.

Every control period:
  1. collect the site states (/state), grid signals (grid /v1/signals) and gateway demand (gateway /state);
  2. check the inputs: unreachable sites are left out of the period (excluded); without any usable
     site, or when inputs the policy requires (signals / demand) are missing or incomplete, the
     period is skipped with the reason, never decided with default inputs;
  3. decide: policy.decide(view) -> online servers, power-cap ratio per site and optional routing weights;
  4. apply: instructions to the site agents (POST /admin/capacity), weights to the gateway;
  5. record: decision log (JSONL with result = applied / skipped and the reason) and Prometheus metrics.

Startup: the signal clock must be synchronised from the grid-signal service in time.

Metrics: cpc_controller_cycles_total{result,reason}, cpc_controller_collect_errors_total{target},
cpc_controller_apply_errors_total{site}, cpc_upstream_up{component="controller",upstream}.

Interface: GET /state (last period and upstream health), GET /healthz, /readyz, PUT /admin/policy,
POST /admin/step (run a period now), GET /metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry, Counter, Gauge
from pydantic import BaseModel, Field, ValidationError

from ..common import JsonlWriter, admin_headers, metrics_response, require_admin, sync_clock
from ..config import ConfigError, Endpoints, Scenario
from ..grid.signals import GridSignal
from ..health import HealthBoard, describe
from ..models import rtt_matrix
from ..schemas import CapacityTarget, DemandSnapshot, SiteSnapshot
from ..sched import CapacityPlan, ControlView, make_capacity

log = logging.getLogger("cpc.controller")


class PolicyBody(BaseModel):
    policy: str
    params: dict = Field(default_factory=dict)


def _observed(s: SiteSnapshot) -> dict:
    return {"nodes_on": s.nodes_on, "busy": s.busy, "queue": s.queue_len,
            "it_kw": None if s.it_power_w is None else round(s.it_power_w / 1000, 3),
            "energy_coverage": s.energy_coverage, "arrival_rate": s.arrival_rate}


def _signal(g: GridSignal) -> dict:
    return {"price": g.price, "price_label": g.price_label, "carbon_intensity": g.carbon_intensity, "cap_kw": g.power_cap_kw}


def create_app(scn: Scenario, ep: Endpoints, policy: str | None = None, params: dict | None = None,
               interval_s: float | None = None, log_path: str | None = None, startup_timeout_s: float = 30.0,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """transport injects faults in tests (httpx.MockTransport); default: the real network."""
    cap = scn.scheduler.capacity
    pol = {"policy": make_capacity(policy or cap.policy, params if policy else cap.params)}
    interval = interval_s or scn.scheduler.control_interval_s
    client = httpx.AsyncClient(timeout=5.0, headers=admin_headers(), trust_env=False, transport=transport)
    writer = JsonlWriter(log_path)
    rtt = rtt_matrix(scn)
    min_active = {s.id: s.min_active for s in scn.sites}
    state: dict = {"record": None, "clock": None}

    reg = CollectorRegistry()
    board = HealthBoard("upstream", reg, {"component": "controller"})
    for name in ("grid", "gateway", *(f"site:{s}" for s in ep.sites)):
        board.register(name)
    g_nodes = Gauge("cpc_controller_target_nodes", "target online servers", ["site"], registry=reg)
    g_cap = Gauge("cpc_controller_target_cap_ratio", "target power-cap ratio", ["site"], registry=reg)
    c_cycles = Counter("cpc_controller_cycles", "control periods (result: applied / skipped / error; reason: why skipped)",
                       ["result", "reason"], registry=reg)
    c_dec = Counter("cpc_controller_decisions", "decisions", ["policy"], registry=reg)
    c_collect = Counter("cpc_controller_collect_errors", "failed collections (target: grid / gateway / site:<id>)",
                        ["target"], registry=reg)
    c_apply = Counter("cpc_controller_apply_errors", "failed instructions", ["site"], registry=reg)
    c_push = Counter("cpc_controller_weight_push_errors", "failed routing-weight updates", registry=reg)
    g_pol = Gauge("cpc_controller_policy_info", "current capacity policy", ["policy"], registry=reg)
    g_last = Gauge("cpc_controller_last_cycle_timestamp_seconds", "time of the last control period (Unix s)", ["result"],
                   registry=reg)

    async def get(url: str):
        r = await client.get(url, timeout=3.0)
        r.raise_for_status()
        return r.json()

    def failed(target: str, e: BaseException) -> str:
        board.fail(target, e)
        c_collect.labels(target).inc()
        return describe(e)

    async def send(site: str, target: CapacityTarget) -> str | None:
        """Send an instruction to a site agent; returns the error, None on success."""
        try:
            r = await client.post(ep.sites[site] + "/admin/capacity", json=target.model_dump(), timeout=5.0)
        except httpx.HTTPError as e:
            return describe(e)
        return None if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:200]}"

    async def collect() -> tuple[ControlView | None, str | None, dict]:
        """Returns (view, reason to skip, collection info); the reason is None when the policy's inputs are complete."""
        info: dict = {"excluded": {}, "errors": {}}
        sites: dict[str, SiteSnapshot] = {}
        res = await asyncio.gather(*(get(u + "/state") for u in ep.sites.values()), return_exceptions=True)
        for sid, r in zip(ep.sites, res):
            try:
                if isinstance(r, BaseException):
                    raise r
                sites[sid] = SiteSnapshot.model_validate(r)
                board.ok(f"site:{sid}")
            except (httpx.HTTPError, ValidationError) as e:
                info["excluded"][sid] = failed(f"site:{sid}", e)
        signals: dict[str, GridSignal] | None = None
        try:
            signals = {k: GridSignal.model_validate(v) for k, v in (await get(ep.grid + "/v1/signals"))["signals"].items()}
            board.ok("grid")
        except (httpx.HTTPError, ValueError, KeyError) as e:
            info["errors"]["grid"] = failed("grid", e)
        demand: DemandSnapshot | None = None
        try:
            demand = DemandSnapshot.model_validate(await get(ep.gateway + "/state"))
            board.ok("gateway")
        except (httpx.HTTPError, ValueError) as e:
            info["errors"]["gateway"] = failed("gateway", e)

        req = pol["policy"].requires
        if not sites:
            return None, "no_site_available", info
        if "signals" in req:
            if signals is None:
                return None, "missing_signals", info
            if lack := sorted(sites.keys() - signals.keys()):
                info["signals_missing_for"] = lack
                return None, "incomplete_signals", info
        if "demand" in req and demand is None:
            return None, "missing_demand", info
        view = ControlView(now=state["clock"].now(), interval_s=interval, sites=sites, signals=signals or {},
                           demand=demand, rtt_ms=rtt, slo=scn.slo, min_active=min_active, excluded=info["excluded"])
        return view, None, info

    def record(rec: dict) -> dict:
        writer.write(rec)
        state["record"] = rec
        g_last.labels(rec["result"]).set(rec["t_wall"])
        return rec

    async def apply(plan: CapacityPlan, rec: dict) -> dict:
        errors = await asyncio.gather(*(send(sid, tgt) for sid, tgt in plan.targets.items()))
        applied = {}
        for (sid, tgt), err in zip(plan.targets.items(), errors):
            applied[sid] = {"ok": err is None, "error": err}
            if err is not None:
                c_apply.labels(sid).inc()
                log.warning("site %s: capacity instruction failed: %s", sid, err,
                            extra={"event": "capacity_apply_failed", "site": sid})
            if tgt.active_nodes is not None:
                g_nodes.labels(sid).set(tgt.active_nodes)
            if tgt.cap_ratio is not None:
                g_cap.labels(sid).set(tgt.cap_ratio)
        pushed = None
        if plan.weights is not None:
            try:
                r = await client.put(ep.gateway + "/admin/weights", json={"weights": plan.weights})
                r.raise_for_status()
                pushed = True
                board.ok("gateway")
            except httpx.HTTPError as e:
                pushed = False
                c_push.inc()
                rec["weights_error"] = describe(e)
                board.fail("gateway", e)
        rec["weights_pushed"] = pushed
        return applied

    async def step() -> dict:
        view, skip, info = await collect()
        policy = pol["policy"]
        rec = {"t_wall": time.time(), "t_signal": state["clock"].now().isoformat(), "policy": policy.name,
               "excluded": info.pop("excluded"), "collect_errors": info.pop("errors")}
        if skip is not None:
            c_cycles.labels("skipped", skip).inc()
            log.warning("control period skipped: %s %s", skip, info, extra={"event": "control_cycle_skipped", "reason": skip})
            return record({**rec, "result": "skipped", "reason": skip, **info})
        plan = policy.decide(view)
        c_dec.labels(policy.name).inc()
        applied = await apply(plan, rec)
        c_cycles.labels("applied", "").inc()
        return record(rec | {
            "result": "applied",
            "targets": {k: v.model_dump() for k, v in plan.targets.items()},
            "weights": plan.weights,
            "notes": plan.notes,
            "applied": applied,
            "observed": {k: _observed(s) for k, s in view.sites.items()},
            "signals": {k: _signal(g) for k, g in view.signals.items()},
            "demand": view.demand.rates if view.demand else None,
        })

    async def loop() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await step()
            except Exception:  # noqa: BLE001 - one failed period does not stop the loop; logged and counted
                c_cycles.labels("error", "exception").inc()
                log.exception("control period failed", extra={"event": "control_cycle_error"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["clock"] = await sync_clock(client, ep.grid, startup_timeout_s)  # startup fails if unreachable
        board.ok("grid")
        writer.start()
        task = asyncio.create_task(loop())
        yield
        task.cancel()
        await writer.stop()
        await client.aclose()

    app = FastAPI(title="cpc capacity controller", lifespan=lifespan)
    g_pol.labels(pol["policy"].name).set(1)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        ready = state["clock"] is not None
        return JSONResponse({"ready": ready, "upstreams": board.snapshot()}, status_code=200 if ready else 503)

    @app.get("/state")
    def get_state() -> dict:
        return {"policy": pol["policy"].name, "requires": sorted(pol["policy"].requires), "interval_s": interval,
                "upstreams": board.snapshot(), "last": state["record"]}

    @app.post("/admin/step", dependencies=[Depends(require_admin)])
    async def force_step() -> dict:
        return await step()

    @app.put("/admin/policy", dependencies=[Depends(require_admin)])
    def set_policy(body: PolicyBody) -> dict:
        try:
            pol["policy"] = make_capacity(body.policy, body.params)
        except (ConfigError, KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        g_pol.clear()
        g_pol.labels(pol["policy"].name).set(1)
        log.info("capacity policy -> %s %s", body.policy, body.params, extra={"event": "capacity_policy_changed"})
        return {"policy": pol["policy"].name}

    @app.get("/metrics")
    def metrics():
        return metrics_response(reg)

    return app
