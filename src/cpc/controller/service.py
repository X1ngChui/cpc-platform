"""节点容量管理控制器（controller）。

每个控制周期：
  1. 采集：各站点状态（/state）、电网信号（grid /v1/signals）、网关需求统计（gateway /state）；
  2. 决策：调用容量策略 decide(view) → 各站点目标在线节点数、功率封顶比例、（可选）路由权重；
  3. 执行：通过执行器下发到站点代理（或 Kubernetes），把路由权重下发到网关；
  4. 记录：决策日志（JSONL）与 Prometheus 指标。

接口：GET /state（最近一次决策）、PUT /admin/policy、POST /admin/step（立即决策一次）、GET /metrics。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from prometheus_client import CollectorRegistry, Counter, Gauge

from ..common import JsonlWriter, metrics_response
from ..config import Endpoints, Scenario
from ..grid.signals import GridSignal
from ..models import SimClock, rtt_matrix
from ..schemas import DemandSnapshot, SiteSnapshot
from ..sched import ControlView, make_capacity
from .actuators import KubernetesActuator, SiteAgentActuator

log = logging.getLogger("cpc.controller")


def create_app(scn: Scenario, ep: Endpoints, policy: str | None = None, params: dict | None = None,
               interval_s: float | None = None, log_path: str | None = None, actuator: str = "site-agent",
               k8s_namespace: str = "cpc", k8s_template: str = "vllm-{site}") -> FastAPI:
    pol = {"policy": make_capacity(policy or scn.scheduler.capacity.policy, params if policy else scn.scheduler.capacity.params)}
    interval = interval_s or scn.scheduler.control_interval_s
    client = httpx.AsyncClient(timeout=5.0)
    act = KubernetesActuator(k8s_namespace, k8s_template) if actuator == "kubernetes" else SiteAgentActuator(ep.sites, client)
    writer = JsonlWriter(log_path)
    rtt = rtt_matrix(scn)
    min_active = {s.id: s.min_active for s in scn.sites}
    last: dict = {"plan": None, "time": None}
    clock_box = {"clock": SimClock(start=scn.clock.start, speedup=scn.clock.speedup), "synced": False}

    reg = CollectorRegistry()
    g_nodes = Gauge("cpc_controller_target_nodes", "目标在线节点数", ["site"], registry=reg)
    g_cap = Gauge("cpc_controller_target_cap_ratio", "目标功率封顶比例", ["site"], registry=reg)
    c_dec = Counter("cpc_controller_decisions", "决策次数", ["policy"], registry=reg)
    c_err = Counter("cpc_controller_errors", "执行失败次数", ["site"], registry=reg)
    g_pol = Gauge("cpc_controller_policy_info", "当前容量策略", ["policy"], registry=reg)

    async def collect() -> ControlView:
        async def get(url: str):
            r = await client.get(url, timeout=3.0)
            r.raise_for_status()
            return r.json()

        if not clock_box["synced"]:
            try:
                clock_box["clock"] = SimClock.from_dict(await get(ep.grid + "/v1/clock"))
                clock_box["synced"] = True
            except Exception:  # noqa: BLE001
                pass
        sites: dict[str, SiteSnapshot] = {}
        res = await asyncio.gather(*(get(u + "/state") for u in ep.sites.values()), return_exceptions=True)
        for sid, r in zip(ep.sites, res):
            if isinstance(r, Exception):
                log.warning("site %s unreachable: %s", sid, r)
                continue
            sites[sid] = SiteSnapshot.model_validate(r)
        signals: dict[str, GridSignal] = {}
        try:
            signals = {k: GridSignal.model_validate(v) for k, v in (await get(ep.grid + "/v1/signals"))["signals"].items()}
        except Exception as e:  # noqa: BLE001
            log.warning("grid unreachable: %s", e)
        demand = DemandSnapshot()
        try:
            d = await get(ep.gateway + "/state")
            demand = DemandSnapshot.model_validate(d)
        except Exception as e:  # noqa: BLE001
            log.debug("gateway unreachable: %s", e)
        return ControlView(now=clock_box["clock"].now(), interval_s=interval, sites=sites, signals=signals,
                           demand=demand, rtt_ms=rtt, slo=scn.slo, min_active=min_active)

    async def step() -> dict:
        view = await collect()
        if not view.sites:
            return {"skipped": "no site reachable"}
        plan = pol["policy"].decide(view)
        c_dec.labels(pol["policy"].name).inc()
        results = {}
        for sid, tgt in plan.targets.items():
            ok = await act.apply(sid, tgt)
            results[sid] = ok
            if not ok:
                c_err.labels(sid).inc()
            if tgt.active_nodes is not None:
                g_nodes.labels(sid).set(tgt.active_nodes)
            if tgt.cap_ratio is not None:
                g_cap.labels(sid).set(tgt.cap_ratio)
        if plan.weights is not None:
            try:
                await client.put(ep.gateway + "/admin/weights", json={"weights": plan.weights})
            except httpx.HTTPError as e:
                log.warning("push weights failed: %s", e)
        rec = {
            "t_wall": time.time(),
            "t_signal": view.now.isoformat(),
            "policy": pol["policy"].name,
            "targets": {k: v.model_dump() for k, v in plan.targets.items()},
            "weights": plan.weights,
            "notes": plan.notes,
            "applied": results,
            "observed": {k: {"nodes_on": s.nodes_on, "busy": s.busy, "queue": s.queue_len, "it_kw": round(s.it_power_w / 1000, 3),
                             "arrival_rate": s.arrival_rate} for k, s in view.sites.items()},
            "signals": {k: {"price": g.price_yuan_per_kwh, "period": g.period, "carbon": g.carbon_kg_per_kwh, "cap_kw": g.power_cap_kw}
                        for k, g in view.signals.items()},
            "demand": view.demand.rates,
        }
        writer.write(rec)
        last["plan"], last["time"] = rec, time.time()
        return rec

    async def loop() -> None:
        await asyncio.sleep(min(3.0, interval))  # 等待其他服务就绪
        while True:
            try:
                await step()
            except Exception:  # noqa: BLE001
                log.exception("control step failed")
            await asyncio.sleep(interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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
        return {"ok": True}

    @app.get("/state")
    def state() -> dict:
        return {"policy": pol["policy"].name, "interval_s": interval, "actuator": act.name, "last": last["plan"]}

    @app.post("/admin/step")
    async def force_step() -> dict:
        return await step()

    @app.put("/admin/policy")
    def set_policy(body: dict) -> dict:
        try:
            pol["policy"] = make_capacity(body["policy"], body.get("params"))
        except KeyError as e:
            raise HTTPException(400, str(e))
        g_pol.clear()
        g_pol.labels(pol["policy"].name).set(1)
        log.info("capacity policy -> %s %s", body["policy"], body.get("params"))
        return {"policy": pol["policy"].name}

    @app.get("/metrics")
    def metrics():
        return metrics_response(reg)

    return app
