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


def _json_arg(flag: str, text: str | None) -> dict | None:
    """解析 JSON 对象形式的命令行参数。"""
    if text is None:
        return None
    try:
        v = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{flag} 不是有效的 JSON（第 {e.colno} 个字符：{e.msg}）。"
                          f"示例：{flag} '{{\"max_wait_s\": 1.0}}'（键与字符串用双引号）") from None
    if not isinstance(v, dict):
        raise ConfigError(f"{flag} 应为 JSON 对象，如 '{{\"max_wait_s\": 1.0}}'")
    return v


def _policy(name: str | None, params: str | None, flag: str) -> dict | None:
    """--routing / --capacity 与对应的参数：只给出参数时修改场景中策略的参数。"""
    p = _json_arg(flag, params)
    return None if name is None and p is None else {"policy": name, "params": p}


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
    for kv in args.where or []:
        if "=" not in kv:
            raise ConfigError(f"--where 的每一项须为 字段=值，当前为 {kv}")
        k, v = kv.split("=", 1)
        where[k] = int(v) if k == "device_count" else v
    m = (d.cpc or {}).get("measurement") or {}  # cpc measure 写入的测量条件与结果
    notes = []

    def pick(arg, key, label):
        if arg is not None:
            return arg
        if m.get(key) is not None:
            notes.append(f"{label}取自测量（{key} = {m[key]}）")
            return m[key]
        return None

    slots = pick(args.slots, "slots", "并发槽数")
    idle = pick(args.idle_w, "idle_w", "在线空载功率")
    boot = pick(args.boot_s, "boot_s", "开机时间")
    off = pick(args.off_w, "off_w", "关机功率")
    if slots is None or idle is None:
        raise ConfigError("须给出 --slots 与 --idle-w（数据集中没有 cpc measure 记录的测量值）")
    prefill = args.prefill_tokens_per_s
    if prefill is None and m.get("prefill_tokens_per_s_est") is not None:
        prefill = m["prefill_tokens_per_s_est"]
        notes.append(f"预填充吞吐由并发 1 时的首 token 时延估计（{prefill} token/s）")
    prof = profile_from_points(load_points(d, where), int(slots), float(idle), args.host_w, prefill, boot, source=d.ref,
                               idle_measured=args.idle_w is None, off_w=off)

    class Flow(list):
        pass

    yaml.SafeDumper.add_representer(Flow, lambda dumper, x: dumper.represent_sequence("tag:yaml.org,2002:seq", x, flow_style=True))
    for k in ("power_w", "tpot_s"):
        prof[k] = [Flow(p) for p in prof[k]]
    text = yaml.safe_dump({args.name: prof}, allow_unicode=True, sort_keys=False, width=200)
    for n in notes:
        text += f"    # {n}\n"
    if m.get("warning"):
        text += f"    # 警告：{m['warning']}\n"
        print(f"警告：{m['warning']}", file=sys.stderr)
    missing = [k for k in ("prefill_tokens_per_s", "boot_s") if k not in prof]
    if missing:
        text += f"    # 须补充：{'、'.join(missing)}（测量数据中没有这些参数）\n"
    print(text, end="")


def cmd_measure(args) -> None:
    """测量站点中一台真实服务器在各级并发下的功率与 token 间隔，写出 power_profile 数据集。"""
    import pyarrow as pa

    from .datasets.build import write_dataset
    from .measure import run_measure

    levels = None if args.concurrency is None else sorted({int(x) for x in args.concurrency.split(",")})
    print(f"测量站点 {args.site} 的真实服务器（每级预热 {args.warmup:g} s、测量 {args.duration:g} s）")

    async def main():
        from .measure import pick_node

        lv = levels
        if lv is None:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as c:
                _, n = await pick_node(c, args.site.rstrip("/"), args.node)
            k, lv = 1, [0]
            while k < n["slots"]:
                lv.append(k)
                k *= 2
            lv.append(n["slots"])
        return await run_measure(args.site, args.node, lv, args.warmup, args.duration, args.input_tokens,
                                 args.output_tokens, args.model, args.boot)

    import httpx

    try:
        meta, res = asyncio.run(main())
    except httpx.HTTPError as e:
        raise StartupError(f"无法访问站点 {args.site}：{type(e).__name__} {e}") from e
    from .measure import flat_power_warning

    warn = flat_power_warning(res)
    if warn:
        meta["warning"] = warn
        print(f"警告：{warn}", file=sys.stderr)
    idle = next((x for x in res if x.concurrency == 0), None)
    if idle is not None and idle.power_w is not None:
        meta["idle_w"] = round(idle.power_w, 1)
    rows = [x for x in res if x.concurrency > 0 and x.power_w is not None and x.itl_s is not None]
    if not rows:
        raise ConfigError("没有可用的测量（功率或 token 间隔缺失）")
    first = min(rows, key=lambda x: x.concurrency)
    if first.ttft_s:
        meta["prefill_tokens_per_s_est"] = round(args.input_tokens / first.ttft_s)
    task = f"synthetic-in{args.input_tokens}-out{args.output_tokens}"
    cols = {
        "device": [args.device] * len(rows), "device_count": [args.device_count] * len(rows),
        "model": [args.model_name] * len(rows), "concurrency": [round(x.busy_mean, 3) for x in rows],
        "power_w": [round(x.power_w, 2) for x in rows], "itl_s": [round(x.itl_s, 6) for x in rows],
        "max_concurrency": [meta["slots"]] * len(rows),
        "throughput_tokens_per_s": [round(x.throughput_tokens_per_s, 2) for x in rows],
        "ttft_s": [round(x.ttft_s, 4) if x.ttft_s is not None else None for x in rows],
        "energy_per_token_j": [round(x.power_w / x.throughput_tokens_per_s, 4) if x.throughput_tokens_per_s else None
                               for x in rows],
        "task": [task] * len(rows),
        "target_concurrency": [x.concurrency for x in rows],
        "power_coverage": [round(x.power_coverage, 4) for x in rows],
        "requests": [x.requests for x in rows],
    }
    spec = [("device", "string", None), ("device_count", "integer", None), ("model", "string", None),
            ("concurrency", "number", None), ("power_w", "number", "W"), ("itl_s", "number", "s"),
            ("max_concurrency", "integer", None), ("throughput_tokens_per_s", "number", "token/s"),
            ("ttft_s", "number", "s"), ("energy_per_token_j", "number", "J"), ("task", "string", None),
            ("target_concurrency", "integer", None), ("power_coverage", "number", None), ("requests", "integer", None)]
    fields = [{"name": n, "type": t, **({"unit": u} if u else {})} for n, t, u in spec]
    out = Path(args.out)
    d = write_dataset(out, name=args.name or out.name, version=datetime.now().strftime("%Y.%m.%d"), kind="power_profile",
                      table=pa.table(cols), fields=fields, fmt="csv", cpc={"measurement": meta},
                      description=f"cpc measure：站点 {meta['site']} 的服务器 {meta['node']}，功率范围 {meta['power_scope']}")
    print(f"已写出数据集 {d.ref}：{out}/（{len(rows)} 级）")
    print(f"生成服务器类型：cpc profile {out} --name <名称>")


# ------------------------------------------------------------------ 仿真


def _run_variant(scenario: str, name: str, routing: dict | None, capacity: dict | None, workload: dict, hours: float,
                 bin_s: float, out: str) -> dict:
    """运行一个变体（可在子进程中执行）：加载场景、覆盖策略与负载参数、仿真并写出结果数据包。"""
    from .config import override_policy
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
        scn.scheduler.routing = override_policy(scn.scheduler.routing, routing["policy"], routing["params"])
    if capacity is not None:
        scn.scheduler.capacity = override_policy(scn.scheduler.capacity, capacity["policy"], capacity["params"])
    r, c = scn.scheduler.routing, scn.scheduler.capacity
    t0 = time.time()
    res = Simulator(scn, make_routing(r.policy, r.params), make_capacity(c.policy, c.params), hours=hours, bin_s=bin_s).run()
    res.summary["wall_s"] = round(time.time() - t0, 2)
    write_run(out, res, scn, {"name": name, "mode": "simulation", "hours": hours, "bin_s": bin_s,
                              "routing": r.model_dump(), "capacity": c.model_dump()})
    return {"variant": name, **res.summary}


def _width(s: str) -> int:
    """终端显示宽度：全角字符计 2。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int, right: bool = False) -> str:
    fill = " " * max(0, n - _width(s))
    return fill + s if right else s + fill


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
    routing = _policy(args.routing, args.routing_params, "--routing-params")
    capacity = _policy(args.capacity, args.capacity_params, "--capacity-params")
    name = f"{args.routing or scn.scheduler.routing.policy}__{args.capacity or scn.scheduler.capacity.policy}"
    out = args.out or f"results/sim-{name}"
    s = _run_variant(args.scenario, name, routing, capacity, _overrides(args), args.hours, args.bin_s, out)
    print(f"仿真完成（{s['wall_s']} 秒）：路由 {s['routing']}，容量 {s['capacity']}，{s['hours']:g} 小时")
    _print_summary(s)
    print(f"结果已写入 {out}/")


def cmd_run(args) -> None:
    """按实验描述并行运行全部变体，写出各变体的结果与对比表。"""
    from .results import load_experiment, write_comparison
    from .sched import make_capacity, make_routing

    exp = load_experiment(args.experiment)
    out = Path(args.out or f"results/{exp.name}")
    variants = [{"name": v.name, "routing": v.routing.model_dump() if v.routing else None,
                 "capacity": v.capacity.model_dump() if v.capacity else None, "workload": v.workload} for v in exp.variants]
    scn = load_scenario(exp.scenario_path())
    for v in variants:  # 在启动子进程之前检查各变体的策略名、参数与负载字段
        try:
            r, c = v["routing"] or scn.scheduler.routing.model_dump(), v["capacity"] or scn.scheduler.capacity.model_dump()
            make_routing(r["policy"], r["params"])
            make_capacity(c["policy"], c["params"])
            for k in v["workload"] or {}:
                if k not in type(scn.workload).model_fields:
                    raise ConfigError(f"workload 没有字段 {k}")
        except ConfigError as e:
            raise ConfigError(f"变体 {v['name']}：{e}") from None
    ctx = multiprocessing.get_context("spawn")  # 子进程不继承父进程的线程与状态
    print(f"实验 {exp.name}：{len(variants)} 个变体，场景 {exp.scenario_path()}，{exp.hours:g} 小时")
    with ProcessPoolExecutor(max_workers=max(1, min(len(variants), args.jobs)), mp_context=ctx) as ex:
        futs = [ex.submit(_run_variant, str(exp.scenario_path()), v["name"], v.get("routing"), v.get("capacity"),
                          v.get("workload", {}), exp.hours, exp.bin_s, str(out / v["name"])) for v in variants]
        rows = [f.result() for f in futs]
    p = write_comparison(out, rows)
    cur = rows[0].get("currency") or ""
    w = max(18, max(_width(r["variant"]) for r in rows) + 2)
    cols = [("完成", 7), ("拒绝", 7), ("SLO", 9), ("TTFT P99", 10), ("设施 kWh", 10), ("电费 " + cur, 10), ("碳 kg", 7)]
    print("  " + _pad("变体", w) + "".join(_pad(h, n, right=True) for h, n in cols))
    for r in rows:
        vals = [f"{r['served']}", f"{r['rejected']}", f"{r['slo_attainment']:.2%}", f"{(r['ttft_p99_s'] or 0):.3f}",
                f"{r['facility_kwh']:.1f}", f"{r['cost']:.1f}", f"{r['carbon_kg']:.1f}"]
        print("  " + _pad(r["variant"], w) + "".join(_pad(v, n, right=True) for v, (_, n) in zip(vals, cols)))
    print(f"结果已写入 {out}/，对比表 {p}")


def cmd_compare(args) -> None:
    """并列比较结果目录（仿真或实验）：总体与各站点的请求、时延与能耗；两个目录时给出第二个与第一个的比值。"""
    from .results import compare_runs

    if len(args.dirs) < 2:
        raise ConfigError("cpc compare 需要至少两个结果目录")
    for d in args.dirs:
        if not (Path(d) / "summary.json").is_file():
            raise ConfigError(f"{d} 不是结果目录（缺少 summary.json）")
    names, rows = compare_runs(args.dirs)
    w = max(12, *(_width(n) + 2 for n in names))
    ratio = len(names) == 2
    print("  " + _pad("范围", 8) + _pad("指标", 18) + "".join(_pad(n, w, right=True) for n in names)
          + (_pad("比值", 8, right=True) if ratio else ""))

    def fmt(v, metric):
        if v is None:
            return "—"
        if metric == "SLO 达成率":
            return f"{v:.1%}"
        return f"{v:d}" if isinstance(v, int) else f"{v:.3f}"

    last = None
    for scope, metric, vals in rows:
        line = "  " + _pad(scope if scope != last else "", 8) + _pad(metric, 18)
        line += "".join(_pad(fmt(v, metric), w, right=True) for v in vals)
        if ratio:
            a, b = vals
            line += _pad(f"{b / a:.2f}" if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a else "—", 8,
                         right=True)
        print(line)
        last = scope


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
    ep = _ep(args, scn)
    config = {"scenario": Path(args.scenario).read_text(encoding="utf-8"), "endpoints": ep.model_dump()}
    _serve(create_app(scn, config=config), *_bind(args, ep.grid))


def fetch_control_config(control: str, timeout_s: float, client=None) -> tuple[Scenario, Endpoints]:
    """从控制节点的电网信号服务取得场景与服务地址（GET /v1/config）；在 timeout_s 内重试连接。"""
    import httpx

    from .common import admin_headers
    from .config import scenario_from_text

    url = control.rstrip("/") + "/v1/config"
    own = client is None
    client = client or httpx.Client(timeout=10.0, trust_env=False)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                r = client.get(url, headers=admin_headers())
                break
            except httpx.TransportError as e:
                if time.monotonic() > deadline:
                    raise StartupError(f"无法连接控制节点 {control}：{e}") from e
                time.sleep(2.0)
    finally:
        if own:
            client.close()
    if r.status_code in (401, 403):
        raise ConfigError(f"控制节点 {control} 拒绝访问配置：管理令牌 CPC_ADMIN_TOKEN 不一致")
    if r.status_code != 200:
        raise ConfigError(f"控制节点 {control} 返回 {r.status_code}：{r.text[:200]}")
    body = r.json()
    scn = scenario_from_text(body["scenario"], f"控制节点 {control} 的场景")
    try:
        ep = Endpoints.model_validate(body["endpoints"])
    except ValueError as e:
        raise ConfigError(f"控制节点 {control} 的服务地址无效：{e}") from e
    return scn, ep


def cmd_site(args) -> None:
    from .site.service import create_app

    if args.control:
        scn, ep = fetch_control_config(args.control, args.startup_timeout)
    else:
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
    params = _json_arg("--params", args.params)
    _serve(create_app(scn, ep, args.policy, params, args.log, args.startup_timeout), *_bind(args, ep.gateway))


def cmd_controller(args) -> None:
    from .controller.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    params = _json_arg("--params", args.params)
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


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"应为数值，实际为 {text!r}") from None


def _positive(text: str) -> float:
    v = _number(text)
    if not v > 0:
        raise argparse.ArgumentTypeError(f"应大于 0，实际为 {text}")
    return v


def _nonnegative(text: str) -> float:
    v = _number(text)
    if not v >= 0:
        raise argparse.ArgumentTypeError(f"应不小于 0，实际为 {text}")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpc", description="算电协同跨数据中心 AI 计算任务调度平台",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd", required=True, metavar="子命令")

    def scenario(sp):
        sp.add_argument("--scenario", "-s", default=DEFAULT_SCENARIO, help=f"场景文件（缺省 {DEFAULT_SCENARIO}）")
        return sp

    def workload(sp):
        g = sp.add_argument_group("负载变换（覆盖场景 workload 中的同名参数）")
        g.add_argument("--start-s", type=_nonnegative, default=None, help="时间窗起点（原始 trace 秒）")
        g.add_argument("--duration-s", type=_positive, default=None, help="时间窗长度（原始 trace 秒）")
        g.add_argument("--time-scale", type=_positive, default=None, help="时间压缩倍数（>1 压缩）")
        g.add_argument("--rate-scale", type=_positive, default=None, help="到达率缩放倍数")

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
    sp.add_argument("--where", nargs="+", default=None, metavar="字段=值",
                    help="选择同一组测量，如 device=H100 device_count=8 model=... task=...")
    sp.add_argument("--slots", type=int, default=None, help="并发槽数（推理引擎的最大批大小）；cpc measure 的数据集可省略")
    sp.add_argument("--idle-w", type=float, default=None, help="服务器在线空载功率（W），作为表的第一个点；cpc measure 的数据集可省略")
    sp.add_argument("--host-w", type=float, default=0.0, help="加到每个测量点上的主机部分功率（W，缺省 0）")
    sp.add_argument("--prefill-tokens-per-s", type=float, default=None, help="预填充吞吐（不在测量中）")
    sp.add_argument("--boot-s", type=float, default=None, help="开机到可服务的时间；cpc measure --boot 的数据集可省略")
    sp.add_argument("--off-w", type=float, default=None,
                    help="关机时的功率（W，缺省 0）；cpc measure --boot 的数据集中记录了实测值")
    sp.add_argument("--name", default="server", help="服务器类型名称（缺省 server）")
    sp.set_defaults(fn=cmd_profile)

    sp = sub.add_parser("compare", help="并列比较结果目录（仿真或实验）的总体与各站点指标")
    sp.add_argument("dirs", nargs="+", help="结果目录（两个或以上；两个时给出比值）")
    sp.set_defaults(fn=cmd_compare)

    sp = sub.add_parser("measure", help="测量站点中一台真实服务器的功率与 token 间隔，写出 power_profile 数据集")
    sp.add_argument("--site", required=True, help="站点代理地址，如 http://10.0.0.11:9100")
    sp.add_argument("--node", default=None, help="被测服务器 id（站点中只有一台开启的真实服务器时可省略）")
    sp.add_argument("--concurrency", default=None, help="各级并发，如 0,1,2,4（缺省 0、1、2、4…直至并发上限）")
    sp.add_argument("--warmup", type=_nonnegative, default=15.0, help="每级的预热时间（秒，缺省 15）")
    sp.add_argument("--duration", type=_positive, default=60.0, help="每级的测量时间（秒，缺省 60）")
    sp.add_argument("--input-tokens", type=int, default=256, help="每个请求的输入长度（缺省 256）")
    sp.add_argument("--output-tokens", type=int, default=256, help="每个请求的输出长度（缺省 256）")
    sp.add_argument("--model", default="cpc", help="请求中的模型名（缺省 cpc）")
    sp.add_argument("--device", required=True, help="设备型号，写入数据集（如 A100、2xE5-2650v2）")
    sp.add_argument("--device-count", type=int, default=1, help="一个推理实例占用的设备数（缺省 1）")
    sp.add_argument("--model-name", required=True, help="推理模型名，写入数据集（如 Qwen2.5-7B-Instruct）")
    sp.add_argument("--boot", action="store_true",
                    help="同时测量关机功率与开机时间（经管理接口关闭再开启服务器，需要 CPC_ADMIN_TOKEN）")
    sp.add_argument("--out", required=True, help="数据集目录")
    sp.add_argument("--name", default=None, help="数据集名称（缺省为目录名）")
    sp.set_defaults(fn=cmd_measure)

    # 仿真
    sp = scenario(sub.add_parser("sim", help="运行一次仿真，结果写为结果数据包"))
    sp.add_argument("--routing", default=None, help="覆盖场景的路由策略")
    sp.add_argument("--routing-params", default=None, help="路由策略参数（JSON）；不给出 --routing 时修改场景中策略的参数")
    sp.add_argument("--capacity", default=None, help="覆盖场景的容量策略")
    sp.add_argument("--capacity-params", default=None, help="容量策略参数（JSON）；不给出 --capacity 时修改场景中策略的参数")
    sp.add_argument("--hours", type=_positive, default=24, help="仿真时长（小时，缺省 24）")
    sp.add_argument("--bin-s", type=_positive, default=300.0, help="时间序列的时间片长度（秒，缺省 300）")
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
    sp.add_argument("--interval", type=_positive, default=None, help="控制周期（秒）")
    sp.add_argument("--speedup", type=_positive, default=None, help="信号时钟加速倍数")
    sp.add_argument("--initial-active", type=int, default=None, help="各站点初始在线的服务器数")
    sp.add_argument("--no-controller", action="store_true", help="不启动控制器")
    sp.add_argument("--log-dir", default="logs", help="日志目录（缺省 logs）")
    sp.set_defaults(fn=cmd_up)

    sp = scenario(sub.add_parser("live", help="运行一次实验：回放负载、采样，结果写为结果数据包"))
    sp.add_argument("--duration", type=_positive, required=True, help="实验时长（秒）")
    sp.add_argument("--endpoints", "-e", default=None, help="已部署服务的地址文件；缺省时在本进程中启动全部服务")
    sp.add_argument("--routing", default=None, help="覆盖场景的路由策略（只用于本进程启动的服务）")
    sp.add_argument("--capacity", default=None, help="覆盖场景的容量策略（只用于本进程启动的服务）")
    sp.add_argument("--interval", type=_positive, default=None, help="控制周期（秒）")
    sp.add_argument("--speedup", type=_positive, default=None, help="信号时钟加速倍数")
    sp.add_argument("--initial-active", type=int, default=None, help="各站点初始在线的服务器数")
    sp.add_argument("--sample-s", type=_positive, default=10.0, help="站点状态与电网信号的采样周期（秒，缺省 10）")
    sp.add_argument("--model", default="cpc", help="请求中的 model 字段（真实推理实例的模型名）")
    sp.add_argument("--no-stream", action="store_true", help="使用非流式请求（不测量首 token 时延）")
    workload(sp)
    sp.add_argument("--out", default=None, help="结果目录（缺省 results/live-<时间>）")
    sp.set_defaults(fn=cmd_live)

    sp = scenario(sub.add_parser("loadgen", help="按场景负载向网关回放请求"))
    endpoints(sp, listen=False)
    sp.add_argument("--duration", type=_positive, default=60, help="回放时长（秒）")
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
    sp.add_argument("--cap-kw", type=_nonnegative, required=True, help="设施功率上限（kW）")
    sp.add_argument("--duration-min", type=_positive, default=60, help="持续时间（信号时间，分钟）")
    sp.add_argument("--kind", default="peak_shaving")
    sp.set_defaults(fn=cmd_event)

    # 服务
    sp = scenario(sub.add_parser("grid", help="电网信号服务（信号时钟的权威来源）"))
    endpoints(sp)
    sp.add_argument("--speedup", type=_positive, default=None, help="信号时钟加速倍数")
    sp.set_defaults(fn=cmd_grid)

    sp = scenario(sub.add_parser("site", help="站点代理"))
    endpoints(sp)
    sp.add_argument("--site", required=True)
    sp.add_argument("--control", default=None,
                    help="从控制节点取得场景与服务地址（电网信号服务的地址，如 http://10.0.0.10:9000），代替 -s 与 -e")
    sp.add_argument("--startup-timeout", type=_positive, default=120.0, help="等待控制节点可达的期限（秒，缺省 120）")
    sp.add_argument("--enable-power-cap", action="store_true", help="通过传感器设置加速卡功率上限（NVML 需要 root）")
    sp.add_argument("--initial-active", type=int, default=None)
    sp.set_defaults(fn=cmd_site)

    sp = scenario(sub.add_parser("gateway", help="路由网关"))
    endpoints(sp)
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None, help="策略参数（JSON）")
    sp.add_argument("--log", default=None, help="请求日志（JSONL）")
    sp.add_argument("--startup-timeout", type=_positive, default=30.0, help="等待电网信号服务可达的期限（秒）")
    sp.set_defaults(fn=cmd_gateway)

    sp = scenario(sub.add_parser("controller", help="容量控制器"))
    endpoints(sp)
    sp.add_argument("--policy", default=None)
    sp.add_argument("--params", default=None, help="策略参数（JSON）")
    sp.add_argument("--interval", type=_positive, default=None, help="控制周期（秒）")
    sp.add_argument("--log", default=None, help="决策日志（JSONL）")
    sp.add_argument("--startup-timeout", type=_positive, default=30.0, help="等待电网信号服务可达的期限（秒）")
    sp.set_defaults(fn=cmd_controller)

    # 功率测量
    sp = sub.add_parser("sensors", help="列出或读取功率传感器")
    sp.add_argument("--list", action="store_true", help="列出全部传感器类型及本机是否具备其前提条件")
    sp.add_argument("--types", default=None, help="逗号分隔的传感器类型，如 nvml,rapl")
    sp.add_argument("--spec", default=None, help="传感器配置（YAML，含参数）")
    sp.add_argument("--count", type=int, default=3)
    sp.add_argument("--interval", type=_positive, default=1.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_sensors)

    sp = sub.add_parser("exporter", help="通用功率导出器（Prometheus 格式）")
    sp.add_argument("--port", type=int, default=9400)
    sp.add_argument("--host", default=None)
    sp.add_argument("--site", default=None, help="附加 site 标签")
    sp.add_argument("--types", default=None, help="逗号分隔的传感器类型")
    sp.add_argument("--spec", default=None, help="传感器配置（YAML）")
    sp.add_argument("--interval", type=_positive, default=1.0, help="采样周期（秒）")
    sp.set_defaults(fn=cmd_exporter)
    return p


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (ConfigError, StartupError, SensorError, DatasetError) as e:
        sys.stdout.flush()  # 先输出已打印的内容，错误信息在最后
        print(f"cpc {args.cmd}：{e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main(sys.argv[1:])
