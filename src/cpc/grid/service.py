"""电网信号服务（grid）。

对外提供：
- GET  /v1/clock                     信号时钟（其他服务据此同步）
- GET  /v1/signals[?at=ISO时间]       各站点当前（或指定时刻）的电价、碳强度、功率上限
- GET  /v1/forecast?site=&hours=24   某站点未来若干小时的逐时信号
- GET  /v1/events  POST /v1/events  DELETE /v1/events   需求响应事件的查询、注入与清除
- GET  /metrics                      Prometheus 指标（cpc_grid_*）
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from prometheus_client import CollectorRegistry, Gauge

from ..common import metrics_response
from ..config import CST, DREvent, Scenario
from ..models import SimClock
from .signals import GridModel

PERIOD_CODE = {"deep_valley": 0, "valley": 1, "flat": 2, "peak": 3, "sharp": 4, "trace": -1}


def create_app(scn: Scenario, clock: SimClock | None = None) -> FastAPI:
    model = GridModel(scn)
    clock = clock or SimClock(start=scn.clock.start, speedup=scn.clock.speedup)
    reg = CollectorRegistry()
    g_price = Gauge("cpc_grid_price_yuan_per_kwh", "分时电价（元/kWh）", ["site", "province"], registry=reg)
    g_period = Gauge("cpc_grid_price_period", "电价时段编码：0深谷 1低谷 2平 3峰 4尖峰", ["site"], registry=reg)
    g_carbon = Gauge("cpc_grid_carbon_kg_per_kwh", "电力碳排放因子（kgCO2/kWh）", ["site", "province"], registry=reg)
    g_cap = Gauge("cpc_grid_power_cap_kw", "站点设施功率上限（kW，无上限时为 -1）", ["site"], registry=reg)
    g_dr = Gauge("cpc_grid_dr_event_active", "需求响应事件是否生效", ["site"], registry=reg)
    g_pue = Gauge("cpc_site_pue", "站点 PUE", ["site"], registry=reg)
    g_eff = Gauge("cpc_grid_effective_price_yuan_per_kwh", "计入碳价后的等效电价（元/kWh）", ["site"], registry=reg)
    g_hour = Gauge("cpc_grid_signal_hour", "信号时钟的小时数（含小数）", registry=reg)

    def refresh() -> None:
        now = clock.now()
        g_hour.set(now.astimezone(CST).hour + now.astimezone(CST).minute / 60)
        for sid, s in model.snapshot(now).items():
            g_price.labels(sid, s.province).set(s.price_yuan_per_kwh)
            g_period.labels(sid).set(PERIOD_CODE.get(s.period, -1))
            g_carbon.labels(sid, s.province).set(s.carbon_kg_per_kwh)
            g_cap.labels(sid).set(-1 if s.power_cap_kw is None else s.power_cap_kw)
            g_dr.labels(sid).set(1 if s.dr_event else 0)
            g_pue.labels(sid).set(s.pue)
            g_eff.labels(sid).set(s.effective_price)

    async def ticker() -> None:
        while True:
            refresh()
            await asyncio.sleep(1.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    app = FastAPI(title="cpc grid-signal service", lifespan=lifespan)
    app.state.model = model
    app.state.clock = clock

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/v1/clock")
    def get_clock() -> dict:
        return clock.to_dict()

    @app.get("/v1/signals")
    def signals(at: datetime | None = None) -> dict:
        t = at or clock.now()
        if t.tzinfo is None:
            t = t.replace(tzinfo=CST)
        return {"time": t.isoformat(), "signals": {k: v.model_dump(mode="json") for k, v in model.snapshot(t).items()}}

    @app.get("/v1/forecast")
    def forecast(site: str, hours: float = 24, step_min: int = 60) -> dict:
        try:
            scn.site(site)
        except KeyError:
            raise HTTPException(404, f"unknown site {site}")
        t0 = clock.now().replace(minute=0, second=0, microsecond=0)
        return {"site": site, "points": [s.model_dump(mode="json") for s in model.forecast(site, t0, hours, step_min)]}

    @app.get("/v1/events")
    def list_events() -> list[dict]:
        return [e.model_dump(mode="json") for e in model.events]

    @app.post("/v1/events")
    def add_event(body: dict) -> dict:
        """注入需求响应事件。可给出 start/end（ISO），或给出 duration_min（从当前信号时间开始）。"""
        now = clock.now()
        if "start" not in body:
            body["start"] = now.isoformat()
        if "end" not in body:
            body["end"] = (datetime.fromisoformat(body["start"]) + timedelta(minutes=float(body.pop("duration_min", 60)))).isoformat()
        try:
            ev = DREvent.model_validate(body)
            model.add_event(ev)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))
        refresh()
        return ev.model_dump(mode="json")

    @app.delete("/v1/events")
    def clear_events(site: str | None = None) -> dict:
        model.clear_events(site)
        refresh()
        return {"remaining": len(model.events)}

    @app.get("/metrics")
    def metrics():
        refresh()
        return metrics_response(reg)

    return app
