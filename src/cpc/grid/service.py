"""电网信号服务（grid）。

对外提供：
- GET  /v1/clock                     信号时钟（其他服务据此同步）
- GET  /v1/signals[?at=ISO时间]       各站点当前（或指定时刻）的电价、碳强度、功率上限；信号不可用的站点列入 missing
- GET  /v1/events  POST /v1/events  DELETE /v1/events   需求响应事件的查询、注入与清除
- GET  /healthz、/readyz              存活检查；就绪检查（数据加载与校验在启动时完成，服务运行即就绪）
- GET  /metrics                      Prometheus 指标（cpc_grid_*）

启动检查：电价、碳强度与扩展信号的数据集符合规范，收录场景中全部站点的区域，覆盖场景时钟起点。任一不满足即启动失败。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException
from prometheus_client import CollectorRegistry, Gauge

from ..common import metrics_response, require_admin
from ..config import DREvent, Scenario
from ..models import SimClock
from .signals import GridModel

def create_app(scn: Scenario, clock: SimClock | None = None) -> FastAPI:
    model = GridModel(scn)
    clock = clock or SimClock(start=scn.clock.start, speedup=scn.clock.speedup)
    reg = CollectorRegistry()
    cur = model.currency
    g_price = Gauge("cpc_grid_price_per_kwh", f"电价（{cur}/kWh）", ["site", "region", "currency"], registry=reg)
    g_carbon = Gauge("cpc_grid_carbon_intensity_kg_per_kwh", "电力碳强度（kgCO2/kWh）", ["site", "region"], registry=reg)
    g_extra = Gauge("cpc_grid_extra_signal", "扩展信号（单位见数据集描述）", ["site", "signal"], registry=reg)
    g_cap = Gauge("cpc_grid_power_cap_kw", "站点设施功率上限（kW，无上限时为 -1）", ["site"], registry=reg)
    g_dr = Gauge("cpc_grid_dr_event_active", "需求响应事件是否生效", ["site"], registry=reg)
    g_eff = Gauge("cpc_grid_effective_price_per_kwh", f"计入碳价后的等效电价（{cur}/kWh）", ["site", "currency"], registry=reg)
    g_avail = Gauge("cpc_grid_signal_available", "站点当前信号是否可用（1 可用，0 数据缺失）", ["site"], registry=reg)
    g_time = Gauge("cpc_grid_signal_time_seconds", "信号时钟（Unix 时间）", registry=reg)
    nan = float("nan")

    def refresh() -> None:
        now = clock.now()
        g_time.set(now.timestamp())
        signals, missing = model.snapshot(now)
        for sid in missing:
            g_avail.labels(sid).set(0)
            g_price.labels(sid, scn.site(sid).region, cur).set(nan)
            g_carbon.labels(sid, scn.site(sid).region).set(nan)
            g_eff.labels(sid, cur).set(nan)
        for sid, s in signals.items():
            region = scn.site(sid).region
            g_avail.labels(sid).set(1)
            g_price.labels(sid, region, cur).set(s.price)
            g_carbon.labels(sid, region).set(s.carbon_intensity)
            for k, v in s.extra.items():
                g_extra.labels(sid, k).set(nan if v is None else v)
            g_cap.labels(sid).set(-1 if s.power_cap_kw is None else s.power_cap_kw)
            g_dr.labels(sid).set(1 if s.dr_event else 0)
            g_eff.labels(sid, cur).set(s.effective_price)

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
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz() -> dict:
        return {"ready": True, "sites": len(scn.sites), "events": len(model.events), "sources": model.sources()}

    @app.get("/v1/clock")
    def get_clock() -> dict:
        return clock.to_dict()

    @app.get("/v1/signals")
    def signals(at: datetime | None = None) -> dict:
        t = at or clock.now()
        if t.tzinfo is None:
            raise HTTPException(400, "参数 at 须带时区（RFC 3339）")
        signals, missing = model.snapshot(t)
        return {"time": t.isoformat(), "signals": {k: v.model_dump(mode="json") for k, v in signals.items()},
                "missing": missing, "sources": model.sources()}

    @app.get("/v1/events")
    def list_events() -> list[dict]:
        return [e.model_dump(mode="json") for e in model.events]

    @app.post("/v1/events", dependencies=[Depends(require_admin)])
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
