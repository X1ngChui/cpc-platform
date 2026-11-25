"""命令行入口：cpc <子命令>（等价于 python -m cpc <子命令>）。

准备：check（检查场景与数据并打印概要）、data（数据集）、profile（由测量数据生成服务器类型）
仿真：sim（运行一次）、run（按实验描述运行多个变体）、report（作图）
实验：up（本机启动全部服务）、live（运行一次实验并写出结果）、loadgen（回放负载）、event（注入需求响应事件）
服务：grid、site、gateway、controller（单独启动某个服务）
功率测量：sensors（读取传感器）、exporter（通用功率导出器）

需要访问其他服务的命令（loadgen、event 及单独启动的服务）须用 -e / CPC_ENDPOINTS 给出地址文件，或用 --local 使用本机地址。
配置、数据或启动检查失败时打印原因并以状态码 2 退出。
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
from .config import ConfigError, Endpoints, Scenario, _host_port, default_local_endpoints, load_endpoints, load_scenario
from .datasets import DatasetError
from .sensors.base import SensorError

log = logging.getLogger("cpc")
DEFAULT_SCENARIO = "configs/scenario.yaml"
WORKLOAD_KEYS = ("start_s", "duration_s", "time_scale", "rate_scale")


def _scn(args) -> Scenario:
    scn = load_scenario(args.scenario)
    for key in WORKLOAD_KEYS:
        v = getattr(args, key, None)
        if v is not None:
            setattr(scn.workload, key, v)
    return scn


def _overrides(args) -> dict:
    return {k: getattr(args, k, None) for k in WORKLOAD_KEYS}


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


def _policy(name: str | None, params: str | None) -> dict | None:
    return {"policy": name, "params": json.loads(params) if params else {}} if name else None


# ------------------------------------------------------------------ 准备


def cmd_check(args) -> None:
    """检查场景：配置、数据集、策略名与参数、负载时间窗；打印概要。"""
    from .datasets.workload import load_workload
    from .grid.signals import GridModel, SignalUnavailable
    from .models import idle_w, peak_w, rtt_matrix
    from .sched import make_capacity, make_dispatch, make_power, make_routing

    scn = _scn(args)
    print(f"场景 {scn.name}（{Path(args.scenario).resolve()}）")
    print(f"  信号时钟起点 {scn.clock.start.isoformat()}，实验中加速 ×{scn.clock.speedup:g}")
    print("\n服务器类型")
    print(f"  {'名称':<10s} {'并发槽':>6s} {'空载 W':>9s} {'满载 W':>9s} {'满载 W/槽':>10s} {'开机 s':>7s}  说明")
    for name, p in scn.node_profiles.items():
        print(f"  {name:<10s} {p.slots:>6d} {idle_w(p):>9.0f} {peak_w(p):>9.0f} {peak_w(p) / p.slots:>10.1f} {p.boot_s:>7.0f}  {p.description}")
    print("\n站点")
    print(f"  {'站点':<6s} {'区域':<16s} {'PUE':>5s} {'服务器':>6s} {'并发槽':>7s} {'满载设施 kW':>11s}  服务器池 / 站内调度")
    for s in scn.sites:
        n = slots = peak = 0
        pools = []
        for p in s.pools:
            prof = scn.node_profiles[p.profile]
            k = p.size
            n += k
            slots += k * (prof.slots if p.backend == "emulated" else 0) + sum(u.max_inflight for u in p.upstreams)
            peak += k * peak_w(prof)
            pools.append(f"{p.id}:{p.profile}×{k}{'（真实）' if p.backend == 'openai' else ''}")
        local = f"{s.scheduler.dispatch.policy}/{s.scheduler.power.policy}"
        print(f"  {s.id:<6s} {s.region:<16s} {s.pue:>5.2f} {n:>6d} {slots:>7d} {peak * s.pue / 1000:>11.1f}  {'，'.join(pools)}；{local}")
        make_dispatch(s.scheduler.dispatch.policy, s.scheduler.dispatch.params)
        make_power(s.scheduler.power.policy, s.scheduler.power.params)
    m = rtt_matrix(scn)
    print("\n往返时延（ms，来源 → 站点）")
    print("  " + " " * 8 + "".join(f"{s.id:>8s}" for s in scn.sites))
    for src in scn.sources:
        print(f"  {src.id:<8s}" + "".join(f"{m[src.id][s.id]:>8.1f}" for s in scn.sites) + f"   （请求占比 {src.share:g}）")
    g = GridModel(scn)
    print(f"\n电网信号（电价 {g.currency}/kWh，碳强度 kgCO2/kWh）：{g.sources()}")
    day = (datetime.fromisoformat(args.date).replace(tzinfo=scn.clock.start.tzinfo) if args.date
           else scn.clock.start.replace(hour=0, minute=0, second=0, microsecond=0))
    print(f"  {day:%Y-%m-%d} 各时刻（电价/时段/碳强度）")
    print("  时刻  " + "".join(f"{s.id:>24s}" for s in scn.sites))
    for h in range(0, 24, args.step):
        t = day + timedelta(hours=h, minutes=30)
        cells = []
        for s in scn.sites:
            try:
                x = g.signal(s.id, t)
                cells.append(f"{x.price:.3f}/{x.price_label or '-'}/{x.carbon_intensity:.3f}")
            except SignalUnavailable:
                cells.append("缺失")
        print(f"  {h:02d}:30 " + "".join(f"{c:>24s}" for c in cells))
    tr = load_workload(scn)
    sm = tr.summary()
    print(f"\n负载 {sm['ref']}：{sm['requests']} 个请求，{sm['duration_s'] / 3600:.2f} 小时，平均 {sm['mean_rps']} 请求/秒，"
          f"输入 / 输出 token 均值 {sm['input_tokens_mean']} / {sm['output_tokens_mean']}")
    r, c = scn.scheduler.routing, scn.scheduler.capacity
    make_routing(r.policy, r.params)
    make_capacity(c.policy, c.params)
    print(f"\n全局策略：路由 {r.policy} {r.params or ''}，容量 {c.policy} {c.params or ''}")
    print("\n场景检查通过")


def cmd_data(args) -> None:
    """数据集：类别规范、检查与统计、描述信息。参数为数据集目录或描述文件。"""
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
    for path in args.paths:
        d = load_descriptor(path)
        if args.data_cmd == "show":
            print(json.dumps(d.summary(), ensure_ascii=False, indent=2))
            continue
        rows = validate(d)
        print(f"{d.ref}（{d.kind}）符合规范：" + "，".join(f"{k} {v} 行" for k, v in rows.items()))
        print(json.dumps(_data_stats(d), ensure_ascii=False, indent=2))


def _data_stats(d) -> dict:
    if d.kind == "workload":
        from .datasets.workload import load_trace

        return load_trace(d).summary()
    if d.kind == "grid_signal":
        from .datasets.grid import SignalSet

        ss = SignalSet(d)
        out: dict = {"signals": {}}
        for sig in ss.signals():
            regs = ss.regions(sig)
            spans = [ss.coverage(sig, r) for r in regs]
            out["signals"][sig] = {"unit": ss.unit(sig), "regions": regs, "from": min(a for a, _ in spans).isoformat(),
                                   "to": max(b for _, b in spans).isoformat()}
        return out
    from .datasets.power import load_points

    groups: dict[str, int] = {}
    for p in load_points(d):
        k = f"device={p.device} device_count={p.device_count} model={p.model}"
        groups[k] = groups.get(k, 0) + 1
    return {"measurements_by_group": groups}


def cmd_profile(args) -> None:
    """由功率特性数据集生成服务器类型，打印可写入场景 node_profiles 的 YAML。"""
    import yaml

    from .datasets import load_descriptor
    from .datasets.power import load_points, profile_from_points

    d = load_descriptor(args.dataset)
    where = {}
    for kv in args.where:
        if "=" not in kv:
            raise ConfigError(f"--where 的每一项须为 字段=值，当前为 {kv}")
        k, v = kv.split("=", 1)
        where[k] = int(v) if k == "device_count" else v
    prof = profile_from_points(load_points(d, where), args.slots, args.idle_w, args.host_w, args.prefill_tokens_per_s,
                               args.boot_s, source=d.ref)

    class Flow(list):
        pass

    yaml.SafeDumper.add_representer(Flow, lambda dumper, x: dumper.represent_sequence("tag:yaml.org,2002:seq", x, flow_style=True))
    for k in ("power_w", "tpot_s"):
        prof[k] = [Flow(p) for p in prof[k]]
    text = yaml.safe_dump({args.name: prof}, allow_unicode=True, sort_keys=False, width=200)
    missing = [k for k in ("prefill_tokens_per_s", "boot_s") if k not in prof]
    if missing:
        text += f"    # 须补充：{'、'.join(missing)}（测量数据中没有这些参数）\n"
    print(text, end="")


# ------------------------------------------------------------------ 仿真


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


def _print_summary(s: dict) -> None:
    cur = s.get("currency") or ""
    keys = [("arrived", "到达请求"), ("served", "完成"), ("rejected", "站点拒绝"), ("unrouted", "未路由"), ("failed", "失败"),
            ("unfinished", "结束时未完成")]
    print("  请求：" + "，".join(f"{label} {s[k]}" for k, label in keys if k in s))
    if s.get("ttft_p99_s") is not None:
        print(f"  SLO 达成率 {s['slo_attainment']:.2%}；首 token 时延 P50 {s['ttft_p50_s']:.3f} s，P99 {s['ttft_p99_s']:.3f} s")
    print(f"  设施电量 {s['facility_kwh']:.3f} kWh（IT {s['it_kwh']:.3f} kWh），电费 {s['cost']:.2f} {cur}，"
          f"碳排放 {s['carbon_kg']:.2f} kg，每请求 {s['wh_per_request']:.2f} Wh")
    if s.get("signal_coverage") not in (None, 1.0):
        print(f"  注意：只有 {s['signal_coverage']:.1%} 的设施电量有电网信号，其余部分未计入电费与碳排放")


def cmd_sim(args) -> None:
    scn = load_scenario(args.scenario)
    routing = _policy(args.routing, args.routing_params)
    capacity = _policy(args.capacity, args.capacity_params)
    name = f"{args.routing or scn.scheduler.routing.policy}__{args.capacity or scn.scheduler.capacity.policy}"
    out = args.out or f"results/sim-{name}"
    s = _run_variant(args.scenario, name, routing, capacity, _overrides(args), args.hours, args.bin_s, out)
    print(f"仿真完成（{s['wall_s']} 秒）：路由 {s['routing']}，容量 {s['capacity']}，{s['hours']:g} 小时")
    _print_summary(s)
    print(f"结果已写入 {out}/")


def cmd_run(args) -> None:
    """按实验描述并行运行全部变体，写出各变体的结果与对比表。"""
    from .results import load_experiment, write_comparison

    exp = load_experiment(args.experiment)
    out = Path(args.out or f"results/{exp.name}")
    variants = [{"name": v.name, "routing": v.routing.model_dump() if v.routing else None,
                 "capacity": v.capacity.model_dump() if v.capacity else None, "workload": v.workload} for v in exp.variants]
    ctx = multiprocessing.get_context("spawn")  # 子进程不继承父进程的线程与状态
    print(f"实验 {exp.name}：{len(variants)} 个变体，场景 {exp.scenario_path()}，{exp.hours:g} 小时")
    with ProcessPoolExecutor(max_workers=max(1, min(len(variants), args.jobs)), mp_context=ctx) as ex:
        futs = [ex.submit(_run_variant, str(exp.scenario_path()), v["name"], v.get("routing"), v.get("capacity"),
                          v.get("workload", {}), exp.hours, exp.bin_s, str(out / v["name"])) for v in variants]
        rows = [f.result() for f in futs]
    p = write_comparison(out, rows)
    cur = rows[0].get("currency") or ""
    print(f"  {'变体':<24s} {'完成':>9s} {'拒绝':>6s} {'SLO':>7s} {'TTFT P99':>9s} {'设施 kWh':>10s} {'电费 ' + cur:>10s} {'碳 kg':>9s}")
    for r in rows:
        print(f"  {r['variant']:<24s} {r['served']:>9d} {r['rejected']:>6d} {r['slo_attainment']:>7.2%} "
              f"{(r['ttft_p99_s'] or 0):>9.3f} {r['facility_kwh']:>10.1f} {r['cost']:>10.1f} {r['carbon_kg']:>9.1f}")
    print(f"结果已写入 {out}/，对比表 {p}")


def cmd_report(args) -> None:
    from .sim.report import plot_comparison, plot_run

    for d in args.dirs:
        p = Path(d)
        if (p / "comparison.csv").exists():
            print(plot_comparison(p))
        runs = [p] if (p / "timeseries.csv").exists() else [x for x in sorted(p.iterdir()) if (x / "timeseries.csv").exists()]
        for r in runs:
            print(plot_run(r))


# ------------------------------------------------------------------ 实验


def cmd_up(args) -> None:
    """在一个进程中启动电网信号服务、全部站点代理、网关和控制器，直到 Ctrl+C 或任一服务退出。"""
    from .launch import Running, build_apps

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    ep = load_endpoints(args.endpoints, scn) if args.endpoints else default_local_endpoints(scn)
    log_dir = Path(args.log_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
    apps = build_apps(scn, ep, log_dir, routing=args.routing, capacity=args.capacity, interval=args.interval,
                      initial_active=args.initial_active, controller=not args.no_controller)

    async def main() -> int:
        run = Running(apps, args.host)
        await run.start()
        print(f"[cpc up] 场景 {scn.name}：{len(scn.sites)} 个站点，信号时钟加速 ×{scn.clock.speedup:g}")
        for app, url in apps:
            print(f"  {app.title:<36s} {url}")
        print(f"  日志目录 {log_dir.resolve()}；Ctrl+C 退出")
        exited = await run.wait()
        await run.stop()
        print(f"[cpc up] 服务异常退出：{exited}", file=sys.stderr)
        return 1

    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
    except RuntimeError as e:
        raise StartupError(str(e)) from e


def cmd_live(args) -> None:
    """运行一次实验：未给出地址文件时在本进程中启动全部服务；回放负载、采样并写出结果数据包。"""
    from .launch import Running, build_apps
    from .live import run_live
    from .results import write_run

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    out = Path(args.out or f"results/live-{datetime.now():%Y%m%d-%H%M%S}")
    local = not args.endpoints
    ep = default_local_endpoints(scn) if local else load_endpoints(args.endpoints, scn)

    async def main():
        run = None
        if local:
            if args.routing:
                scn.scheduler.routing.policy, scn.scheduler.routing.params = args.routing, {}
            if args.capacity:
                scn.scheduler.capacity.policy, scn.scheduler.capacity.params = args.capacity, {}
            apps = build_apps(scn, ep, out / "logs", interval=args.interval, initial_active=args.initial_active)
            run = Running(apps)
            await run.start()
            print(f"[cpc live] 已在本进程中启动 {len(apps)} 个服务")
        elif args.routing or args.capacity:
            raise ConfigError("连接已部署的服务时，策略由各服务的启动参数决定；请改用 /admin/policy 切换")
        print(f"[cpc live] 回放负载 {args.duration:g} 秒（采样周期 {args.sample_s:g} 秒）…")
        try:
            return await run_live(scn, ep, args.duration, sample_s=args.sample_s, model=args.model, stream=not args.no_stream,
                                  decisions_log=(out / "logs" / "controller.jsonl") if local else None)
        finally:
            if run is not None:
                await run.stop()

    try:
        res, pol = asyncio.run(main())
    except RuntimeError as e:
        raise StartupError(str(e)) from e
    write_run(out, res, scn, {"name": out.name, "mode": "live", "duration_s": args.duration, "sample_s": args.sample_s,
                              "endpoints": None if local else ep.model_dump(), **pol})
    print(f"实验完成：路由 {pol['routing']}，容量 {pol['capacity']}，{args.duration:g} 秒")
    _print_summary(res.summary)
    print(f"结果已写入 {out}/")


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


def cmd_event(args) -> None:
    import httpx

    from .common import admin_headers

    scn = _scn(args)
    ep = _ep(args, scn)
    body = {"site": args.site, "cap_kw": args.cap_kw, "duration_min": args.duration_min, "kind": args.kind}
    r = httpx.post(ep.grid + "/v1/events", json=body, headers=admin_headers(), trust_env=False)
    print(r.status_code, r.text)


# ------------------------------------------------------------------ 服务


def cmd_grid(args) -> None:
    from .grid.service import create_app

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    _serve(create_app(scn), *_bind(args, _ep(args, scn).grid))


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
    _serve(create_app(scn, ep, args.policy, params, args.interval, args.log, args.startup_timeout), *_bind(args, ep.controller))


# ------------------------------------------------------------------ 功率测量


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
            print(f"{name:<14s} {cls.requires:<40s} {'具备' if cls.detect() else ''}")
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


# ------------------------------------------------------------------ 解析


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpc", description="算电协同跨数据中心 AI 计算任务调度平台",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd", required=True, metavar="子命令")

    def scenario(sp):
        sp.add_argument("--scenario", "-s", default=DEFAULT_SCENARIO, help=f"场景文件（缺省 {DEFAULT_SCENARIO}）")
        return sp

    def workload(sp):
        g = sp.add_argument_group("负载变换（覆盖场景 workload 中的同名参数）")
        g.add_argument("--start-s", type=float, default=None, help="时间窗起点（原始 trace 秒）")
        g.add_argument("--duration-s", type=float, default=None, help="时间窗长度（原始 trace 秒）")
        g.add_argument("--time-scale", type=float, default=None, help="时间压缩倍数（>1 压缩）")
        g.add_argument("--rate-scale", type=float, default=None, help="到达率缩放倍数")

    def endpoints(sp, local=True, listen=True):
        sp.add_argument("--endpoints", "-e", default=None, help="服务地址文件（也可用环境变量 CPC_ENDPOINTS）")
        if local:
            sp.add_argument("--local", action="store_true", help="使用本机地址（与 cpc up 相同的端口）")
        if listen:
            sp.add_argument("--host", default=None, help="覆盖监听地址（容器内用 0.0.0.0）")
            sp.add_argument("--port", type=int, default=None, help="覆盖监听端口")

    # 准备
    sp = scenario(sub.add_parser("check", help="检查场景与数据，打印站点、时延、电网信号与负载概要"))
    sp.add_argument("--date", default=None, help="打印电网信号的日期（YYYY-MM-DD，缺省为场景时钟起点所在日）")
    sp.add_argument("--step", type=int, default=3, help="电网信号表的小时间隔（缺省 3）")
    workload(sp)
    sp.set_defaults(fn=cmd_check)

    dp = sub.add_parser("data", help="数据集：类别规范、检查与统计、描述信息")
    dsub = dp.add_subparsers(dest="data_cmd", required=True, metavar="操作")
    dsub.add_parser("kinds", help="打印各类数据集的核心字段与约定扩展字段").set_defaults(fn=cmd_data)
    for name, h in (("check", "完整检查数据集并打印统计"), ("show", "打印描述文件摘要（出处、许可、资源、字段）")):
        x = dsub.add_parser(name, help=h)
        x.add_argument("paths", nargs="+", help="数据集目录或描述文件")
        x.set_defaults(fn=cmd_data)

    sp = sub.add_parser("profile", help="由功率特性数据集生成服务器类型（打印 YAML）")
    sp.add_argument("dataset", help="power_profile 数据集目录或描述文件")
    sp.add_argument("--where", nargs="+", required=True, metavar="字段=值",
                    help="选择同一组测量，如 device=H100 device_count=8 model=... task=...")
    sp.add_argument("--slots", type=int, required=True, help="并发槽数（推理引擎的最大批大小）")
    sp.add_argument("--idle-w", type=float, required=True, help="服务器在线空载功率（W），作为表的第一个点")
    sp.add_argument("--host-w", type=float, default=0.0, help="加到每个测量点上的主机部分功率（W，缺省 0）")
    sp.add_argument("--prefill-tokens-per-s", type=float, default=None, help="预填充吞吐（不在测量中）")
    sp.add_argument("--boot-s", type=float, default=None, help="开机到可服务的时间（不在测量中）")
    sp.add_argument("--name", default="server", help="服务器类型名称（缺省 server）")
    sp.set_defaults(fn=cmd_profile)

    # 仿真
    sp = scenario(sub.add_parser("sim", help="运行一次仿真，结果写为结果数据包"))
    sp.add_argument("--routing", default=None, help="覆盖场景的路由策略")
    sp.add_argument("--routing-params", default=None, help="路由策略参数（JSON）")
    sp.add_argument("--capacity", default=None, help="覆盖场景的容量策略")
    sp.add_argument("--capacity-params", default=None, help="容量策略参数（JSON）")
    sp.add_argument("--hours", type=float, default=24, help="仿真时长（小时，缺省 24）")
    sp.add_argument("--bin-s", type=float, default=300.0, help="时间序列的时间片长度（秒，缺省 300）")
    workload(sp)
    sp.add_argument("--out", default=None, help="结果目录（缺省 results/sim-<路由>__<容量>）")
    sp.set_defaults(fn=cmd_sim)

    sp = sub.add_parser("run", help="按实验描述运行多个仿真变体并对比")
    sp.add_argument("experiment", help="实验描述文件（YAML）")
    sp.add_argument("--jobs", type=int, default=4, help="并行进程数（缺省 4）")
    sp.add_argument("--out", default=None, help="结果目录（缺省 results/<实验名>）")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("report", help="由结果目录作图（需要 analysis 可选依赖）")
    sp.add_argument("dirs", nargs="+", help="结果目录或实验结果目录")
    sp.set_defaults(fn=cmd_report)

    # 实验
    sp = scenario(sub.add_parser("up", help="本机单进程启动全部实验服务"))
    endpoints(sp, local=False, listen=False)
    sp.add_argument("--host", default=None, help="覆盖全部服务的监听地址")
    sp.add_argument("--routing", default=None, help="覆盖场景的路由策略")
    sp.add_argument("--capacity", default=None, help="覆盖场景的容量策略")
    sp.add_argument("--interval", type=float, default=None, help="控制周期（秒）")
    sp.add_argument("--speedup", type=float, default=None, help="信号时钟加速倍数")
    sp.add_argument("--initial-active", type=int, default=None, help="各站点初始在线的服务器数")
    sp.add_argument("--no-controller", action="store_true", help="不启动控制器")
    sp.add_argument("--log-dir", default="logs", help="日志目录（缺省 logs）")
    sp.set_defaults(fn=cmd_up)

    sp = scenario(sub.add_parser("live", help="运行一次实验：回放负载、采样，结果写为结果数据包"))
    sp.add_argument("--duration", type=float, required=True, help="实验时长（秒）")
    sp.add_argument("--endpoints", "-e", default=None, help="已部署服务的地址文件；缺省时在本进程中启动全部服务")
    sp.add_argument("--routing", default=None, help="覆盖场景的路由策略（只用于本进程启动的服务）")
    sp.add_argument("--capacity", default=None, help="覆盖场景的容量策略（只用于本进程启动的服务）")
    sp.add_argument("--interval", type=float, default=None, help="控制周期（秒）")
    sp.add_argument("--speedup", type=float, default=None, help="信号时钟加速倍数")
    sp.add_argument("--initial-active", type=int, default=None, help="各站点初始在线的服务器数")
    sp.add_argument("--sample-s", type=float, default=10.0, help="站点状态与电网信号的采样周期（秒，缺省 10）")
    sp.add_argument("--model", default="cpc", help="请求中的 model 字段（真实推理实例的模型名）")
    sp.add_argument("--no-stream", action="store_true", help="使用非流式请求（不测量首 token 时延）")
    workload(sp)
    sp.add_argument("--out", default=None, help="结果目录（缺省 results/live-<时间>）")
    sp.set_defaults(fn=cmd_live)

    sp = scenario(sub.add_parser("loadgen", help="按场景负载向网关回放请求"))
    endpoints(sp, listen=False)
    sp.add_argument("--duration", type=float, default=60, help="回放时长（秒）")
    workload(sp)
    sp.add_argument("--gateway", default=None, help="直接给出网关地址（与 --grid 一起使用时不需要地址文件）")
    sp.add_argument("--grid", default=None, help="直接给出电网信号服务地址")
    sp.add_argument("--no-stream", action="store_true")
    sp.add_argument("--model", default="cpc", help="请求中的 model 字段（真实推理实例的模型名）")
    sp.add_argument("--out", default=None, help="逐请求记录（JSONL）")
    sp.set_defaults(fn=cmd_loadgen)

    sp = scenario(sub.add_parser("event", help="向电网信号服务注入需求响应事件"))
    endpoints(sp, listen=False)
    sp.add_argument("--site", required=True)
    sp.add_argument("--cap-kw", type=float, required=True, help="设施功率上限（kW）")
    sp.add_argument("--duration-min", type=float, default=60, help="持续时间（信号时间，分钟）")
    sp.add_argument("--kind", default="peak_shaving")
    sp.set_defaults(fn=cmd_event)

    # 服务
    sp = scenario(sub.add_parser("grid", help="电网信号服务（信号时钟的权威来源）"))
    endpoints(sp)
    sp.add_argument("--speedup", type=float, default=None, help="信号时钟加速倍数")
    sp.set_defaults(fn=cmd_grid)

    sp = scenario(sub.add_parser("site", help="站点代理"))
    endpoints(sp)
    sp.add_argument("--site", required=True)
    sp.add_argument("--enable-power-cap", action="store_true", help="通过传感器设置加速卡功率上限（NVML 需要 root）")
    sp.add_argument("--initial-active", type=int, default=None)
    sp.set_defaults(fn=cmd_site)

    sp = scenario(sub.add_parser("gateway", help="路由网关"))
    endpoints(sp)
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None, help="策略参数（JSON）")
    sp.add_argument("--log", default=None, help="请求日志（JSONL）")
    sp.add_argument("--startup-timeout", type=float, default=30.0, help="等待电网信号服务可达的期限（秒）")
    sp.set_defaults(fn=cmd_gateway)

    sp = scenario(sub.add_parser("controller", help="容量控制器"))
    endpoints(sp)
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None, help="策略参数（JSON）")
    sp.add_argument("--interval", type=float, default=None, help="控制周期（秒）")
    sp.add_argument("--log", default=None, help="决策日志（JSONL）")
    sp.add_argument("--startup-timeout", type=float, default=30.0, help="等待电网信号服务可达的期限（秒）")
    sp.set_defaults(fn=cmd_controller)

    # 功率测量
    sp = sub.add_parser("sensors", help="列出或读取功率传感器")
    sp.add_argument("--list", action="store_true", help="列出全部传感器类型及本机是否具备其前提条件")
    sp.add_argument("--types", default=None, help="逗号分隔的传感器类型，如 nvml,rapl")
    sp.add_argument("--spec", default=None, help="传感器配置（YAML，含参数）")
    sp.add_argument("--count", type=int, default=3)
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_sensors)

    sp = sub.add_parser("exporter", help="通用功率导出器（Prometheus 格式）")
    sp.add_argument("--port", type=int, default=9400)
    sp.add_argument("--host", default=None)
    sp.add_argument("--site", default=None, help="附加 site 标签")
    sp.add_argument("--types", default=None, help="逗号分隔的传感器类型")
    sp.add_argument("--spec", default=None, help="传感器配置（YAML）")
    sp.add_argument("--interval", type=float, default=1.0, help="采样周期（秒）")
    sp.set_defaults(fn=cmd_exporter)
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
