"""Grid-signal service (grid).

- GET  /v1/clock                 signal clock (other services synchronise with it)
- GET  /v1/signals[?at=ISO]      price, carbon intensity and power limit of every site now (or at a
                                 given time); sites without a signal are listed under missing
- GET  /v1/events  POST /v1/events  DELETE /v1/events   list, inject and clear demand-response events
- GET  /v1/config                scenario text and service addresses (admin token), fetched at startup
                                 by site agents on other machines
- GET  /healthz, /readyz         liveness; readiness (data are loaded and checked at startup, so a
                                 running service is ready)
- GET  /metrics                  Prometheus metrics (cpc_grid_*)

Startup checks: the price, carbon and extra-signal datasets follow the specification, cover the
regions of all sites and the start of the scenario clock. Any failure stops the service.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException
from prometheus_client import CollectorRegistry, Gauge
from pydantic import ValidationError

from ..common import metrics_response, require_admin
from ..config import DREvent, Scenario
from ..models import SimClock
from .signals import GridModel

NAN = float("nan")


def create_app(scn: Scenario, clock: SimClock | None = None, config: dict | None = None) -> FastAPI:
    """config: {"scenario": scenario text, "endpoints": service addresses}; enables GET /v1/config."""
    model = GridModel(scn)
    clock = clock or SimClock(start=scn.clock.start, speedup=scn.clock.speedup)
    reg = CollectorRegistry()
    cur = model.currency
    g_price = Gauge("cpc_grid_price_per_kwh", f"electricity price ({cur}/kWh)", ["site", "region", "currency"], registry=reg)
    g_carbon = Gauge("cpc_grid_carbon_intensity_kg_per_kwh", "carbon intensity (kgCO2/kWh)", ["site", "region"], registry=reg)
    g_extra = Gauge("cpc_grid_extra_signal", "extra signal (unit in the dataset descriptor)", ["site", "signal"], registry=reg)
    g_cap = Gauge("cpc_grid_power_cap_kw", "facility power limit (kW, -1 without a limit)", ["site"], registry=reg)
    g_dr = Gauge("cpc_grid_dr_event_active", "whether a demand-response event is active", ["site"], registry=reg)
    g_eff = Gauge("cpc_grid_effective_price_per_kwh", f"price including carbon cost ({cur}/kWh)", ["site", "currency"],
                  registry=reg)
    g_avail = Gauge("cpc_grid_signal_available", "whether the site has a signal now (1) or data are missing (0)",
                    ["site"], registry=reg)
    g_time = Gauge("cpc_grid_signal_time_seconds", "signal clock (Unix time)", registry=reg)

    def refresh() -> None:
        now = clock.now()
        g_time.set(now.timestamp())
        signals, missing = model.snapshot(now)
        for site in scn.sites:
            sid, region = site.id, site.region
            if (s := signals.get(sid)) is None:
                g_avail.labels(sid).set(0)
                for g in (g_price.labels(sid, region, cur), g_carbon.labels(sid, region), g_eff.labels(sid, cur)):
                    g.set(NAN)
                continue
            g_avail.labels(sid).set(1)
            g_price.labels(sid, region, cur).set(s.price)
            g_carbon.labels(sid, region).set(s.carbon_intensity)
            g_eff.labels(sid, cur).set(s.effective_price)
            g_cap.labels(sid).set(-1 if s.power_cap_kw is None else s.power_cap_kw)
            g_dr.labels(sid).set(bool(s.dr_event))
            for k, v in s.extra.items():
                g_extra.labels(sid, k).set(NAN if v is None else v)

    async def ticker() -> None:
        while True:
            refresh()
            await asyncio.sleep(1.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="cpc grid-signal service", lifespan=lifespan)
    app.state.model = model
    app.state.clock = clock

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz() -> dict:
        return {"ready": True, "sites": len(scn.sites), "events": len(model.events), "sources": model.sources()}

    if config is not None:
        @app.get("/v1/config", dependencies=[Depends(require_admin)])
        def get_config() -> dict:
            return config

    @app.get("/v1/clock")
    def get_clock() -> dict:
        return clock.to_dict()

    @app.get("/v1/signals")
    def signals(at: datetime | None = None) -> dict:
        t = at or clock.now()
        if t.tzinfo is None:
            raise HTTPException(400, "at needs a UTC offset (RFC 3339)")
        signals, missing = model.snapshot(t)
        return {"time": t.isoformat(), "signals": {k: v.model_dump(mode="json") for k, v in signals.items()},
                "missing": missing, "sources": model.sources()}

    @app.get("/v1/events")
    def list_events() -> list[dict]:
        return [e.model_dump(mode="json") for e in model.events]

    @app.post("/v1/events", dependencies=[Depends(require_admin)])
    def add_event(body: dict) -> dict:
        """Inject a demand-response event with start/end (ISO) or duration_min from start (default: now)."""
        try:
            body.setdefault("start", clock.now().isoformat())
            if "end" not in body:
                minutes = float(body.pop("duration_min", 60))
                body["end"] = (datetime.fromisoformat(str(body["start"])) + timedelta(minutes=minutes)).isoformat()
            ev = DREvent.model_validate(body)
            model.add_event(ev)
        except (KeyError, ValueError, ValidationError) as e:
            raise HTTPException(400, str(e)) from e
        refresh()
        return ev.model_dump(mode="json")

    @app.delete("/v1/events", dependencies=[Depends(require_admin)])
    def clear_events(site: str | None = None) -> dict:
        model.clear_events(site)
        refresh()
        return {"remaining": len(model.events)}

    @app.get("/metrics")
    def metrics():
        refresh()
        return metrics_response(reg)

    return app
