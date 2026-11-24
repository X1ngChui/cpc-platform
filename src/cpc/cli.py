"""命令行入口：python -m cpc <子命令> 或 cpc <子命令>。

服务类：grid / site / gateway / controller / up（本机单进程启动全部服务）/ exporter（通用功率导出器）
工具类（能耗感知）：sensors（探测本机功率传感器）
工具类：loadgen / sim / compare / report / signals / rtt / event
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .common import setup_logging
from .config import CST, Endpoints, Scenario, load_endpoints, load_scenario, _host_port

log = logging.getLogger("cpc")


def _scn(args) -> Scenario:
    return load_scenario(args.scenario)


def _ep(args, scn: Scenario) -> Endpoints:
    return load_endpoints(getattr(args, "endpoints", None), scn)


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
    app = create_app(scn, args.site, power=args.power, enable_power_cap=args.enable_power_cap, initial_active=args.initial_active)
    _serve(app, *_bind(args, ep.sites[args.site]))


def cmd_gateway(args) -> None:
    from .gateway.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    params = json.loads(args.params) if args.params else None
    _serve(create_app(scn, ep, args.policy, params, args.log), *_bind(args, ep.gateway))


def cmd_controller(args) -> None:
    from .controller.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    params = json.loads(args.params) if args.params else None
    app = create_app(scn, ep, args.policy, params, args.interval, args.log, args.actuator, args.k8s_namespace, args.k8s_template)
    _serve(app, *_bind(args, ep.controller))


def cmd_up(args) -> None:
    """本机开发模式：在一个进程中启动电网信号服务、全部站点代理、网关和控制器。"""
    import uvicorn

    from .controller.service import create_app as ctl_app
    from .gateway.service import create_app as gw_app
    from .grid.service import create_app as grid_app
    from .site.service import create_app as site_app

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    ep = _ep(args, scn)
    out = Path(args.log_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    apps = [(grid_app(scn), ep.grid)]
    apps += [(site_app(scn, s.id, initial_active=args.initial_active), ep.sites[s.id]) for s in scn.sites]
    apps.append((gw_app(scn, ep, args.routing, None, str(out / f"gateway-{stamp}.jsonl")), ep.gateway))
    if not args.no_controller:
        apps.append((ctl_app(scn, ep, args.capacity, None, args.interval, str(out / f"controller-{stamp}.jsonl")), ep.controller))

    async def main() -> None:
        servers = []
        for app, url in apps:
            h, p = _host_port(url)
            servers.append(uvicorn.Server(uvicorn.Config(app, host=args.host or h, port=p, log_level="warning", access_log=False)))
        print(f"[cpc up] 场景 {scn.name}：{len(scn.sites)} 个站点，信号时钟加速 ×{scn.clock.speedup}")
        for app, url in apps:
            print(f"  {app.title:<36s} {url}")
        print(f"  日志目录 {out.resolve()}；Ctrl+C 退出")
        await asyncio.gather(*(s.serve() for s in servers))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def _sensors_from_args(args):
    import yaml

    from .sensors import autodetect, make_sensor

    if getattr(args, "spec", None):
        with open(args.spec, encoding="utf-8") as f:
            return [make_sensor(s) for s in yaml.safe_load(f)["sensors"]]
    if getattr(args, "types", None):
        return [make_sensor(t) for t in args.types.split(",")]
    return autodetect()


def cmd_exporter(args) -> None:
    from .sensors.exporter import serve

    sensors = _sensors_from_args(args)
    if not sensors:
        raise SystemExit("未探测到可用传感器；可用 --types 或 --spec 显式指定")
    serve(sensors, args.port, args.host or "0.0.0.0", args.site, args.interval)


def cmd_sensors(args) -> None:
    """列出可用传感器类型、探测本机传感器并打印读数。"""
    from .sensors import SENSORS
    from .sensors.base import EnergyIntegrator
    from .sensors.exporter import host_total

    if args.list:
        for name, cls in SENSORS.items():
            ok = ""
            try:
                ok = "可用" if cls.detect() else ""
            except Exception:  # noqa: BLE001
                pass
            print(f"{name:<14s} {cls.requires:<40s} {'已实测' if cls.verified else '按文档实现'}  {ok}")
        return
    sensors = _sensors_from_args(args)
    print("传感器：", [s.name for s in sensors] or "无")
    integ = EnergyIntegrator()
    for k in range(args.count):
        rs = []
        for s in sensors:
            try:
                rs += s.read()
            except Exception as e:  # noqa: BLE001
                print(f"  {s.name} 读取失败：{e}")
        integ.update(rs)
        if args.json:
            print(json.dumps([r.to_dict() for r in rs], ensure_ascii=False))
        else:
            for r in rs:
                pw = "-" if r.power_w is None else f"{r.power_w:8.2f} W"
                en = "-" if r.energy_j is None else f"{r.energy_j:12.1f} J"
                ut = "" if r.util_pct is None else f"{r.util_pct:5.1f} %"
                print(f"  [{r.sensor}] {r.device:<14s} {r.kind:<8s} {r.vendor:<8s} {r.model[:28]:<28s} {pw:>11s} {en:>15s} {ut}")
            print(f"  主机合计 {host_total(rs):.2f} W")
        if k + 1 < args.count:
            time.sleep(args.interval)


# ------------------------------------------------------------------ 工具


def cmd_loadgen(args) -> None:
    from .loadgen.client import run

    scn = _scn(args)
    ep = _ep(args, scn)
    s = asyncio.run(run(args.gateway or ep.gateway, scn, args.duration, grid=args.grid or ep.grid, rps=args.rps,
                        stream=not args.no_stream, out=args.out, trace=args.trace, time_scale=args.time_scale, model=args.model))
    print(json.dumps(s, ensure_ascii=False, indent=2))


def _run_sim(scenario: str, routing: str, capacity: str, hours: float, rps: float | None, out: str | None,
             routing_params: dict | None = None, capacity_params: dict | None = None) -> dict:
    from .sched import make_capacity, make_routing
    from .sim.des import Simulator

    scn = load_scenario(scenario)
    rp = routing_params if routing_params is not None else (scn.scheduler.routing.params if routing == scn.scheduler.routing.policy else {})
    cp = capacity_params if capacity_params is not None else (scn.scheduler.capacity.params if capacity == scn.scheduler.capacity.policy else {})
    t0 = time.time()
    res = Simulator(scn, make_routing(routing, rp), make_capacity(capacity, cp), hours=hours, rps=rps).run()
    res.summary["wall_s"] = round(time.time() - t0, 2)
    if out:
        d = Path(out)
        d.mkdir(parents=True, exist_ok=True)
        (d / "summary.json").write_text(json.dumps(res.summary, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(d / "timeseries.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(res.timeseries[0]))
            w.writeheader()
            w.writerows(res.timeseries)
        with open(d / "decisions.jsonl", "w", encoding="utf-8") as f:
            for r in res.decisions:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return res.summary


def cmd_sim(args) -> None:
    scn = _scn(args)
    routing = args.routing or scn.scheduler.routing.policy
    capacity = args.capacity or scn.scheduler.capacity.policy
    out = args.out or f"results/sim-{routing}-{capacity}"
    s = _run_sim(args.scenario, routing, capacity, args.hours, args.rps, out)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    print(f"结果已写入 {out}/")


def cmd_compare(args) -> None:
    """并行运行多组 路由:容量 策略组合，输出对比表。"""
    combos = [c.split(":") for c in args.runs.split(",")]
    out = Path(args.out)
    with ProcessPoolExecutor(max_workers=min(len(combos), args.jobs)) as ex:
        futs = [ex.submit(_run_sim, args.scenario, r, c, args.hours, args.rps, str(out / f"{r}__{c}")) for r, c in combos]
        rows = [f.result() for f in futs]
    keys = ["routing", "capacity", "served", "rejected", "slo_attainment", "ttft_p99_s", "facility_kwh", "cost_yuan", "carbon_kg", "wh_per_request"]
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "comparison.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(" | ".join(keys))
    for r in rows:
        print(" | ".join(f"{r[k]:.4g}" if isinstance(r[k], float) else str(r[k]) for k in keys))
    print(f"对比表已写入 {out / 'comparison.csv'}")


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
    from .grid.signals import GridModel

    scn = _scn(args)
    g = GridModel(scn)
    day = datetime.fromisoformat(args.date).replace(tzinfo=CST) if args.date else scn.clock.start
    sites = [s.id for s in scn.sites]
    print("hour | " + " | ".join(f"{s}:price/period/carbon" for s in sites))
    for h in range(24):
        t = day.replace(hour=0, minute=30) + timedelta(hours=h)
        cells = []
        for sid in sites:
            x = g.signal(sid, t)
            cells.append(f"{x.price_yuan_per_kwh:.3f}/{x.period}/{x.carbon_kg_per_kwh:.3f}")
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


# ------------------------------------------------------------------ 解析


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpc", description="算电协同跨数据中心 AI 任务调度虚拟化平台")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, endpoints=True):
        sp.add_argument("--scenario", "-s", default="configs/scenario.yaml", help="场景文件")
        if endpoints:
            sp.add_argument("--endpoints", "-e", default=None, help="服务地址文件（缺省为本机默认端口）")
            sp.add_argument("--host", default=None, help="覆盖监听地址（容器内用 0.0.0.0）")
            sp.add_argument("--port", type=int, default=None, help="覆盖监听端口")
        return sp

    sp = common(sub.add_parser("grid", help="电网信号服务（同时是信号时钟的权威来源）"))
    sp.add_argument("--speedup", type=float, default=None, help="覆盖信号时钟加速倍数")
    sp.set_defaults(fn=cmd_grid)

    sp = common(sub.add_parser("site", help="站点代理"))
    sp.add_argument("--site", required=True)
    sp.add_argument("--power", choices=["auto", "model", "sensors"], default="auto",
                    help="真实后端的功率来源：auto=场景配置或自动探测的传感器，失败时退回模型")
    sp.add_argument("--enable-power-cap", action="store_true", help="允许通过 NVML 设置 GPU 功率上限（需 root）")
    sp.add_argument("--initial-active", type=int, default=None)
    sp.set_defaults(fn=cmd_site)

    sp = common(sub.add_parser("gateway", help="路由网关"))
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None, help="策略参数（JSON）")
    sp.add_argument("--log", default=None, help="请求日志 JSONL 路径")
    sp.set_defaults(fn=cmd_gateway)

    sp = common(sub.add_parser("controller", help="容量控制器"))
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None)
    sp.add_argument("--interval", type=float, default=None, help="控制周期（秒）")
    sp.add_argument("--log", default=None, help="决策日志 JSONL 路径")
    sp.add_argument("--actuator", choices=["site-agent", "kubernetes"], default="site-agent")
    sp.add_argument("--k8s-namespace", default="cpc")
    sp.add_argument("--k8s-template", default="vllm-{site}")
    sp.set_defaults(fn=cmd_controller)

    sp = common(sub.add_parser("up", help="本机单进程启动全部服务"))
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
    sp.add_argument("--types", default=None, help="逗号分隔的传感器类型，如 nvml,rapl；缺省自动探测")
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
    sp.add_argument("--rps", type=float, default=None)
    sp.add_argument("--gateway", default=None)
    sp.add_argument("--grid", default=None)
    sp.add_argument("--no-stream", action="store_true")
    sp.add_argument("--trace", default=None, help="Azure LLM trace CSV")
    sp.add_argument("--time-scale", type=float, default=1.0, help="trace 时间压缩倍数")
    sp.add_argument("--model", default="cpc", help="请求中的 model 字段（真实后端需与 vLLM 的模型名一致）")
    sp.add_argument("--out", default=None)
    sp.set_defaults(fn=cmd_loadgen)

    sp = common(sub.add_parser("sim", help="离散事件仿真"), endpoints=False)
    sp.add_argument("--routing", default=None)
    sp.add_argument("--capacity", default=None)
    sp.add_argument("--hours", type=float, default=24)
    sp.add_argument("--rps", type=float, default=None)
    sp.add_argument("--out", default=None)
    sp.set_defaults(fn=cmd_sim)

    sp = common(sub.add_parser("compare", help="并行仿真多组策略并对比"), endpoints=False)
    sp.add_argument("--runs", default="nearest:static,least_loaded:reactive,cost_aware:reactive,weighted:grid_aware",
                    help="逗号分隔的 路由:容量 组合")
    sp.add_argument("--hours", type=float, default=24)
    sp.add_argument("--rps", type=float, default=None)
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

    sp = common(sub.add_parser("event", help="向电网信号服务注入需求响应事件"))
    sp.add_argument("--site", required=True)
    sp.add_argument("--cap-kw", type=float, required=True)
    sp.add_argument("--duration-min", type=float, default=60, help="信号时间（分钟）")
    sp.add_argument("--kind", default="peak_shaving")
    sp.set_defaults(fn=cmd_event)
    return p


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    from .sched import load_plugins

    load_plugins()
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
