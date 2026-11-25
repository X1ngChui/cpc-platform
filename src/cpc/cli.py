"""Command line: cpc <command> (same as python -m cpc <command>).

Preparation:  check (check a scenario and its data), data (datasets), profile (server type from measurements)
Simulation:   sim (one run), run (variants of an experiment), compare (compare result directories)
Experiments:  up (all services in one process), live (run an experiment and write its result),
              event (inject a demand-response event), measure (measure a real server)
Services:     grid, site, gateway, controller (one service each)
Sensors:      sensors (list or read power sensors)

Commands that reach other services (event and the single services) need an endpoints file (-e or
CPC_ENDPOINTS) or --local for the local addresses. Configuration, data and startup errors print the
reason and exit with status 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import multiprocessing
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from .common import StartupError, setup_logging
from .config import ConfigError, Endpoints, Scenario, default_local_endpoints, host_port, load_endpoints, load_scenario
from .datasets import DatasetError
from .sensors.base import SensorError

log = logging.getLogger("cpc")
DEFAULT_SCENARIO = "configs/scenario.yaml"
WORKLOAD_KEYS = ("start_s", "duration_s", "time_scale", "rate_scale")


def _scn(args) -> Scenario:
    scn = load_scenario(args.scenario)
    for key in WORKLOAD_KEYS:
        if (v := getattr(args, key, None)) is not None:
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
    h, p = host_port(url)
    return args.host or h, args.port or p


def _json_arg(flag: str, text: str | None) -> dict | None:
    """Parse a command-line argument holding a JSON object."""
    if text is None:
        return None
    try:
        v = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{flag} is not valid JSON (character {e.colno}: {e.msg}); "
                          f"example: {flag} '{{\"max_wait_s\": 1.0}}' (double quotes around keys and strings)") from None
    if not isinstance(v, dict):
        raise ConfigError(f"{flag} should be a JSON object, e.g. '{{\"max_wait_s\": 1.0}}'")
    return v


def _policy(name: str | None, params: str | None, flag: str) -> dict | None:
    """--routing / --capacity with their parameters; parameters alone update the policy of the scenario."""
    p = _json_arg(flag, params)
    return None if name is None and p is None else {"policy": name, "params": p}


def _width(s: str) -> int:
    """Display width in a terminal: wide characters count 2."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, n: int, right: bool = False) -> str:
    fill = " " * max(0, n - _width(s))
    return fill + s if right else s + fill


def _table(header: list[tuple[str, int]], rows: list[list[str]], first: int) -> None:
    """Print a table: the first column left-aligned in width first, the others right-aligned."""
    (h0, _), *rest = header
    print("  " + _pad(h0, first) + "".join(_pad(h, n, right=True) for h, n in rest))
    for r in rows:
        print("  " + _pad(r[0], first) + "".join(_pad(v, n, right=True) for v, (_, n) in zip(r[1:], rest)))


# ------------------------------------------------------------------ preparation


def cmd_check(args) -> None:
    """Check a scenario: configuration, datasets, policies and parameters, workload window; print a summary."""
    from .datasets.workload import load_workload
    from .grid.signals import GridModel, SignalUnavailable
    from .models import idle_w, peak_w, rtt_matrix
    from .sched import make_capacity, make_dispatch, make_power, make_routing

    scn = _scn(args)
    print(f"Scenario {scn.name} ({Path(args.scenario).resolve()})")
    print(f"  signal clock starts {scn.clock.start.isoformat()}, speed-up x{scn.clock.speedup:g} in live experiments")
    print("\nServer types")
    print(f"  {'name':<10s} {'slots':>6s} {'idle W':>8s} {'peak W':>8s} {'peak W/slot':>12s} {'boot s':>7s}  description")
    for name, p in scn.node_profiles.items():
        print(f"  {name:<10s} {p.slots:>6d} {idle_w(p):>8.0f} {peak_w(p):>8.0f} {peak_w(p) / p.slots:>12.1f} "
              f"{p.boot_s:>7.0f}  {p.description}")
    print("\nSites")
    print(f"  {'site':<6s} {'region':<16s} {'PUE':>5s} {'servers':>8s} {'slots':>6s} {'peak facility kW':>17s}  "
          "pools; dispatch/power policy")
    for s in scn.sites:
        n = slots = peak = 0
        pools = []
        for p in s.pools:
            prof, k = scn.node_profiles[p.profile], p.size
            n += k
            slots += k * prof.slots if p.backend == "emulated" else sum(u.max_inflight for u in p.upstreams)
            peak += k * peak_w(prof)
            pools.append(f"{p.id}:{p.profile}x{k}{' (real)' if p.backend == 'openai' else ''}")
        sched = s.scheduler
        make_dispatch(sched.dispatch.policy, sched.dispatch.params)
        make_power(sched.power.policy, sched.power.params)
        print(f"  {s.id:<6s} {s.region:<16s} {s.pue:>5.2f} {n:>8d} {slots:>6d} {peak * s.pue / 1000:>17.1f}  "
              f"{', '.join(pools)}; {sched.dispatch.policy}/{sched.power.policy}")
    m = rtt_matrix(scn)
    print("\nRound-trip times (ms, source -> site)")
    print("  " + " " * 8 + "".join(f"{s.id:>8s}" for s in scn.sites))
    for src in scn.sources:
        print(f"  {src.id:<8s}" + "".join(f"{m[src.id][s.id]:>8.1f}" for s in scn.sites) + f"   (share {src.share:g})")
    g = GridModel(scn)
    print(f"\nGrid signals (price {g.currency}/kWh, carbon intensity kgCO2/kWh): {g.sources()}")
    day = (datetime.fromisoformat(args.date).replace(tzinfo=scn.clock.start.tzinfo) if args.date
           else scn.clock.start.replace(hour=0, minute=0, second=0, microsecond=0))
    print(f"  {day:%Y-%m-%d} (price/period/carbon intensity)")
    print("  time  " + "".join(f"{s.id:>24s}" for s in scn.sites))
    for h in range(0, 24, args.step):
        t = day + timedelta(hours=h, minutes=30)
        cells = []
        for s in scn.sites:
            try:
                x = g.signal(s.id, t)
                cells.append(f"{x.price:.3f}/{x.price_label or '-'}/{x.carbon_intensity:.3f}")
            except SignalUnavailable:
                cells.append("missing")
        print(f"  {h:02d}:30 " + "".join(f"{c:>24s}" for c in cells))
    sm = load_workload(scn).summary()
    print(f"\nWorkload {sm['ref']}: {sm['requests']} requests, {sm['duration_s'] / 3600:.2f} h, {sm['mean_rps']} requests/s, "
          f"mean input / output tokens {sm['input_tokens_mean']} / {sm['output_tokens_mean']}")
    r, c = scn.scheduler.routing, scn.scheduler.capacity
    make_routing(r.policy, r.params)
    make_capacity(c.policy, c.params)
    print(f"\nGlobal policies: routing {r.policy} {r.params or ''}, capacity {c.policy} {c.params or ''}")
    print("\nScenario check passed")


def cmd_data(args) -> None:
    """Datasets: kind specifications, validation and statistics, descriptor summary."""
    from .datasets import KINDS, load_descriptor
    from .datasets.table import validate

    if args.data_cmd == "kinds":
        for kind, spec in KINDS.items():
            print(f"{kind} (resource {spec.resource}): {spec.description}")
            for tag, fields in (("core", spec.core), ("conv", spec.conventional)):
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
        print(f"{d.ref} ({d.kind}) is valid: " + ", ".join(f"{k} {v} rows" for k, v in rows.items()))
        print(json.dumps(_data_stats(d), ensure_ascii=False, indent=2))


def _data_stats(d) -> dict:
    match d.kind:
        case "workload":
            from .datasets.workload import load_trace

            return load_trace(d).summary()
        case "grid_signal":
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

    groups = Counter(f"device={p.device} device_count={p.device_count} model={p.model}" for p in load_points(d))
    return {"measurements_by_group": dict(groups)}


def cmd_profile(args) -> None:
    """Server type from a power-profile dataset, printed as YAML for node_profiles of a scenario."""
    import yaml

    from .datasets import load_descriptor
    from .datasets.power import load_points, profile_from_points

    d = load_descriptor(args.dataset)
    where = {}
    for kv in args.where or []:
        if "=" not in kv:
            raise ConfigError(f"every --where item must be field=value, got {kv}")
        k, v = kv.split("=", 1)
        where[k] = int(v) if k == "device_count" else v
    m = (d.cpc or {}).get("measurement") or {}  # conditions and results written by cpc measure
    notes = []

    def pick(arg, key, label):
        if arg is not None:
            return arg
        if m.get(key) is not None:
            notes.append(f"{label} from the measurement ({key} = {m[key]})")
            return m[key]
        return None

    slots = pick(args.slots, "slots", "concurrency slots")
    idle = pick(args.idle_w, "idle_w", "online idle power")
    boot = pick(args.boot_s, "boot_s", "boot time")
    off = pick(args.off_w, "off_w", "power while off")
    if slots is None or idle is None:
        raise ConfigError("give --slots and --idle-w (the dataset has no values recorded by cpc measure)")
    if (prefill := args.prefill_tokens_per_s) is None and (prefill := m.get("prefill_tokens_per_s_est")) is not None:
        notes.append(f"prefill throughput estimated from the TTFT at concurrency 1 ({prefill} token/s)")
    prof = profile_from_points(load_points(d, where), int(slots), float(idle), args.host_w, prefill, boot, source=d.ref,
                               idle_measured=args.idle_w is None, off_w=off)

    class Flow(list):
        pass

    yaml.SafeDumper.add_representer(Flow, lambda dumper, x: dumper.represent_sequence("tag:yaml.org,2002:seq", x, flow_style=True))
    for k in ("power_w", "tpot_s"):
        prof[k] = [Flow(p) for p in prof[k]]
    text = yaml.safe_dump({args.name: prof}, allow_unicode=True, sort_keys=False, width=200)
    text += "".join(f"    # {n}\n" for n in notes)
    if warning := m.get("warning"):
        text += f"    # warning: {warning}\n"
        print(f"warning: {warning}", file=sys.stderr)
    if missing := [k for k in ("prefill_tokens_per_s", "boot_s") if k not in prof]:
        text += f"    # to add: {', '.join(missing)} (not in the measurements)\n"
    print(text, end="")


MEASURE_FIELDS = [("device", "string", None), ("device_count", "integer", None), ("model", "string", None),
                  ("concurrency", "number", None), ("power_w", "number", "W"), ("itl_s", "number", "s"),
                  ("max_concurrency", "integer", None), ("throughput_tokens_per_s", "number", "token/s"),
                  ("ttft_s", "number", "s"), ("energy_per_token_j", "number", "J"), ("task", "string", None),
                  ("target_concurrency", "integer", None), ("power_coverage", "number", None), ("requests", "integer", None)]


def cmd_measure(args) -> None:
    """Measure power and inter-token latency of one real server of a site; write a power_profile dataset."""
    import httpx
    import pyarrow as pa

    from .datasets.build import write_dataset
    from .measure import flat_power_warning, pick_node, run_measure

    levels = None if args.concurrency is None else sorted({int(x) for x in args.concurrency.split(",")})
    print(f"Measuring a real server of site {args.site} (warm-up {args.warmup:g} s, measurement {args.duration:g} s per level)")

    async def main():
        lv = levels
        if lv is None:  # 0, 1, 2, 4, ... up to the concurrency limit
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as c:
                _, n = await pick_node(c, args.site.rstrip("/"), args.node)
            lv = [0, *(2 ** k for k in range(n["slots"].bit_length()) if 2 ** k < n["slots"]), n["slots"]]
        return await run_measure(args.site, args.node, lv, args.warmup, args.duration, args.input_tokens,
                                 args.output_tokens, args.model, args.boot)

    try:
        meta, res = asyncio.run(main())
    except httpx.HTTPError as e:
        raise StartupError(f"cannot reach site {args.site}: {type(e).__name__} {e}") from e
    if warn := flat_power_warning(res):
        meta["warning"] = warn
        print(f"warning: {warn}", file=sys.stderr)
    if (idle := next((x for x in res if x.concurrency == 0), None)) is not None and idle.power_w is not None:
        meta["idle_w"] = round(idle.power_w, 1)
    if not (rows := [x for x in res if x.concurrency > 0 and x.power_w is not None and x.itl_s is not None]):
        raise ConfigError("no usable measurement (power or inter-token latency missing)")
    if (first := min(rows, key=lambda x: x.concurrency)).ttft_s:
        meta["prefill_tokens_per_s_est"] = round(args.input_tokens / first.ttft_s)
    n = len(rows)
    cols = {
        "device": [args.device] * n, "device_count": [args.device_count] * n, "model": [args.model_name] * n,
        "concurrency": [round(x.busy_mean, 3) for x in rows], "power_w": [round(x.power_w, 2) for x in rows],
        "itl_s": [round(x.itl_s, 6) for x in rows], "max_concurrency": [meta["slots"]] * n,
        "throughput_tokens_per_s": [round(x.throughput_tokens_per_s, 2) for x in rows],
        "ttft_s": [None if x.ttft_s is None else round(x.ttft_s, 4) for x in rows],
        "energy_per_token_j": [round(x.power_w / x.throughput_tokens_per_s, 4) if x.throughput_tokens_per_s else None
                               for x in rows],
        "task": [f"synthetic-in{args.input_tokens}-out{args.output_tokens}"] * n,
        "target_concurrency": [x.concurrency for x in rows], "power_coverage": [round(x.power_coverage, 4) for x in rows],
        "requests": [x.requests for x in rows],
    }
    fields = [{"name": name, "type": t, **({"unit": u} if u else {})} for name, t, u in MEASURE_FIELDS]
    out = Path(args.out)
    d = write_dataset(out, name=args.name or out.name, version=datetime.now().strftime("%Y.%m.%d"), kind="power_profile",
                      table=pa.table(cols), fields=fields, fmt="csv", cpc={"measurement": meta},
                      description=f"cpc measure: server {meta['node']} of site {meta['site']}, power scope {meta['power_scope']}")
    print(f"Wrote dataset {d.ref}: {out}/ ({n} levels)")
    print(f"Server type: cpc profile {out} --name <name>")


# ------------------------------------------------------------------ simulation


def _set_workload(scn: Scenario, workload: dict, what: str) -> None:
    for k, v in workload.items():
        if k not in type(scn.workload).model_fields:
            raise ConfigError(f"{what}: workload has no field {k}")
        if v is not None:
            setattr(scn.workload, k, v)


def _run_variant(scenario: str, name: str, routing: dict | None, capacity: dict | None, workload: dict, hours: float,
                 bin_s: float, out: str) -> dict:
    """Run one variant (possibly in a subprocess): load the scenario, apply the overrides, simulate, write the result."""
    from .config import override_policy
    from .results import write_run
    from .sched import make_capacity, make_routing
    from .sim.des import Simulator

    scn = load_scenario(scenario)
    _set_workload(scn, workload, f"variant {name}")
    sched = scn.scheduler
    if routing is not None:
        sched.routing = override_policy(sched.routing, routing["policy"], routing["params"])
    if capacity is not None:
        sched.capacity = override_policy(sched.capacity, capacity["policy"], capacity["params"])
    r, c = sched.routing, sched.capacity
    t0 = time.time()
    res = Simulator(scn, make_routing(r.policy, r.params), make_capacity(c.policy, c.params), hours=hours, bin_s=bin_s).run()
    res.summary["wall_s"] = round(time.time() - t0, 2)
    write_run(out, res, scn, {"name": name, "mode": "simulation", "hours": hours, "bin_s": bin_s,
                              "routing": r.model_dump(), "capacity": c.model_dump()})
    return {"variant": name, **res.summary}


def _print_summary(s: dict) -> None:
    cur = s.get("currency") or ""
    keys = [("arrived", "arrived"), ("served", "served"), ("rejected", "rejected by sites"), ("unrouted", "unrouted"),
            ("failed", "failed"), ("unfinished", "unfinished at the end")]
    print("  requests: " + ", ".join(f"{label} {s[k]}" for k, label in keys if k in s))
    if s.get("ttft_p99_s") is not None:
        print(f"  SLO attainment {s['slo_attainment']:.2%}; TTFT P50 {s['ttft_p50_s']:.3f} s, P99 {s['ttft_p99_s']:.3f} s")
    print(f"  facility energy {s['facility_kwh']:.3f} kWh (IT {s['it_kwh']:.3f} kWh), cost {s['cost']:.2f} {cur}, "
          f"carbon {s['carbon_kg']:.2f} kg, {s['wh_per_request']:.2f} Wh per request")
    if s.get("signal_coverage") not in (None, 1.0):
        print(f"  note: only {s['signal_coverage']:.1%} of the facility energy had grid signals; "
              "the rest is not in cost and carbon")


def cmd_sim(args) -> None:
    scn = load_scenario(args.scenario)
    routing = _policy(args.routing, args.routing_params, "--routing-params")
    capacity = _policy(args.capacity, args.capacity_params, "--capacity-params")
    name = f"{args.routing or scn.scheduler.routing.policy}__{args.capacity or scn.scheduler.capacity.policy}"
    out = args.out or f"results/sim-{name}"
    s = _run_variant(args.scenario, name, routing, capacity, _overrides(args), args.hours, args.bin_s, out)
    print(f"Simulation finished ({s['wall_s']} s): routing {s['routing']}, capacity {s['capacity']}, {s['hours']:g} h")
    _print_summary(s)
    print(f"Result written to {out}/")


def cmd_run(args) -> None:
    """Run all variants of an experiment in parallel; write their results and a comparison table."""
    from .results import load_experiment, write_comparison
    from .sched import make_capacity, make_routing

    exp = load_experiment(args.experiment)
    out = Path(args.out or f"results/{exp.name}")
    variants = [{"name": v.name, "routing": v.routing.model_dump() if v.routing else None,
                 "capacity": v.capacity.model_dump() if v.capacity else None, "workload": v.workload} for v in exp.variants]
    scn = load_scenario(exp.scenario_path())
    for v in variants:  # check policies, parameters and workload fields before starting subprocesses
        try:
            r = v["routing"] or scn.scheduler.routing.model_dump()
            c = v["capacity"] or scn.scheduler.capacity.model_dump()
            make_routing(r["policy"], r["params"])
            make_capacity(c["policy"], c["params"])
            _set_workload(scn.model_copy(deep=True), v["workload"], f"variant {v['name']}")
        except ConfigError as e:
            raise ConfigError(f"variant {v['name']}: {e}") from None
    ctx = multiprocessing.get_context("spawn")  # subprocesses do not inherit threads and state
    print(f"Experiment {exp.name}: {len(variants)} variants, scenario {exp.scenario_path()}, {exp.hours:g} h")
    with ProcessPoolExecutor(max_workers=max(1, min(len(variants), args.jobs)), mp_context=ctx) as ex:
        futs = [ex.submit(_run_variant, str(exp.scenario_path()), v["name"], v["routing"], v["capacity"], v["workload"],
                          exp.hours, exp.bin_s, str(out / v["name"])) for v in variants]
        rows = [f.result() for f in futs]
    p = write_comparison(out, rows)
    cur = rows[0].get("currency") or ""
    _table([("variant", 0), ("served", 8), ("rejected", 10), ("SLO", 9), ("TTFT P99", 10), ("facility kWh", 14),
            (f"cost {cur}", 10), ("carbon kg", 11)],
           [[r["variant"], f"{r['served']}", f"{r['rejected']}", f"{r['slo_attainment']:.2%}", f"{r['ttft_p99_s'] or 0:.3f}",
             f"{r['facility_kwh']:.1f}", f"{r['cost']:.1f}", f"{r['carbon_kg']:.1f}"] for r in rows],
           max(18, max(_width(r["variant"]) for r in rows) + 2))
    print(f"Results written to {out}/, comparison {p}")


def cmd_compare(args) -> None:
    """Compare result directories (simulation or live) side by side, in total and per site; with two
    directories also the ratio of the second to the first."""
    from .results import compare_runs

    if len(args.dirs) < 2:
        raise ConfigError("cpc compare needs at least two result directories")
    if missing := [d for d in args.dirs if not (Path(d) / "summary.json").is_file()]:
        raise ConfigError(f"{missing[0]} is not a result directory (summary.json is missing)")
    names, rows = compare_runs(args.dirs)
    w = max(12, *(_width(n) + 2 for n in names))
    ratio = len(names) == 2

    def fmt(v, metric):
        if v is None:
            return "-"
        if metric == "SLO attainment":
            return f"{v:.1%}"
        return f"{v:d}" if isinstance(v, int) else f"{v:.3f}"

    def quotient(a, b) -> str:
        ok = isinstance(a, (int, float)) and isinstance(b, (int, float)) and a
        return f"{b / a:.2f}" if ok else "-"

    print("  " + _pad("scope", 8) + _pad("metric", 23) + "".join(_pad(n, w, right=True) for n in names)
          + (_pad("ratio", 8, right=True) if ratio else ""))
    last = None
    for scope, metric, vals in rows:
        line = "  " + _pad(scope if scope != last else "", 8) + _pad(metric, 23)
        line += "".join(_pad(fmt(v, metric), w, right=True) for v in vals)
        print(line + (_pad(quotient(*vals), 8, right=True) if ratio else ""))
        last = scope


# ------------------------------------------------------------------ experiments


def cmd_up(args) -> None:
    """Start the grid-signal service, all site agents, the gateway and the controller in one process,
    until Ctrl+C or until a service exits."""
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
        print(f"[cpc up] scenario {scn.name}: {len(scn.sites)} sites, signal clock speed-up x{scn.clock.speedup:g}")
        for app, url in apps:
            print(f"  {app.title:<36s} {url}")
        print(f"  logs in {log_dir.resolve()}; Ctrl+C to stop")
        exited = await run.wait()
        await run.stop()
        print(f"[cpc up] services exited unexpectedly: {exited}", file=sys.stderr)
        return 1

    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
    except RuntimeError as e:
        raise StartupError(str(e)) from e


def cmd_live(args) -> None:
    """Run an experiment: without an endpoints file all services start in this process; replay the
    workload, sample and write the result package."""
    from .launch import Running, build_apps
    from .live import run_live
    from .results import write_run

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    out = Path(args.out or f"results/live-{datetime.now():%Y%m%d-%H%M%S}")
    if args.endpoints and args.local:
        raise ConfigError("give -e or --local, not both")
    local = not (args.endpoints or args.local)  # start the services in this process
    ep = load_endpoints(args.endpoints, scn) if args.endpoints else default_local_endpoints(scn)
    if not local and (args.routing or args.capacity):
        raise ConfigError("deployed services use the policies they were started with; switch them with /admin/policy")
    for name, attr in ((args.routing, "routing"), (args.capacity, "capacity")):
        if name:
            setattr(scn.scheduler, attr, type(scn.scheduler.routing)(policy=name))

    async def main():
        run = None
        if local:
            apps = build_apps(scn, ep, out / "logs", interval=args.interval, initial_active=args.initial_active)
            run = Running(apps)
            await run.start()
            print(f"[cpc live] started {len(apps)} services in this process")
        print(f"[cpc live] replaying the workload for {args.duration:g} s (sampling every {args.sample_s:g} s) ...")
        try:
            return await run_live(scn, ep, args.duration, sample_s=args.sample_s, model=args.model, stream=not args.no_stream,
                                  decisions_log=out / "logs" / "controller.jsonl" if local else None)
        finally:
            if run is not None:
                await run.stop()

    try:
        res, pol = asyncio.run(main())
    except RuntimeError as e:
        raise StartupError(str(e)) from e
    write_run(out, res, scn, {"name": out.name, "mode": "live", "duration_s": args.duration, "sample_s": args.sample_s,
                              "endpoints": None if local else ep.model_dump(), **pol})
    print(f"Experiment finished: routing {pol['routing']}, capacity {pol['capacity']}, {args.duration:g} s")
    _print_summary(res.summary)
    print(f"Result written to {out}/")


def cmd_event(args) -> None:
    import httpx

    from .common import admin_headers

    scn = _scn(args)
    ep = _ep(args, scn)
    body = {"site": args.site, "cap_kw": args.cap_kw, "duration_min": args.duration_min, "kind": args.kind}
    try:
        r = httpx.post(ep.grid + "/v1/events", json=body, headers=admin_headers(), trust_env=False)
    except httpx.HTTPError as e:
        raise StartupError(f"cannot reach the grid-signal service {ep.grid}: {type(e).__name__} {e}") from e
    if r.status_code != 200:
        raise ConfigError(f"the grid-signal service rejected the event ({r.status_code}): {r.text[:300]}")
    print(r.text)


# ------------------------------------------------------------------ services


def cmd_grid(args) -> None:
    from .grid.service import create_app

    scn = _scn(args)
    if args.speedup is not None:
        scn.clock.speedup = args.speedup
    ep = _ep(args, scn)
    config = {"scenario": Path(args.scenario).read_text(encoding="utf-8"), "endpoints": ep.model_dump()}
    _serve(create_app(scn, config=config), *_bind(args, ep.grid))


def fetch_control_config(control: str, timeout_s: float, client=None) -> tuple[Scenario, Endpoints]:
    """Scenario and service addresses from the grid-signal service of the control node (GET /v1/config),
    retrying the connection for timeout_s."""
    import httpx

    from .common import admin_headers
    from .config import scenario_from_text, validate

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
                    raise StartupError(f"cannot connect to the control node {control}: {e}") from e
                time.sleep(2.0)
    finally:
        if own:
            client.close()
    if r.status_code in (401, 403):
        raise ConfigError(f"the control node {control} refused the configuration: CPC_ADMIN_TOKEN differs")
    if r.status_code != 200:
        raise ConfigError(f"the control node {control} returned {r.status_code}: {r.text[:200]}")
    body = r.json()
    scn = scenario_from_text(body["scenario"], f"scenario of the control node {control}")
    return scn, validate(Endpoints, body["endpoints"], f"service addresses of the control node {control}")


def cmd_site(args) -> None:
    from .site.service import create_app

    if args.control:
        scn, ep = fetch_control_config(args.control, args.startup_timeout)
    else:
        scn = _scn(args)
        ep = _ep(args, scn)
    if args.site not in ep.sites:
        raise ConfigError(f"site {args.site} is not in the scenario; available: {sorted(ep.sites)}")
    app = create_app(scn, args.site, enable_power_cap=args.enable_power_cap, initial_active=args.initial_active)
    _serve(app, *_bind(args, ep.sites[args.site]))


def cmd_gateway(args) -> None:
    from .gateway.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    _serve(create_app(scn, ep, args.policy, _json_arg("--params", args.params), args.log, args.startup_timeout),
           *_bind(args, ep.gateway))


def cmd_controller(args) -> None:
    from .controller.service import create_app

    scn = _scn(args)
    ep = _ep(args, scn)
    _serve(create_app(scn, ep, args.policy, _json_arg("--params", args.params), args.interval, args.log,
                      args.startup_timeout), *_bind(args, ep.controller))


# ------------------------------------------------------------------ sensors


def _sensors_from_args(args):
    """Sensors from --spec (YAML with a list under sensors) or --types (comma-separated type names); exactly one."""
    from .config import read_yaml
    from .sensors import make_sensor

    if args.spec and args.types:
        raise ConfigError("give --spec or --types, not both")
    if args.spec:
        return [make_sensor(s) for s in read_yaml(Path(args.spec), "sensor file").get("sensors", [])]
    if args.types:
        return [make_sensor(t) for t in args.types.split(",")]
    raise ConfigError("choose sensors with --types or --spec (cpc sensors --list shows the types and their prerequisites)")


def cmd_sensors(args) -> None:
    """List the sensor types and whether this host meets their prerequisites, or read sensors and print the readings."""
    from .health import HealthBoard
    from .sensors import SENSORS, EnergyIntegrator, read_all

    if args.list:
        for name, cls in SENSORS.all().items():
            print(f"{name:<14s} {cls.requires:<56s} {'available' if cls.detect() else ''}")
        return
    sensors = _sensors_from_args(args)
    print("sensors:", [s.id for s in sensors])
    integ, board = EnergyIntegrator(), HealthBoard("sensor", None)
    for k in range(args.count):
        rs = integ.update(read_all(sensors, board))
        for name, h in board.items.items():
            if not h.up:
                print(f"  {name} failed: {h.last_error}")
        if args.json:
            print(json.dumps([r.to_dict() for r in rs], ensure_ascii=False))
        else:
            for r in rs:
                pw = "-" if r.power_w is None else f"{r.power_w:8.2f} W"
                en = "-" if r.energy_j is None else f"{r.energy_j:12.1f} J"
                ut = "" if r.util_pct is None else f"{r.util_pct:5.1f} %"
                err = f"  errors {r.errors}" if r.errors else ""
                print(f"  [{r.sensor}] {r.device:<14s} {r.kind:<8s} {r.vendor:<8s} {r.model[:28]:<28s} {pw:>11s} {en:>15s} {ut}{err}")
        if k + 1 < args.count:
            time.sleep(args.interval)


# ------------------------------------------------------------------ parser


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"should be a number, got {text!r}") from None


def _positive(text: str) -> float:
    if not (v := _number(text)) > 0:
        raise argparse.ArgumentTypeError(f"should be greater than 0, got {text}")
    return v


def _nonnegative(text: str) -> float:
    if not (v := _number(text)) >= 0:
        raise argparse.ArgumentTypeError(f"should be at least 0, got {text}")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpc", description="Energy-aware scheduling of AI inference across data centres",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def scenario(sp):
        sp.add_argument("--scenario", "-s", default=DEFAULT_SCENARIO, help=f"scenario file (default {DEFAULT_SCENARIO})")
        return sp

    def workload(sp):
        g = sp.add_argument_group("workload transformation (overrides the scenario workload)")
        g.add_argument("--start-s", type=_nonnegative, default=None, help="window start (original trace seconds)")
        g.add_argument("--duration-s", type=_positive, default=None, help="window length (original trace seconds)")
        g.add_argument("--time-scale", type=_positive, default=None, help="time compression factor (>1 compresses)")
        g.add_argument("--rate-scale", type=_positive, default=None, help="arrival-rate factor")

    def endpoints(sp, local=True, listen=True):
        sp.add_argument("--endpoints", "-e", default=None, help="endpoints file (or the environment variable CPC_ENDPOINTS)")
        if local:
            sp.add_argument("--local", action="store_true", help="use the local addresses (the ports of cpc up)")
        if listen:
            sp.add_argument("--host", default=None, help="listen address (0.0.0.0 in containers)")
            sp.add_argument("--port", type=int, default=None, help="listen port")

    def policies(sp, note=""):
        sp.add_argument("--routing", default=None, help=f"routing policy instead of the scenario's{note}")
        sp.add_argument("--capacity", default=None, help=f"capacity policy instead of the scenario's{note}")
        sp.add_argument("--interval", type=_positive, default=None, help="control period (s)")
        sp.add_argument("--speedup", type=_positive, default=None, help="signal clock speed-up")
        sp.add_argument("--initial-active", type=int, default=None, help="servers online at start in every site")

    # preparation
    sp = scenario(sub.add_parser("check", help="check a scenario and its data; print sites, latencies, signals and workload"))
    sp.add_argument("--date", default=None, help="day of the signal table (YYYY-MM-DD, default: day of the clock start)")
    sp.add_argument("--step", type=int, default=3, help="hours between rows of the signal table (default 3)")
    workload(sp)
    sp.set_defaults(fn=cmd_check)

    dp = sub.add_parser("data", help="datasets: kind specifications, validation and statistics, descriptor summary")
    dsub = dp.add_subparsers(dest="data_cmd", required=True, metavar="action")
    dsub.add_parser("kinds", help="core and conventional fields of every dataset kind").set_defaults(fn=cmd_data)
    for name, h in (("check", "validate datasets completely and print statistics"),
                    ("show", "print the descriptor summary (sources, licences, resources, fields)")):
        x = dsub.add_parser(name, help=h)
        x.add_argument("paths", nargs="+", help="dataset directories or descriptors")
        x.set_defaults(fn=cmd_data)

    sp = sub.add_parser("profile", help="server type from a power-profile dataset (printed as YAML)")
    sp.add_argument("dataset", help="power_profile dataset directory or descriptor")
    sp.add_argument("--where", nargs="+", default=None, metavar="field=value",
                    help="select one group of measurements, e.g. device=H100 device_count=8 model=... task=...")
    sp.add_argument("--slots", type=int, default=None, help="concurrency slots (largest batch of the engine); "
                                                            "optional for datasets of cpc measure")
    sp.add_argument("--idle-w", type=float, default=None, help="online idle power (W), the first table point; "
                                                               "optional for datasets of cpc measure")
    sp.add_argument("--host-w", type=float, default=0.0, help="host power added to every point (W, default 0)")
    sp.add_argument("--prefill-tokens-per-s", type=float, default=None, help="prefill throughput (not measured)")
    sp.add_argument("--boot-s", type=float, default=None, help="time from power-on to service; "
                                                               "optional for datasets of cpc measure --boot")
    sp.add_argument("--off-w", type=float, default=None, help="power while off (W, default 0); "
                                                              "measured by cpc measure --boot")
    sp.add_argument("--name", default="server", help="name of the server type (default server)")
    sp.set_defaults(fn=cmd_profile)

    sp = sub.add_parser("measure", help="measure power and inter-token latency of a real server; write a power_profile dataset")
    sp.add_argument("--site", required=True, help="site-agent address, e.g. http://10.0.0.11:9100")
    sp.add_argument("--node", default=None, help="server to measure (optional with one real server that is on)")
    sp.add_argument("--concurrency", default=None, help="levels, e.g. 0,1,2,4 (default 0, 1, 2, 4, ... up to the limit)")
    sp.add_argument("--warmup", type=_nonnegative, default=15.0, help="warm-up per level (s, default 15)")
    sp.add_argument("--duration", type=_positive, default=60.0, help="measurement per level (s, default 60)")
    sp.add_argument("--input-tokens", type=int, default=256, help="input length of each request (default 256)")
    sp.add_argument("--output-tokens", type=int, default=256, help="output length of each request (default 256)")
    sp.add_argument("--model", default="cpc", help="model name in the requests (default cpc)")
    sp.add_argument("--device", required=True, help="device model written to the dataset (e.g. A100, 2xE5-2650v2)")
    sp.add_argument("--device-count", type=int, default=1, help="devices of one inference instance (default 1)")
    sp.add_argument("--model-name", required=True, help="inference model written to the dataset (e.g. Qwen2.5-7B-Instruct)")
    sp.add_argument("--boot", action="store_true", help="also measure power while off and boot time (switches the "
                                                        "server off and on through the admin interface; CPC_ADMIN_TOKEN)")
    sp.add_argument("--out", required=True, help="dataset directory")
    sp.add_argument("--name", default=None, help="dataset name (default: the directory name)")
    sp.set_defaults(fn=cmd_measure)

    # simulation
    sp = scenario(sub.add_parser("sim", help="run one simulation and write its result package"))
    sp.add_argument("--routing", default=None, help="routing policy instead of the scenario's")
    sp.add_argument("--routing-params", default=None, help="routing parameters (JSON); without --routing they update "
                                                           "the scenario's policy")
    sp.add_argument("--capacity", default=None, help="capacity policy instead of the scenario's")
    sp.add_argument("--capacity-params", default=None, help="capacity parameters (JSON); without --capacity they update "
                                                            "the scenario's policy")
    sp.add_argument("--hours", type=_positive, default=24, help="simulated hours (default 24)")
    sp.add_argument("--bin-s", type=_positive, default=300.0, help="time bin of the time series (s, default 300)")
    workload(sp)
    sp.add_argument("--out", default=None, help="result directory (default results/sim-<routing>__<capacity>)")
    sp.set_defaults(fn=cmd_sim)

    sp = sub.add_parser("run", help="run the simulation variants of an experiment and compare them")
    sp.add_argument("experiment", help="experiment file (YAML)")
    sp.add_argument("--jobs", type=int, default=4, help="parallel processes (default 4)")
    sp.add_argument("--out", default=None, help="result directory (default results/<experiment>)")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("compare", help="compare result directories (simulation or live), in total and per site")
    sp.add_argument("dirs", nargs="+", help="result directories (two or more; ratios for two)")
    sp.set_defaults(fn=cmd_compare)

    # experiments
    sp = scenario(sub.add_parser("up", help="start all experiment services in one process"))
    endpoints(sp, local=False, listen=False)
    sp.add_argument("--host", default=None, help="listen address of all services")
    policies(sp)
    sp.add_argument("--no-controller", action="store_true", help="do not start the controller")
    sp.add_argument("--log-dir", default="logs", help="log directory (default logs)")
    sp.set_defaults(fn=cmd_up)

    sp = scenario(sub.add_parser("live", help="run an experiment: replay the workload, sample, write the result package"))
    sp.add_argument("--duration", type=_positive, required=True, help="duration (s)")
    sp.add_argument("--endpoints", "-e", default=None, help="endpoints of deployed services; default: start all services here")
    sp.add_argument("--local", action="store_true", help="connect to services running at the local addresses (cpc up)")
    policies(sp, " (services started here only)")
    sp.add_argument("--sample-s", type=_positive, default=10.0, help="sampling period of sites and signals (s, default 10)")
    sp.add_argument("--model", default="cpc", help="model field of the requests (model name of real instances)")
    sp.add_argument("--no-stream", action="store_true", help="non-streaming requests (no TTFT)")
    workload(sp)
    sp.add_argument("--out", default=None, help="result directory (default results/live-<time>)")
    sp.set_defaults(fn=cmd_live)

    sp = scenario(sub.add_parser("event", help="inject a demand-response event into the grid-signal service"))
    endpoints(sp, listen=False)
    sp.add_argument("--site", required=True)
    sp.add_argument("--cap-kw", type=_positive, required=True, help="facility power limit (kW)")
    sp.add_argument("--duration-min", type=_positive, default=60, help="duration (signal time, minutes)")
    sp.add_argument("--kind", default="peak_shaving")
    sp.set_defaults(fn=cmd_event)

    # services
    sp = scenario(sub.add_parser("grid", help="grid-signal service (authoritative signal clock)"))
    endpoints(sp)
    sp.add_argument("--speedup", type=_positive, default=None, help="signal clock speed-up")
    sp.set_defaults(fn=cmd_grid)

    sp = scenario(sub.add_parser("site", help="site agent"))
    endpoints(sp)
    sp.add_argument("--site", required=True)
    sp.add_argument("--control", default=None, help="fetch scenario and addresses from the control node (address of its "
                                                    "grid-signal service, e.g. http://10.0.0.10:9000) instead of -s and -e")
    sp.add_argument("--startup-timeout", type=_positive, default=120.0, help="time to wait for the control node (s, default 120)")
    sp.add_argument("--enable-power-cap", action="store_true", help="set accelerator power limits through the sensors "
                                                                    "(NVML needs root, or SYS_ADMIN in a container)")
    sp.add_argument("--initial-active", type=int, default=None)
    sp.set_defaults(fn=cmd_site)

    for name, h, interval in (("gateway", "routing gateway", False), ("controller", "capacity controller", True)):
        sp = scenario(sub.add_parser(name, help=h))
        endpoints(sp)
        sp.add_argument("--policy", default=None)
        sp.add_argument("--params", default=None, help="policy parameters (JSON)")
        if interval:
            sp.add_argument("--interval", type=_positive, default=None, help="control period (s)")
        sp.add_argument("--log", default=None, help="request or decision log (JSONL)")
        sp.add_argument("--startup-timeout", type=_positive, default=30.0, help="time to wait for the grid-signal service (s)")
        sp.set_defaults(fn=cmd_gateway if name == "gateway" else cmd_controller)

    # sensors
    sp = sub.add_parser("sensors", help="list or read power sensors")
    sp.add_argument("--list", action="store_true", help="list the sensor types and whether this host meets their prerequisites")
    sp.add_argument("--types", default=None, help="comma-separated sensor types, e.g. nvml,rapl")
    sp.add_argument("--spec", default=None, help="sensor file (YAML with parameters)")
    sp.add_argument("--count", type=int, default=3)
    sp.add_argument("--interval", type=_positive, default=1.0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_sensors)
    return p


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (ConfigError, StartupError, SensorError, DatasetError) as e:
        sys.stdout.flush()  # what was printed comes first, the error last
        print(f"cpc {args.cmd}: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main(sys.argv[1:])
