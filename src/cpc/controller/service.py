"""节点容量管理控制器（controller）。

每个控制周期：
  1. 采集：各站点状态（/state）、电网信号（grid /v1/signals）、网关需求统计（gateway /state）；
  2. 检查输入完整性：状态不可达的站点排除在本周期之外（记入 excluded）；没有可用站点，或策略声明所需的输入
     （signals / demand）缺失或不完整时，跳过本周期并记录原因，不以缺省输入继续决策；
  3. 决策：调用容量策略 decide(view) → 各站点目标在线节点数、功率封顶比例、（可选）路由权重；
  4. 执行：把数据中心指令下发到各站点代理（POST /admin/capacity），把路由权重下发到网关；
  5. 记录：决策日志（JSONL，含 result = applied / skipped 与原因）与 Prometheus 指标。

启动：须在期限内从电网信号服务同步信号时钟，否则启动失败。

指标：cpc_controller_cycles_total{result,reason}、cpc_controller_collect_errors_total{target}、
cpc_controller_apply_errors_total{site}、cpc_upstream_up{component="controller",upstream}。

接口：GET /state（最近一次周期记录与上游健康状态）、GET /healthz、/readyz、PUT /admin/policy、
POST /admin/step（立即执行一次）、GET /metrics。
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
from ..config import Endpoints, Scenario
from ..grid.signals import GridSignal
from ..health import HealthBoard, describe
from ..models import SimClock, rtt_matrix
from ..schemas import DemandSnapshot, SiteSnapshot
from ..sched import ControlView, make_capacity

log = logging.getLogger("cpc.controller")


class PolicyBody(BaseModel):
    policy: str
    params: dict = Field(default_factory=dict)


def create_app(scn: Scenario, ep: Endpoints, policy: str | None = None, params: dict | None = None,
               interval_s: float | None = None, log_path: str | None = None, startup_timeout_s: float = 30.0, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """transport 用于测试中注入故障（httpx.MockTransport）；缺省为真实网络。"""
    pol = {"policy": make_capacity(policy or scn.scheduler.capacity.policy, params if policy else scn.scheduler.capacity.params)}
    interval = interval_s or scn.scheduler.control_interval_s
    client = httpx.AsyncClient(timeout=5.0, headers=admin_headers(), trust_env=False, transport=transport)  # 内部通信不走系统代理

    async def send(site: str, target) -> tuple[bool, str | None]:
        """向站点代理下发指令；返回（是否成功，失败原因）。"""
        try:
            r = await client.post(ep.sites[site] + "/admin/capacity", json=target.model_dump(), timeout=5.0)
        except httpx.HTTPError as e:
            return False, describe(e)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        return True, None

    writer = JsonlWriter(log_path)
    rtt = rtt_matrix(scn)
    min_active = {s.id: s.min_active for s in scn.sites}
    last: dict = {"record": None}
    clock_box: dict[str, SimClock | None] = {"clock": None}

    reg = CollectorRegistry()
    board = HealthBoard("upstream", reg, {"component": "controller"})
    for name in ("grid", "gateway", *(f"site:{s}" for s in ep.sites)):
        board.register(name)
    g_nodes = Gauge("cpc_controller_target_nodes", "目标在线节点数", ["site"], registry=reg)
    g_cap = Gauge("cpc_controller_target_cap_ratio", "目标功率封顶比例", ["site"], registry=reg)
    c_cycles = Counter("cpc_controller_cycles", "控制周期（result：applied / skipped / error；reason：跳过原因）",
                       ["result", "reason"], registry=reg)
    c_dec = Counter("cpc_controller_decisions", "决策次数", ["policy"], registry=reg)
    c_collect = Counter("cpc_controller_collect_errors", "采集失败次数（target：grid / gateway / site:<id>）", ["target"], registry=reg)
    c_apply = Counter("cpc_controller_apply_errors", "执行失败次数", ["site"], registry=reg)
    c_push = Counter("cpc_controller_weight_push_errors", "路由权重下发失败次数", registry=reg)
    g_pol = Gauge("cpc_controller_policy_info", "当前容量策略", ["policy"], registry=reg)
    g_last = Gauge("cpc_controller_last_cycle_timestamp_seconds", "最近一次控制周期的时间（Unix 秒）", ["result"], registry=reg)

    async def get(url: str):
        r = await client.get(url, timeout=3.0)
        r.raise_for_status()
        return r.json()

    def failed(target: str, e: BaseException) -> str:
        board.fail(target, e)
        c_collect.labels(target).inc()
        return describe(e)

    async def collect() -> tuple[ControlView | None, str | None, dict]:
        """返回 (视图, 跳过原因, 采集信息)。跳过原因为 None 表示输入满足策略要求。"""
        info: dict = {"excluded": {}, "errors": {}}
        sites: dict[str, SiteSnapshot] = {}
        res = await asyncio.gather(*(get(u + "/state") for u in ep.sites.values()), return_exceptions=True)
        for sid, r in zip(ep.sites, res):
            target = f"site:{sid}"
            if isinstance(r, BaseException):
                info["excluded"][sid] = failed(target, r)
                continue
            try:
                sites[sid] = SiteSnapshot.model_validate(r)
                board.ok(target)
            except ValidationError as e:
                info["excluded"][sid] = failed(target, e)
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
            lack = sorted(set(sites) - set(signals))
            if lack:
                info["signals_missing_for"] = lack
                return None, "incomplete_signals", info
        if "demand" in req and demand is None:
            return None, "missing_demand", info
        assert clock_box["clock"] is not None
        view = ControlView(now=clock_box["clock"].now(), interval_s=interval, sites=sites, signals=signals or {},
                           demand=demand, rtt_ms=rtt, slo=scn.slo, min_active=min_active, excluded=info["excluded"])
        return view, None, info

    def record(rec: dict) -> dict:
        writer.write(rec)
        last["record"] = rec
        g_last.labels(rec["result"]).set(rec["t_wall"])
        return rec

    async def step() -> dict:
        view, skip, info = await collect()
        rec = {"t_wall": time.time(), "t_signal": clock_box["clock"].now().isoformat(), "policy": pol["policy"].name,
               "excluded": info["excluded"], "collect_errors": info["errors"]}
        if skip is not None:
            c_cycles.labels("skipped", skip).inc()
            log.warning("控制周期跳过：%s %s", skip, info, extra={"event": "control_cycle_skipped", "reason": skip})
            extra = {k: v for k, v in info.items() if k not in ("excluded", "errors")}
            return record({**rec, "result": "skipped", "reason": skip, **extra})
        plan = pol["policy"].decide(view)
        c_dec.labels(pol["policy"].name).inc()
        applied: dict[str, dict] = {}
        for sid, tgt in plan.targets.items():
            ok, err = await send(sid, tgt)
            applied[sid] = {"ok": ok, "error": err}
            if not ok:
                c_apply.labels(sid).inc()
                log.warning("站点 %s 容量指令执行失败：%s", sid, err, extra={"event": "capacity_apply_failed", "site": sid})
            if tgt.active_nodes is not None:
                g_nodes.labels(sid).set(tgt.active_nodes)
            if tgt.cap_ratio is not None:
                g_cap.labels(sid).set(tgt.cap_ratio)
        weights_pushed = None
        if plan.weights is not None:
            try:
                r = await client.put(ep.gateway + "/admin/weights", json={"weights": plan.weights})
                r.raise_for_status()
                weights_pushed = True
                board.ok("gateway")
            except httpx.HTTPError as e:
                weights_pushed = False
                c_push.inc()
                rec["weights_error"] = describe(e)
                board.fail("gateway", e)
        c_cycles.labels("applied", "").inc()
        rec.update({
            "result": "applied",
            "targets": {k: v.model_dump() for k, v in plan.targets.items()},
            "weights": plan.weights,
            "weights_pushed": weights_pushed,
            "notes": plan.notes,
            "applied": applied,
            "observed": {k: {"nodes_on": s.nodes_on, "busy": s.busy, "queue": s.queue_len,
                             "it_kw": None if s.it_power_w is None else round(s.it_power_w / 1000, 3),
                             "energy_coverage": s.energy_coverage, "arrival_rate": s.arrival_rate}
                         for k, s in view.sites.items()},
            "signals": {k: {"price": g.price, "price_label": g.price_label, "carbon_intensity": g.carbon_intensity, "cap_kw": g.power_cap_kw}
                        for k, g in view.signals.items()},
            "demand": view.demand.rates if view.demand else None,
        })
        return record(rec)

    async def loop() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await step()
            except Exception:  # noqa: BLE001 —— 单个周期的异常不终止控制循环；记录并计数
                c_cycles.labels("error", "exception").inc()
                log.exception("控制周期异常", extra={"event": "control_cycle_error"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        clock_box["clock"] = await sync_clock(client, ep.grid, startup_timeout_s)  # 不可达则启动失败
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
        ready = clock_box["clock"] is not None
        return JSONResponse({"ready": ready, "upstreams": board.snapshot()}, status_code=200 if ready else 503)

    @app.get("/state")
    def state() -> dict:
        return {"policy": pol["policy"].name, "requires": sorted(pol["policy"].requires), "interval_s": interval,
                "upstreams": board.snapshot(), "last": last["record"]}

    @app.post("/admin/step", dependencies=[Depends(require_admin)])
    async def force_step() -> dict:
        return await step()

    @app.put("/admin/policy", dependencies=[Depends(require_admin)])
    def set_policy(body: PolicyBody) -> dict:
        try:
            pol["policy"] = make_capacity(body.policy, body.params)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, str(e))
        g_pol.clear()
        g_pol.labels(pol["policy"].name).set(1)
        log.info("capacity policy -> %s %s", body.policy, body.params, extra={"event": "capacity_policy_changed"})
        return {"policy": pol["policy"].name}

    @app.get("/metrics")
    def metrics():
        return metrics_response(reg)

    return app
