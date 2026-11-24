"""站点代理（site agent）服务。每个（虚拟）数据中心运行一个。

接口：
- POST /v1/chat/completions, /v1/completions   OpenAI 兼容推理接口（支持 stream）
- GET  /v1/models
- GET  /state                                   站点状态快照（SiteSnapshot）
- POST /admin/capacity                          容量指令（CapacityTarget）
- GET  /metrics                                 Prometheus 指标：
      DCGM_FI_DEV_POWER_USAGE / DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION / DCGM_FI_DEV_GPU_UTIL
      （与 NVIDIA dcgm-exporter 同名，便于与真实环境的仪表盘、规则通用）以及 cpc_site_* / cpc_node_* 指标。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ..common import metrics_response, require_admin
from ..config import Scenario
from ..schemas import CapacityTarget
from .core import Overloaded, SiteCore
from .executors import EmulatedExecutor, ExecResult, OpenAIExecutor, RealNodeHooks
from .power import ModelPower, SensorPower, node_devices

log = logging.getLogger("cpc.site")

STATE_CODE = {"off": 0, "booting": 1, "on": 2, "draining": 3}


def build_power(cfg, prof, power: str):
    """真实后端的功率来源：场景中配置的传感器，或自动探测本机传感器；都没有时退回功率模型。"""
    if power == "model":
        return ModelPower(prof)
    from ..sensors import autodetect, make_sensor

    sensors = [make_sensor(s) for s in cfg.sensors] if cfg.sensors else autodetect()
    if not sensors:
        if power == "sensors":
            raise RuntimeError("未找到可用的功率传感器")
        log.warning("站点 %s 未找到功率传感器，改用功率模型", cfg.id)
        return ModelPower(prof)
    log.info("站点 %s 功率传感器：%s", cfg.id, [s.name for s in sensors])
    return SensorPower(prof, sensors)


def build_core(scn: Scenario, site_id: str, power: str = "auto", enable_power_cap: bool = False, initial_active: int | None = None):
    cfg = scn.site(site_id)
    prof = scn.profile_of(site_id)
    if cfg.backend == "openai":
        psrc = build_power(cfg, prof, power)
        hooks = RealNodeHooks(power=psrc, enable_power_cap=enable_power_cap)
        executor = OpenAIExecutor()
    else:
        psrc = ModelPower(prof)
        hooks = None
        executor = EmulatedExecutor(prof)
    core = SiteCore(cfg, prof, psrc, hooks, scn.scheduler.queue_limit_per_slot, initial_active)
    return core, executor


def create_app(scn: Scenario, site_id: str, tick_s: float = 0.5, **kw) -> FastAPI:
    core, executor = build_core(scn, site_id, **kw)
    cfg = core.cfg
    reg = CollectorRegistry()
    L = ["site"]
    gl = ["gpu", "UUID", "modelName", "Hostname", "site"]
    m_gpu_p = Gauge("DCGM_FI_DEV_POWER_USAGE", "GPU power draw (W)", gl, registry=reg)
    m_gpu_e = Gauge("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "GPU energy since start (mJ)", gl, registry=reg)
    m_gpu_u = Gauge("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (%)", gl, registry=reg)
    m_node_state = Gauge("cpc_node_state", "节点状态：0关 1启动中 2在线 3排空", ["site", "node"], registry=reg)
    m_node_it = Gauge("cpc_node_it_power_watts", "节点 IT 功率（W）", ["site", "node", "source"], registry=reg)
    m_node_host = Gauge("cpc_node_host_power_watts", "节点主机部分功率（W）", ["site", "node"], registry=reg)
    m_node_busy = Gauge("cpc_node_busy_slots", "节点在服务请求数", ["site", "node"], registry=reg)
    m_node_cap = Gauge("cpc_node_power_cap_ratio", "GPU 功率封顶比例", ["site", "node"], registry=reg)
    m_node_e = Gauge("cpc_node_energy_joules_total", "节点累计 IT 能耗（J）", ["site", "node"], registry=reg)
    m_queue = Gauge("cpc_site_queue_length", "排队请求数", L, registry=reg)
    m_busy = Gauge("cpc_site_busy_slots", "在服务请求数", L, registry=reg)
    m_slots = Gauge("cpc_site_slots_on", "在线并发槽数", L, registry=reg)
    m_nodes_on = Gauge("cpc_site_nodes_on", "在线节点数", L, registry=reg)
    m_nodes_total = Gauge("cpc_site_nodes_total", "节点总数", L, registry=reg)
    m_it = Gauge("cpc_site_it_power_watts", "站点 IT 功率（W）", L, registry=reg)
    m_fac = Gauge("cpc_site_facility_power_watts", "站点设施功率（W）= IT × PUE", L, registry=reg)
    m_pue = Gauge("cpc_site_pue_ratio", "站点 PUE", L, registry=reg)
    m_req = Counter("cpc_site_requests", "站点处理的请求", ["site", "status"], registry=reg)
    m_tok = Counter("cpc_site_tokens", "站点处理的 token", ["site", "kind"], registry=reg)
    m_wait = Histogram("cpc_site_queue_wait_seconds", "站内排队时间", L, registry=reg,
                       buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
    m_svc = Histogram("cpc_site_service_seconds", "站内服务时间", L, registry=reg,
                      buckets=(0.5, 1, 2, 4, 8, 16, 32, 64, 128))

    def export() -> None:
        snap_nodes = core.nodes
        for n in snap_nodes:
            m_node_state.labels(cfg.id, n.id).set(STATE_CODE[n.state])
            m_node_it.labels(cfg.id, n.id, n.power_source).set(n.it_power_w)
            m_node_host.labels(cfg.id, n.id).set(n.it_power_w - n.gpu_power_w)
            m_node_busy.labels(cfg.id, n.id).set(n.busy)
            m_node_cap.labels(cfg.id, n.id).set(n.cap_ratio)
            m_node_e.labels(cfg.id, n.id).set(n.energy_j)
            gidx = [d.removeprefix("gpu") for d in node_devices(n)] or [str(i) for i in range(n.gpus)]
            for k, (p, u) in enumerate(zip(n.gpu_powers_w, n.gpu_utils)):
                g = gidx[k] if k < len(gidx) else str(k)
                model = n.gpu_models[k] if k < len(n.gpu_models) else core.profile.gpu_model
                lbl = (g, f"GPU-{cfg.id}-{n.id}-{g}", model, n.id, cfg.id)
                m_gpu_p.labels(*lbl).set(p)
                m_gpu_u.labels(*lbl).set(u)
                if k < len(n.gpu_energy_mj):
                    m_gpu_e.labels(*lbl).set(n.gpu_energy_mj[k])
        s = core.snapshot()
        m_queue.labels(cfg.id).set(s.queue_len)
        m_busy.labels(cfg.id).set(s.busy)
        m_slots.labels(cfg.id).set(s.slots_on)
        m_nodes_on.labels(cfg.id).set(s.nodes_on)
        m_nodes_total.labels(cfg.id).set(s.nodes_total)
        m_it.labels(cfg.id).set(s.it_power_w)
        m_fac.labels(cfg.id).set(s.facility_power_w)
        m_pue.labels(cfg.id).set(cfg.pue)

    async def ticker() -> None:
        while True:
            try:
                await core.tick()
                export()
            except Exception:  # noqa: BLE001
                log.exception("site tick failed")
            await asyncio.sleep(tick_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    app = FastAPI(title=f"cpc site agent [{cfg.id}]", lifespan=lifespan)
    app.state.core = core

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "site": cfg.id}

    @app.get("/state")
    def state():
        return core.snapshot().model_dump()

    @app.post("/admin/capacity", dependencies=[Depends(require_admin)])
    async def capacity(target: CapacityTarget):
        # 必须是 async：apply() 会在事件循环中创建开关机任务
        core.apply(target)
        snap = core.snapshot()
        log.info("capacity target %s -> on=%d booting=%d draining=%d", target.model_dump(), snap.nodes_on, snap.nodes_booting, snap.nodes_draining)
        return snap.model_dump()

    @app.get("/v1/models")
    async def models():
        if isinstance(executor, OpenAIExecutor):
            return await executor.models(core.nodes)
        return await executor.models()

    async def handle(request: Request, path: str):
        body = await request.json()
        t0 = time.monotonic()
        try:
            node = await core.acquire()
        except Overloaded:
            m_req.labels(cfg.id, "rejected").inc()
            return JSONResponse({"error": {"message": f"site {cfg.id} overloaded", "type": "overloaded"}}, status_code=503,
                                headers={"x-cpc-site": cfg.id})
        wait = time.monotonic() - t0
        m_wait.labels(cfg.id).observe(wait)
        hdr = {"x-cpc-site": cfg.id, "x-cpc-node": node.id, "x-cpc-queue-s": f"{wait:.4f}"}
        if body.get("stream"):
            res = ExecResult(200, None)

            async def gen():
                t1 = time.monotonic()
                try:
                    async for chunk in executor.stream(node, path, body, res):
                        yield chunk
                finally:
                    svc = time.monotonic() - t1
                    core.release(node, svc)
                    m_svc.labels(cfg.id).observe(svc)
                    m_req.labels(cfg.id, "ok" if res.status == 200 else "error").inc()
                    m_tok.labels(cfg.id, "prompt").inc(res.prompt_tokens)
                    m_tok.labels(cfg.id, "completion").inc(res.completion_tokens)

            return StreamingResponse(gen(), media_type="text/event-stream", headers=hdr)
        t1 = time.monotonic()
        p_node = node.it_power_w
        share = max(node.busy, 1)
        try:
            res = await executor.complete(node, path, body)
        finally:
            svc = time.monotonic() - t1
            core.release(node, svc)
        m_svc.labels(cfg.id).observe(svc)
        m_req.labels(cfg.id, "ok" if res.status == 200 else "error").inc()
        m_tok.labels(cfg.id, "prompt").inc(res.prompt_tokens)
        m_tok.labels(cfg.id, "completion").inc(res.completion_tokens)
        # 单请求能耗的粗略估计：开始时节点功率按在途请求数均摊 × 服务时间（精确核算见 cpc energy-report）
        hdr["x-cpc-energy-j"] = f"{p_node / share * svc * cfg.pue:.2f}"
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
