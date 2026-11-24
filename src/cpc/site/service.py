"""站点代理（site agent）服务。每个（虚拟）数据中心运行一个。

接口：
- POST /v1/chat/completions, /v1/completions   OpenAI 兼容推理接口（支持 stream）
- GET  /v1/models
- GET  /state                                   站点状态快照（SiteSnapshot），含功率缺失项与传感器健康状态
- POST /admin/capacity                          容量指令（CapacityTarget）
- GET  /healthz                                 存活检查（进程与事件循环可响应）
- GET  /readyz                                  就绪检查（启动检查已通过，状态采样正常运行）
- GET  /metrics                                 Prometheus 指标：
      DCGM_FI_DEV_POWER_USAGE / DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION / DCGM_FI_DEV_GPU_UTIL
      （与 NVIDIA dcgm-exporter 同名）、cpc_site_* / cpc_node_* 指标、cpc_sensor_up / cpc_sensor_errors_total。
      读数缺失的功率类指标取 NaN。

启动检查（失败则进程退出）：真实后端的功率传感器可用且覆盖节点声明的加速卡；配置的容器可访问；开启功率封顶时传感器支持封顶。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ..common import metrics_response, require_admin
from ..config import Scenario
from ..schemas import CapacityTarget
from .core import Overloaded, SiteCore
from .executors import EmulatedExecutor, ExecResult, InvalidRequest, OpenAIExecutor, RealNodeHooks
from .power import ModelPower, SensorPower, node_devices

log = logging.getLogger("cpc.site")

STATE_CODE = {"off": 0, "booting": 1, "on": 2, "draining": 3}
NAN = float("nan")


def _v(x: float | None) -> float:
    return NAN if x is None else x


def build_core(scn: Scenario, site_id: str, registry: CollectorRegistry | None = None, enable_power_cap: bool = False,
               initial_active: int | None = None):
    """按站点配置构造功率来源、节点钩子与执行器。仿真后端使用功率模型；真实后端使用 power 中声明的传感器。"""
    cfg = scn.site(site_id)
    prof = scn.profile_of(site_id)
    if cfg.backend == "openai":
        assert cfg.power is not None  # 由场景校验保证
        psrc = SensorPower(cfg.id, cfg.power, registry)
        core = SiteCore(cfg, prof, psrc, None, scn.scheduler.queue_limit_per_slot, initial_active)
        core.hooks = RealNodeHooks(cfg.upstreams, power=psrc, enable_power_cap=enable_power_cap)
        executor = OpenAIExecutor()
    else:
        if enable_power_cap:
            raise ValueError(f"站点 {cfg.id} 为仿真后端，不支持硬件功率封顶")
        core = SiteCore(cfg, prof, ModelPower(prof), None, scn.scheduler.queue_limit_per_slot, initial_active)
        executor = EmulatedExecutor(prof)
    return core, executor


def create_app(scn: Scenario, site_id: str, tick_s: float = 0.5, enable_power_cap: bool = False,
               initial_active: int | None = None) -> FastAPI:
    reg = CollectorRegistry()
    core, executor = build_core(scn, site_id, reg, enable_power_cap, initial_active)
    cfg = core.cfg
    L = ["site"]
    gl = ["gpu", "UUID", "modelName", "Hostname", "site"]
    m_gpu_p = Gauge("DCGM_FI_DEV_POWER_USAGE", "GPU power draw (W)", gl, registry=reg)
    m_gpu_e = Gauge("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "GPU energy since start (mJ)", gl, registry=reg)
    m_gpu_u = Gauge("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (%)", gl, registry=reg)
    m_node_state = Gauge("cpc_node_state", "节点状态：0关 1启动中 2在线 3排空", ["site", "node"], registry=reg)
    m_node_it = Gauge("cpc_node_it_power_watts", "节点 IT 功率（W；缺失为 NaN）", ["site", "node", "source"], registry=reg)
    m_node_host = Gauge("cpc_node_host_power_watts", "节点主机部分功率（W；缺失为 NaN）", ["site", "node"], registry=reg)
    m_node_valid = Gauge("cpc_node_power_valid", "节点功率读数是否完整（1 完整，0 有缺失）", ["site", "node"], registry=reg)
    m_node_busy = Gauge("cpc_node_busy_slots", "节点在服务请求数", ["site", "node"], registry=reg)
    m_node_cap = Gauge("cpc_node_power_cap_ratio", "GPU 功率封顶比例", ["site", "node"], registry=reg)
    m_node_e = Gauge("cpc_node_energy_joules_total", "节点累计 IT 能耗（J，功率有效区间内积分）", ["site", "node"], registry=reg)
    m_node_miss = Gauge("cpc_node_energy_missing_seconds_total", "节点功率缺失的累计时长（s）", ["site", "node"], registry=reg)
    m_act_err = Gauge("cpc_node_actuation_errors_total", "节点开关机与功率封顶操作的失败次数", ["site", "node", "action"], registry=reg)
    m_queue = Gauge("cpc_site_queue_length", "排队请求数", L, registry=reg)
    m_busy = Gauge("cpc_site_busy_slots", "在服务请求数", L, registry=reg)
    m_slots = Gauge("cpc_site_slots_on", "在线并发槽数", L, registry=reg)
    m_nodes_on = Gauge("cpc_site_nodes_on", "在线节点数", L, registry=reg)
    m_nodes_total = Gauge("cpc_site_nodes_total", "节点总数", L, registry=reg)
    m_it = Gauge("cpc_site_it_power_watts", "站点 IT 功率（W；任一节点缺失时为 NaN）", L, registry=reg)
    m_fac = Gauge("cpc_site_facility_power_watts", "站点设施功率（W）= IT × PUE；缺失时为 NaN", L, registry=reg)
    m_pue = Gauge("cpc_site_pue_ratio", "站点 PUE", L, registry=reg)
    m_cov = Gauge("cpc_site_energy_coverage_ratio", "功率有效时长占比（节点·秒加权）", L, registry=reg)
    m_tick_err = Counter("cpc_site_tick_errors", "站点周期任务（状态推进、功率采样）失败次数", L, registry=reg)
    m_req = Counter("cpc_site_requests", "站点处理的请求", ["site", "status"], registry=reg)
    m_tok = Counter("cpc_site_tokens", "站点处理的 token", ["site", "kind"], registry=reg)
    m_wait = Histogram("cpc_site_queue_wait_seconds", "站内排队时间", L, registry=reg,
                       buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30))
    m_svc = Histogram("cpc_site_service_seconds", "站内服务时间", L, registry=reg,
                      buckets=(0.5, 1, 2, 4, 8, 16, 32, 64, 128))
    status = {"last_tick": None, "last_tick_error": None}

    def export() -> None:
        for n in core.nodes:
            m_node_state.labels(cfg.id, n.id).set(STATE_CODE[n.state])
            m_node_it.labels(cfg.id, n.id, n.power_source).set(_v(n.it_power_w))
            host = None if n.it_power_w is None or n.gpu_power_w is None else n.it_power_w - n.gpu_power_w
            m_node_host.labels(cfg.id, n.id).set(_v(host))
            m_node_valid.labels(cfg.id, n.id).set(0 if n.power_missing else 1)
            m_node_busy.labels(cfg.id, n.id).set(n.busy)
            m_node_cap.labels(cfg.id, n.id).set(n.cap_ratio)
            m_node_e.labels(cfg.id, n.id).set(n.energy_j)
            m_node_miss.labels(cfg.id, n.id).set(n.energy_missing_s)
            gidx = [d.removeprefix("gpu") for d in node_devices(n)] or [str(i) for i in range(n.gpus)]
            for k, (p, u) in enumerate(zip(n.gpu_powers_w, n.gpu_utils)):
                g = gidx[k]
                lbl = (g, f"GPU-{cfg.id}-{n.id}-{g}", n.gpu_models[k], n.id, cfg.id)
                m_gpu_p.labels(*lbl).set(_v(p))
                m_gpu_u.labels(*lbl).set(_v(u))
                m_gpu_e.labels(*lbl).set(_v(n.gpu_energy_mj[k]))
        for (node, action), c in core.actuation_errors.items():
            m_act_err.labels(cfg.id, node, action).set(c)
        s = core.snapshot()
        m_queue.labels(cfg.id).set(s.queue_len)
        m_busy.labels(cfg.id).set(s.busy)
        m_slots.labels(cfg.id).set(s.slots_on)
        m_nodes_on.labels(cfg.id).set(s.nodes_on)
        m_nodes_total.labels(cfg.id).set(s.nodes_total)
        m_it.labels(cfg.id).set(_v(s.it_power_w))
        m_fac.labels(cfg.id).set(_v(s.facility_power_w))
        m_pue.labels(cfg.id).set(cfg.pue)
        m_cov.labels(cfg.id).set(_v(s.energy_coverage))

    async def ticker() -> None:
        while True:
            try:
                await core.tick()
                export()
                status["last_tick"] = time.time()
            except Exception as e:  # noqa: BLE001 —— 单次周期失败不终止服务；计数并在 /readyz 中报告
                m_tick_err.labels(cfg.id).inc()
                status["last_tick_error"] = repr(e)
                log.exception("站点 %s 周期任务失败", cfg.id, extra={"event": "site_tick_failed", "site": cfg.id})
            await asyncio.sleep(tick_s)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    app = FastAPI(title=f"cpc site agent [{cfg.id}]", lifespan=lifespan)
    app.state.core = core
    max_tick_age = max(5.0, 10 * tick_s)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "alive", "site": cfg.id}

    @app.get("/readyz")
    def readyz():
        age = None if status["last_tick"] is None else time.time() - status["last_tick"]
        ready = age is not None and age <= max_tick_age
        body = {"ready": ready, "site": cfg.id, "last_tick_age_s": None if age is None else round(age, 3),
                "last_tick_error": status["last_tick_error"]}
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/state")
    def state():
        return core.snapshot().model_dump()

    @app.post("/admin/capacity", dependencies=[Depends(require_admin)])
    async def capacity(target: CapacityTarget):
        # 必须是 async：apply() 会在事件循环中创建开关机任务
        core.apply(target)
        snap = core.snapshot()
        log.info("capacity target %s -> on=%d booting=%d draining=%d", target.model_dump(), snap.nodes_on, snap.nodes_booting,
                 snap.nodes_draining, extra={"event": "capacity_applied", "site": cfg.id})
        return snap.model_dump()

    @app.get("/v1/models")
    async def models():
        if isinstance(executor, OpenAIExecutor):
            code, body = await executor.models(core.nodes)
            return JSONResponse(body, status_code=code)
        return await executor.models()

    async def handle(request: Request, path: str):
        body = await request.json()
        try:
            executor.validate(body)
        except InvalidRequest as e:
            m_req.labels(cfg.id, "invalid").inc()
            return JSONResponse({"error": {"message": str(e), "type": "invalid_request_error"}}, status_code=400,
                                headers={"x-cpc-site": cfg.id})
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
        # 单请求能耗的粗略估计（请求开始时的节点功率按在途请求数均摊 × 服务时间 × PUE）；节点功率缺失时不给出
        if p_node is not None and not math.isnan(p_node):
            hdr["x-cpc-energy-est-j"] = f"{p_node / share * svc * cfg.pue:.2f}"
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
