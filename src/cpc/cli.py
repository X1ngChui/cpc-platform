"""命令行入口：python -m cpc <子命令> 或 cpc <子命令>。

服务类：grid / site / gateway / controller / up（本机单进程启动全部服务）/ exporter（通用功率导出器）
工具类（能耗感知）：sensors（列出传感器类型、读取指定传感器）
仿真：sim（单次运行）/ run（按实验描述运行多个变体）/ compare（多组策略对比）/ report（作图）
工具类：loadgen / signals / rtt / event
数据集：data kinds / validate / info / summary / fetch / calibrate（参数为数据集目录或描述文件路径）

服务地址：除 up 外，需要访问其他服务的命令须用 -e / CPC_ENDPOINTS 给出地址文件，或用 --local 使用本机模式地址。
配置或启动检查失败时打印原因并以状态码 2 退出。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .common import StartupError, setup_logging
from .config import ConfigError, Endpoints, Scenario, default_local_endpoints, load_endpoints, load_scenario, _host_port
from .datasets import DatasetError
from .sensors.base import SensorError

log = logging.getLogger("cpc")


def _scn(args) -> Scenario:
    scn = load_scenario(args.scenario)
    apply_workload_overrides(scn, args)
    return scn


def apply_workload_overrides(scn: Scenario, args) -> None:
    """命令行显式覆盖场景的负载变换参数（--start-s、--duration-s、--time-scale、--rate-scale）。"""
    for key in ("start_s", "duration_s", "time_scale", "rate_scale"):
        v = getattr(args, key, None)
        if v is not None:
            setattr(scn.workload, key, v)


def _ep(args, scn: Scenario) -> Endpoints:
    return load_endpoints(getattr(args, "endpoints", None), scn, local=getattr(args, "local", False))


def _serve(app, host: str, port: int) -> None:
    import uvicorn

    from .common import warn_if_open

    warn_if_open(app.title, host)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)


def _bind(args, url: str) -> tuple[str, int]:
    h, p = _host_port(url)
    return (args.host or h, args.port or p)


# ------------------------------------------------------------------ 服务


def cmd_grid(args) -> None:
    from .grid.service import create_app

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    ep = _ep(args, scn)
    _serve(create_app(scn), *_bind(args, ep.grid))


def cmd_site(args) -> None:
    from .site.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    if args.site not in ep.sites:
        raise ConfigError(f"站点 {args.site} 不在场景中，可选：{sorted(ep.sites)}")
    app = create_app(scn, args.site, enable_power_cap=args.enable_power_cap, initial_active=args.initial_active)
    _serve(app, *_bind(args, ep.sites[args.site]))


def cmd_gateway(args) -> None:
    from .gateway.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    params = json.loads(args.params) if args.params else None
    _serve(create_app(scn, ep, args.policy, params, args.log, args.startup_timeout), *_bind(args, ep.gateway))


def cmd_controller(args) -> None:
    from .controller.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    params = json.loads(args.params) if args.params else None
    app = create_app(scn, ep, args.policy, params, args.interval, args.log, args.actuator, args.k8s_namespace, args.k8s_template,
                     args.k8s_auth, args.startup_timeout)
    _serve(app, *_bind(args, ep.controller))


def cmd_up(args) -> None:
    """本机开发模式：在一个进程中启动电网信号服务、全部站点代理、网关和控制器。

    未给出 -e 时使用本机模式地址。任一服务启动失败或退出时，停止其余服务并以非零状态退出。
    """
    import uvicorn

    from .controller.service import create_app as ctl_app
    from .gateway.service import create_app as gw_app
    from .grid.service import create_app as grid_app
    from .site.service import create_app as site_app

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    ep = load_endpoints(args.endpoints, scn) if args.endpoints else default_local_endpoints(scn)
    out = Path(args.log_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    apps = [(grid_app(scn), ep.grid)]
    apps += [(site_app(scn, s.id, initial_active=args.initial_active), ep.sites[s.id]) for s in scn.sites]
    apps.append((gw_app(scn, ep, args.routing, None, str(out / f"gateway-{stamp}.jsonl")), ep.gateway))
    if not args.no_controller:
        apps.append((ctl_app(scn, ep, args.capacity, None, args.interval, str(out / f"controller-{stamp}.jsonl")), ep.controller))

    async def main() -> int:
        servers = []
        for app, url in apps:
            h, p = _host_port(url)
            servers.append(uvicorn.Server(uvicorn.Config(app, host=args.host or h, port=p, log_level="warning", access_log=False)))
        print(f"[cpc up] 场景 {scn.name}：{len(scn.sites)} 个站点，信号时钟加速 ×{scn.clock.speedup}")
        for app, url in apps:
            print(f"  {app.title:<36s} {url}")
        print(f"  日志目录 {out.resolve()}；Ctrl+C 退出")
        tasks = {asyncio.create_task(s.serve()): (s, app.title) for s, (app, _) in zip(servers, apps)}
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        failed = [tasks[t][1] for t in done if not tasks[t][0].started or t.exception() is not None]
        for s, _ in tasks.values():
            s.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        if failed:
            print(f"[cpc up] 服务启动失败或异常退出：{failed}", file=sys.stderr)
            return 1
        return 0

    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


def _sensors_from_args(args):
    """按 --spec（YAML，键 sensors 为传感器列表）或 --types（逗号分隔的类型名）构造传感器；二者须给出其一。"""
    import yaml

    from .sensors import make_sensor

    if args.spec and args.types:
        raise ConfigError("--spec 与 --types 只能给出其一")
    if args.spec:
        with open(args.spec, encoding="utf-8") as f:
            return [make_sensor(s) for s in yaml.safe_load(f)["sensors"]]
    if args.types:
        return [make_sensor(t) for t in args.types.split(",")]
    raise ConfigError("请用 --types 或 --spec 指定传感器（cpc sensors --list 列出全部类型及本机前提条件是否具备）")


def cmd_exporter(args) -> None:
    from .sensors.exporter import serve

    serve(_sensors_from_args(args), args.port, args.host or "0.0.0.0", args.site, args.interval)


def cmd_sensors(args) -> None:
    """列出传感器类型及本机前提条件是否具备；或读取指定传感器并打印读数。"""
    from .health import HealthBoard
    from .sensors import SENSORS
    from .sensors.base import EnergyIntegrator
    from .sensors.exporter import read_all

    if args.list:
        for name, cls in SENSORS.all().items():
            ok = "具备" if cls.detect() else ""
            print(f"{name:<14s} {cls.requires:<40s} {ok}")
        return
    sensors = _sensors_from_args(args)
    print("传感器：", [s.id for s in sensors])
    integ = EnergyIntegrator()
    board = HealthBoard("sensor", None)
    for k in range(args.count):
        rs = integ.update(read_all(sensors, board))
        for name, h in board.items.items():
            if not h.up:
                print(f"  {name} 读取失败：{h.last_error}")
        if args.json:
            print(json.dumps([r.to_dict() for r in rs], ensure_ascii=False))
        else:
            for r in rs:
                pw = "-" if r.power_w is None else f"{r.power_w:8.2f} W"
                en = "-" if r.energy_j is None else f"{r.energy_j:12.1f} J"
                ut = "" if r.util_pct is None else f"{r.util_pct:5.1f} %"
                err = f"  错误 {r.errors}" if r.errors else ""
                print(f"  [{r.sensor}] {r.device:<14s} {r.kind:<8s} {r.vendor:<8s} {r.model[:28]:<28s} {pw:>11s} {en:>15s} {ut}{err}")
        if k + 1 < args.count:
            time.sleep(args.interval)


# ------------------------------------------------------------------ 工具


def cmd_loadgen(args) -> None:
    from .loadgen.client import run

    scn = _scn(args)
    if args.gateway and args.grid:
        gateway, grid = args.gateway, args.grid
    else:
        ep = _ep(args, scn)
        gateway, grid = args.gateway or ep.gateway, args.grid or ep.grid
    s = asyncio.run(run(gateway, scn, args.duration, grid=grid, stream=not args.no_stream, out=args.out, model=args.model))
    print(json.dumps(s, ensure_ascii=False, indent=2))


def _run_variant(scenario: str, name: str, routing: dict | None, capacity: dict | None, workload: dict, hours: float,
                 bin_s: float, out: str) -> dict:
    """运行一个变体（可在子进程中执行）：加载场景、覆盖策略与负载参数、仿真并写出结果数据包。"""
    from .config import PolicyConfig
    from .results import write_run
    from .sched import make_capacity, make_routing
    from .sim.des import Simulator

    scn = load_scenario(scenario)
    for k, v in workload.items():
        if v is not None:
            if k not in type(scn.workload).model_fields:
                raise ConfigError(f"变体 {name}：workload 没有字段 {k}")
            setattr(scn.workload, k, v)
    if routing is not None:
        scn.scheduler.routing = PolicyConfig.model_validate(routing)
    if capacity is not None:
        scn.scheduler.capacity = PolicyConfig.model_validate(capacity)
    r, c = scn.scheduler.routing, scn.scheduler.capacity
    t0 = time.time()
    res = Simulator(scn, make_routing(r.policy, r.params), make_capacity(c.policy, c.params), hours=hours, bin_s=bin_s).run()
    res.summary["wall_s"] = round(time.time() - t0, 2)
    write_run(out, res, scn, {"name": name, "mode": "simulation", "hours": hours, "bin_s": bin_s,
                              "routing": r.model_dump(), "capacity": c.model_dump()})
    return {"variant": name, **res.summary}


def _run_variants(scenario: str, variants: list[dict], hours: float, bin_s: float, out: Path, jobs: int) -> list[dict]:
    from .results import write_comparison

    ctx = multiprocessing.get_context("spawn")  # 子进程不继承父进程的线程与状态
    with ProcessPoolExecutor(max_workers=max(1, min(len(variants), jobs)), mp_context=ctx) as ex:
        futs = [ex.submit(_run_variant, scenario, v["name"], v.get("routing"), v.get("capacity"), v.get("workload", {}),
                          hours, bin_s, str(out / v["name"])) for v in variants]
        rows = [f.result() for f in futs]
    p = write_comparison(out, rows)
    keys = ["variant", "served", "rejected", "slo_attainment", "ttft_p99_s", "facility_kwh", "cost", "carbon_kg"]
    print(" | ".join(keys))
    for r in rows:
        print(" | ".join(f"{r[k]:.4g}" if isinstance(r[k], float) else str(r[k]) for k in keys))
    print(f"结果已写入 {out}/，对比表 {p}")
    return rows


def _overrides(args) -> dict:
    return {k: getattr(args, k, None) for k in ("start_s", "duration_s", "time_scale", "rate_scale")}


def cmd_sim(args) -> None:
    scn = load_scenario(args.scenario)
    routing = {"policy": args.routing, "params": json.loads(args.routing_params or "{}")} if args.routing else None
    capacity = {"policy": args.capacity, "params": json.loads(args.capacity_params or "{}")} if args.capacity else None
    name = f"{args.routing or scn.scheduler.routing.policy}__{args.capacity or scn.scheduler.capacity.policy}"
    out = args.out or f"results/sim-{name}"
    s = _run_variant(args.scenario, name, routing, capacity, _overrides(args), args.hours, args.bin_s, out)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print(f"结果已写入 {out}/")


def cmd_run(args) -> None:
    """按实验描述运行全部变体。"""
    from .results import load_experiment

    exp = load_experiment(args.experiment)
    out = Path(args.out or f"results/{exp.name}")
    variants = [{"name": v.name, "routing": v.routing.model_dump() if v.routing else None,
                 "capacity": v.capacity.model_dump() if v.capacity else None, "workload": v.workload} for v in exp.variants]
    _run_variants(str(exp.scenario_path()), variants, exp.hours, exp.bin_s, out, args.jobs)


def cmd_compare(args) -> None:
    """并行仿真多组"路由:容量"策略组合（策略参数取场景中的配置，策略名不同时为空）。"""
    scn = load_scenario(args.scenario)
    variants = []
    for combo in args.runs.split(","):
        r, c = combo.split(":")
        rp = scn.scheduler.routing.params if r == scn.scheduler.routing.policy else {}
        cp = scn.scheduler.capacity.params if c == scn.scheduler.capacity.policy else {}
        variants.append({"name": f"{r}__{c}", "routing": {"policy": r, "params": rp}, "capacity": {"policy": c, "params": cp},
                         "workload": _overrides(args)})
    _run_variants(args.scenario, variants, args.hours, args.bin_s, Path(args.out), args.jobs)


def cmd_report(args) -> None:
    from .sim.report import plot_comparison, plot_run

    for d in args.dirs:
        p = Path(d)
        if (p / "comparison.csv").exists():
            print(plot_comparison(p))
        if (p / "timeseries.csv").exists():
            print(plot_run(p, load_scenario(args.scenario)))
        for sub in sorted(p.iterdir()) if p.is_dir() else []:
            if (sub / "timeseries.csv").exists():
                print(plot_run(sub, load_scenario(args.scenario)))


def cmd_signals(args) -> None:
    from .grid.signals import GridModel, SignalUnavailable

    scn = _scn(args)
    g = GridModel(scn)
    tz = scn.clock.start.tzinfo
    day = datetime.fromisoformat(args.date).replace(tzinfo=tz) if args.date else scn.clock.start
    sites = [s.id for s in scn.sites]
    print(f"信号时间时区：{tz}；电价单位 {g.currency}/kWh；碳强度单位 kgCO2/kWh")
    print("hour | " + " | ".join(f"{s}:price/label/carbon" for s in sites))
    for h in range(24):
        t = day.replace(hour=0, minute=30) + timedelta(hours=h)
        cells = []
        for sid in sites:
            try:
                x = g.signal(sid, t)
                cells.append(f"{x.price:.3f}/{x.price_label or '-'}/{x.carbon_intensity:.3f}")
            except SignalUnavailable:
                cells.append("缺失")
        print(f"{h:02d} | " + " | ".join(cells))


def cmd_rtt(args) -> None:
    from .models import rtt_matrix

    m = rtt_matrix(_scn(args))
    sites = list(next(iter(m.values())))
    print("source \\ site | " + " | ".join(sites))
    for s, row in m.items():
        print(f"{s} | " + " | ".join(f"{row[d]:.1f}" for d in sites))


def cmd_event(args) -> None:
    import httpx

    scn = _scn(args)
    ep = _ep(args, scn)
    body = {"site": args.site, "cap_kw": args.cap_kw, "duration_min": args.duration_min, "kind": args.kind}
    from .common import admin_headers

    r = httpx.post(ep.grid + "/v1/events", json=body, headers=admin_headers(), trust_env=False)
    print(r.status_code, r.text)


def cmd_data(args) -> None:
    """数据集：按规范校验、查看描述、统计、下载远程资源、标定服务器参数。参数为数据集目录或描述文件路径。"""
    from .datasets import KINDS, load_descriptor
    from .datasets.table import validate

    if args.data_cmd == "kinds":
        for kind, spec in KINDS.items():
            print(f"{kind}（资源 {spec.resource}）：{spec.description}")
            for tag, fields in (("核心", spec.core), ("约定扩展", spec.conventional)):
                for name, fs in fields.items():
                    unit = f" [{fs.unit}]" if fs.unit else ""
                    print(f"  {tag:<4s} {name:<26s} {'/'.join(fs.types):<16s}{unit}  {fs.description}")
        return
    if args.data_cmd == "calibrate":
        from .datasets.power import calibrate, load_points

        d = load_descriptor(args.path)
        where = dict(kv.split("=", 1) for kv in args.where)
        pts = load_points(d, {k: (int(v) if k == "device_count" else v) for k, v in where.items()})
        cal = calibrate(pts, args.slots, curves=args.curves)
        print(json.dumps({**cal.to_dict(), "dataset": d.ref, "where": where}, ensure_ascii=False, indent=2))
        return
    for path in args.paths:
        d = load_descriptor(path)
        if args.data_cmd == "validate":
            rows = validate(d)
            print(f"{d.ref}（{d.kind}）符合规范：" + "，".join(f"{k} {v} 行" for k, v in rows.items()))
        elif args.data_cmd == "info":
            print(json.dumps(d.summary(), ensure_ascii=False, indent=2))
        elif args.data_cmd == "summary":
            print(json.dumps(_data_summary(d), ensure_ascii=False, indent=2))
        elif args.data_cmd == "fetch":
            from .datasets.fetch import fetch

            def progress(name: str, done: int, total: int | None) -> None:
                tot = "?" if total is None else f"{total / 1e6:.1f}"
                print(f"\r  {name} {done / 1e6:8.1f} / {tot} MB", end="", file=sys.stderr, flush=True)

            for p in fetch(d, force=args.force, progress=progress):
                print(f"\n  {p}", file=sys.stderr)
            print(f"{d.ref} 的远程资源已就绪")


def _data_summary(d) -> dict:
    if d.kind == "workload":
        from .datasets.workload import load_trace

        return load_trace(d).summary()
    if d.kind == "grid_signal":
        from .datasets.grid import SignalSet

        ss = SignalSet(d)
        out = {"ref": d.ref, "signals": {}}
        for sig in ss.signals():
            regs = ss.regions(sig)
            spans = [ss.coverage(sig, r) for r in regs]
            out["signals"][sig] = {"unit": ss.unit(sig), "regions": regs, "from": min(a for a, _ in spans).isoformat(),
                                   "to": max(b for _, b in spans).isoformat()}
        return out
    if d.kind == "power_profile":
        from .datasets.power import load_points

        pts = load_points(d)
        groups: dict[str, int] = {}
        for p in pts:
            k = f"{p.device} x{p.device_count} {p.model}"
            groups[k] = groups.get(k, 0) + 1
        return {"ref": d.ref, "measurements": len(pts), "groups": groups}
    return d.summary()


# ------------------------------------------------------------------ 解析


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpc", description="算电协同跨数据中心 AI 计算任务调度平台")
    sub = p.add_subparsers(dest="cmd", required=True)

    def workload_args(sp):
        g = sp.add_argument_group("负载变换（覆盖场景 workload 中的同名参数）")
        g.add_argument("--start-s", type=float, default=None, help="时间窗起点（原始 trace 秒）")
        g.add_argument("--duration-s", type=float, default=None, help="时间窗长度（原始 trace 秒）")
        g.add_argument("--time-scale", type=float, default=None, help="时间压缩倍数（>1 压缩）")
        g.add_argument("--rate-scale", type=float, default=None, help="到达率缩放倍数")

    def common(sp, endpoints=True, local=True):
        sp.add_argument("--scenario", "-s", default="configs/scenario.yaml", help="场景文件")
        if endpoints:
            sp.add_argument("--endpoints", "-e", default=None, help="服务地址文件（也可用环境变量 CPC_ENDPOINTS）")
            if local:
                sp.add_argument("--local", action="store_true", help="使用本机模式地址（与 cpc up 相同的默认端口）")
            sp.add_argument("--host", default=None, help="覆盖监听地址（容器内用 0.0.0.0）")
            sp.add_argument("--port", type=int, default=None, help="覆盖监听端口")
        return sp

    sp = common(sub.add_parser("grid", help="电网信号服务（同时是信号时钟的权威来源）"))
    sp.add_argument("--speedup", type=float, default=None, help="覆盖信号时钟加速倍数")
    sp.set_defaults(fn=cmd_grid)

    sp = common(sub.add_parser("site", help="站点代理"))
    sp.add_argument("--site", required=True)
    sp.add_argument("--enable-power-cap", action="store_true", help="允许通过传感器设置加速卡功率上限（NVML，需 root）")
    sp.add_argument("--initial-active", type=int, default=None)
    sp.set_defaults(fn=cmd_site)

    sp = common(sub.add_parser("gateway", help="路由网关"))
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None, help="策略参数（JSON）")
    sp.add_argument("--log", default=None, help="请求日志 JSONL 路径")
    sp.add_argument("--startup-timeout", type=float, default=30.0, help="启动时等待电网信号服务可达的期限（秒）")
    sp.set_defaults(fn=cmd_gateway)

    sp = common(sub.add_parser("controller", help="容量控制器"))
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None)
    sp.add_argument("--interval", type=float, default=None, help="控制周期（秒）")
    sp.add_argument("--log", default=None, help="决策日志 JSONL 路径")
    sp.add_argument("--actuator", choices=["site-agent", "kubernetes"], default="site-agent")
    sp.add_argument("--k8s-namespace", default="cpc")
    sp.add_argument("--k8s-template", default="vllm-{site}")
    sp.add_argument("--k8s-auth", choices=["incluster", "kubeconfig"], default="incluster", help="Kubernetes 认证方式")
    sp.add_argument("--startup-timeout", type=float, default=30.0, help="启动时等待电网信号服务可达的期限（秒）")
    sp.set_defaults(fn=cmd_controller)

    sp = common(sub.add_parser("up", help="本机单进程启动全部服务"), local=False)
    sp.add_argument("--routing", default=None)
    sp.add_argument("--capacity", default=None)
    sp.add_argument("--interval", type=float, default=None)
    sp.add_argument("--speedup", type=float, default=None, help="覆盖信号时钟加速倍数")
    sp.add_argument("--initial-active", type=int, default=None)
    sp.add_argument("--no-controller", action="store_true")
    sp.add_argument("--log-dir", default="logs")
    sp.set_defaults(fn=cmd_up)

    sp = sub.add_parser("exporter", help="通用功率导出器（多硬件；NVIDIA 另含 DCGM 兼容指标名）")
    sp.add_argument("--port", type=int, default=9400)
    sp.add_argument("--host", default=None)
    sp.add_argument("--site", default=None, help="附加 site 标签")
    sp.add_argument("--types", default=None, help="逗号分隔的传感器类型，如 nvml,rapl")
    sp.add_argument("--spec", default=None, help="传感器配置 YAML（含参数，如 ipmi 远程地址）")
    sp.add_argument("--interval", type=float, default=1.0, help="采样周期（秒）")
    sp.set_defaults(fn=cmd_exporter)

    sp = sub.add_parser("sensors", help="探测本机功率传感器并打印读数")
    sp.add_argument("--list", action="store_true", help="列出全部传感器类型及本机可用性")
    sp.add_argument("--types", default=None)
    sp.add_argument("--spec", default=None)
    sp.add_argument("--count", type=int, default=3)
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_sensors)

    sp = common(sub.add_parser("loadgen", help="负载生成器"))
    sp.add_argument("--duration", type=float, default=60)
    workload_args(sp)
    sp.add_argument("--gateway", default=None)
    sp.add_argument("--grid", default=None)
    sp.add_argument("--no-stream", action="store_true")
    sp.add_argument("--model", default="cpc", help="请求中的 model 字段（真实后端需与 vLLM 的模型名一致）")
    sp.add_argument("--out", default=None)
    sp.set_defaults(fn=cmd_loadgen)

    sp = common(sub.add_parser("sim", help="离散事件仿真（单次运行，结果写为数据包）"), endpoints=False)
    sp.add_argument("--routing", default=None, help="覆盖场景的路由策略")
    sp.add_argument("--routing-params", default=None, help="路由策略参数（JSON）")
    sp.add_argument("--capacity", default=None, help="覆盖场景的容量策略")
    sp.add_argument("--capacity-params", default=None, help="容量策略参数（JSON）")
    sp.add_argument("--hours", type=float, default=24)
    sp.add_argument("--bin-s", type=float, default=300.0, help="时间序列的时间片长度（秒）")
    workload_args(sp)
    sp.add_argument("--out", default=None)
    sp.set_defaults(fn=cmd_sim)

    sp = sub.add_parser("run", help="按实验描述运行多个变体并对比")
    sp.add_argument("experiment", help="实验描述文件（YAML）")
    sp.add_argument("--jobs", type=int, default=4, help="并行进程数")
    sp.add_argument("--out", default=None, help="结果目录（缺省为 results/<实验名>）")
    sp.set_defaults(fn=cmd_run)

    sp = common(sub.add_parser("compare", help="并行仿真多组 路由:容量 策略组合并对比"), endpoints=False)
    sp.add_argument("--runs", default="nearest:static,least_loaded:reactive,cost_aware:reactive,weighted:grid_aware",
                    help="逗号分隔的 路由:容量 组合")
    sp.add_argument("--hours", type=float, default=24)
    sp.add_argument("--bin-s", type=float, default=300.0)
    workload_args(sp)
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--out", default="results/compare")
    sp.set_defaults(fn=cmd_compare)

    sp = common(sub.add_parser("report", help="根据仿真结果作图"), endpoints=False)
    sp.add_argument("dirs", nargs="+")
    sp.set_defaults(fn=cmd_report)

    sp = common(sub.add_parser("signals", help="打印某日 24 小时电网信号"), endpoints=False)
    sp.add_argument("--date", default=None, help="YYYY-MM-DD")
    sp.set_defaults(fn=cmd_signals)

    common(sub.add_parser("rtt", help="打印来源—站点 RTT 矩阵"), endpoints=False).set_defaults(fn=cmd_rtt)

    dp = sub.add_parser("data", help="数据集：规范、校验、查看、统计、下载、标定")
    dsub = dp.add_subparsers(dest="data_cmd", required=True)
    dsub.add_parser("kinds", help="打印各类数据集的核心字段与约定扩展字段").set_defaults(fn=cmd_data)
    for name, h in (("validate", "按规范完整校验（描述文件与全部数据）"), ("info", "打印描述文件摘要（出处、许可、资源、字段）"),
                    ("summary", "数据统计（负载：请求数、时长、到达率；电网信号：信号、区域、覆盖范围；功率特性：测量组）"),
                    ("fetch", "下载描述文件中以 URL 给出的资源")):
        x = dsub.add_parser(name, help=h)
        x.add_argument("paths", nargs="+", help="数据集目录或描述文件")
        if name == "fetch":
            x.add_argument("--force", action="store_true", help="重新下载已存在的文件")
        x.set_defaults(fn=cmd_data)
    x = dsub.add_parser("calibrate", help="由功率特性数据集标定服务器模型参数")
    x.add_argument("path", help="power_profile 数据集目录或描述文件")
    x.add_argument("--where", nargs="+", required=True, metavar="字段=值",
                   help="选择同一组测量，如 device=H100 device_count=8 model=meta-llama/Llama-3.1-405B-Instruct-FP8")
    x.add_argument("--slots", type=int, required=True, help="服务器并发槽数（对应推理引擎的最大批大小）")
    x.add_argument("--curves", action="store_true", help="另给出查表式参数 gpu_power_curve、tpot_curve")
    x.set_defaults(fn=cmd_data)

    sp = common(sub.add_parser("event", help="向电网信号服务注入需求响应事件"))
    sp.add_argument("--site", required=True)
    sp.add_argument("--cap-kw", type=float, required=True)
    sp.add_argument("--duration-min", type=float, default=60, help="信号时间（分钟）")
    sp.add_argument("--kind", default="peak_shaving")
    sp.set_defaults(fn=cmd_event)
    return p


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (ConfigError, StartupError, SensorError, DatasetError) as e:
        print(f"cpc {args.cmd}：{e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
