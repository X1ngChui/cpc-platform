"""实验运行（cpc live）：按场景回放负载，周期采样各站点状态与电网信号，结束后写出与仿真相同格式的结果数据包。

两种方式：
- 本机：未给出服务地址时，在本进程中启动全部实验服务（与 cpc up 相同），运行结束后停止；
- 已部署：给出地址文件时，连接已运行的服务（Docker Compose、Kubernetes、云服务器等），只回放负载与采样。

结果的口径与仿真一致（见 cpc.results）：
- 逐请求记录来自负载生成器：到达时间、来源、站点、服务器、状态（served：HTTP 200；rejected：HTTP 503；
  failed：其他状态或连接失败）、路由原因、首 token 时延（流式请求）、端到端时延、token 间隔、是否满足 SLO；
- 时间序列按采样周期：各站点 IT 功率（由站点累计能耗的差分得到）、非关机服务器数、到达率、电价、碳强度、功率上限；
- 电量取站点累计能耗（功率有效区间内的积分）的差分，成本与碳排放按每个采样区间起点的电网信号核算；
  站点状态或信号缺失的区间不核算，汇总中给出覆盖率。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import numpy as np

from .common import StartupError, admin_headers
from .config import Endpoints, Scenario
from .datasets.workload import load_workload
from .loadgen.client import replay
from .results import RunResult

READY_TIMEOUT_S = 180.0


async def _json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url, timeout=5.0)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def wait_ready(client: httpx.AsyncClient, ep: Endpoints, timeout_s: float = READY_TIMEOUT_S) -> None:
    urls = {"grid": ep.grid, "gateway": ep.gateway, "controller": ep.controller, **{f"site:{k}": v for k, v in ep.sites.items()}}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    pending = dict(urls)
    while pending:
        for name, url in list(pending.items()):
            try:
                if (await client.get(url + "/readyz", timeout=3.0)).status_code == 200:
                    pending.pop(name)
            except httpx.HTTPError:
                pass
        if pending and loop.time() > deadline:
            raise StartupError(f"服务在 {timeout_s:.0f} 秒内未就绪：{sorted(pending)}")
        if pending:
            await asyncio.sleep(1.0)


class Sampler:
    """周期采样各站点状态摘要、电网信号与控制器的最近一次决策。"""

    def __init__(self, client: httpx.AsyncClient, ep: Endpoints, period_s: float):
        self.client, self.ep, self.period = client, ep, period_s
        self.rows: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.t0 = time.monotonic()

    async def sample(self) -> None:
        t = time.monotonic() - self.t0
        sites = await asyncio.gather(*(_json(self.client, u + "/state") for u in self.ep.sites.values()))
        sig = await _json(self.client, self.ep.grid + "/v1/signals")
        ctl = await _json(self.client, self.ep.controller + "/state")
        if ctl and ctl.get("last"):
            self.decisions[json.dumps(ctl["last"], sort_keys=True, default=str)] = ctl["last"]
        self.rows.append({"t": t, "sites": dict(zip(self.ep.sites, sites)), "signals": sig})

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.sample()
            try:
                await asyncio.wait_for(stop.wait(), self.period)
            except asyncio.TimeoutError:
                pass
        await self.sample()


async def run_live(scn: Scenario, ep: Endpoints, duration_s: float, *, sample_s: float = 10.0, model: str = "cpc",
                   stream: bool = True, decisions_log: Path | None = None) -> tuple[RunResult, dict]:
    """回放负载并采样；返回结果与运行描述的补充信息（所用策略）。服务须已启动。"""
    trace = load_workload(scn)
    if trace.duration_s < duration_s:
        raise StartupError(f"负载 {trace.ref} 变换后只覆盖 {trace.duration_s:.0f} 秒，短于实验时长 {duration_s:.0f} 秒")
    async with httpx.AsyncClient(trust_env=False, headers=admin_headers()) as client:
        await wait_ready(client, ep)
        gw = await _json(client, ep.gateway + "/state") or {}
        ctl = await _json(client, ep.controller + "/state") or {}
        sampler = Sampler(client, ep, sample_s)
        stop = asyncio.Event()
        task = asyncio.create_task(sampler.loop(stop))
        try:
            _, recs = await replay(ep.gateway, scn, duration_s, grid=ep.grid, stream=stream, model=model, trace=trace)
        finally:
            stop.set()
            await task
    decisions = list(sampler.decisions.values())
    if decisions_log is not None and decisions_log.is_file():
        decisions = [json.loads(x) for x in decisions_log.read_text(encoding="utf-8").splitlines() if x.strip()]
    res = _result(scn, trace, recs, sampler.rows, decisions, duration_s, stream)
    res.summary["routing"] = gw.get("policy")
    res.summary["capacity"] = ctl.get("policy")
    return res, {"routing": gw.get("policy"), "capacity": ctl.get("policy")}


def _status(code: int) -> str:
    return "served" if code == 200 else "rejected" if code == 503 else "failed"


def _result(scn: Scenario, trace, recs: list[dict], rows: list[dict], decisions: list[dict], duration_s: float,
            stream: bool) -> RunResult:
    nan = float("nan")
    sites = [s.id for s in scn.sites]
    pue = {s.id: s.pue for s in scn.sites}
    # ---------------------------------------------------------------- 逐请求记录
    status = np.array([_status(r.get("status", -1)) for r in recs], dtype=object)
    ttft = np.array([nan if r.get("ttft_s") is None else r["ttft_s"] for r in recs], dtype=float)
    e2e = np.array([r.get("e2e_s", nan) for r in recs], dtype=float)
    n_out = np.array([r["n_out"] for r in recs], dtype=np.int64)
    tpot = np.where(np.isnan(ttft), nan, (e2e - ttft) / np.maximum(n_out - 1, 1))
    served = status == "served"
    if stream:
        ok = served & (ttft <= scn.slo.ttft_s) & (tpot <= scn.slo.tpot_s)
    else:
        ok = served & (e2e <= scn.slo.ttft_s + n_out * scn.slo.tpot_s)
    requests = {
        "arrival_s": np.array([r["t"] for r in recs], dtype=float),
        "source": np.array([r["source"] for r in recs], dtype=object),
        "site": np.array([r.get("site") or "" for r in recs], dtype=object),
        "server": np.array([r.get("server") or "" for r in recs], dtype=object),
        "status": status,
        "route_reason": np.array([r.get("route_reason") or "" for r in recs], dtype=object),
        "input_tokens": np.array([r["n_in"] for r in recs], dtype=np.int64), "output_tokens": n_out,
        "ttft_s": ttft, "e2e_s": e2e, "tpot_s": tpot, "slo_ok": ok,
    }
    # ---------------------------------------------------------------- 时间序列与电量核算
    keys = ("it_kwh", "facility_kwh", "cost", "carbon_kg")
    tot = dict.fromkeys(keys, 0.0)
    per_site = {sid: {**dict.fromkeys(keys, 0.0), "requests": int(np.sum(served & (requests["site"] == sid)))} for sid in sites}
    priced = total_fac = 0.0
    missing_intervals = dict.fromkeys(sites, 0)
    currency, sources = None, {}
    ts = []
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0:
            continue
        sig = a["signals"] or {}
        sources = sig.get("sources", sources)
        row = {"t_h": round(a["t"] / 3600, 5), "time": sig.get("time")}
        for sid in sites:
            sa, sb = a["sites"].get(sid), b["sites"].get(sid)
            g = (sig.get("signals") or {}).get(sid)
            currency = currency or (g or {}).get("currency")
            it_kw = None
            if sa and sb:
                e_kwh = (sb["energy_it_j"] - sa["energy_it_j"]) / 3.6e6
                it_kw = e_kwh * 3600 / dt
                fac = e_kwh * pue[sid]
                total_fac += fac
                for d in (tot, per_site[sid]):
                    d["it_kwh"] += e_kwh
                    d["facility_kwh"] += fac
                    if g is not None:
                        d["cost"] += fac * g["price"]
                        d["carbon_kg"] += fac * g["carbon_intensity"]
                if g is not None:
                    priced += fac
            else:
                missing_intervals[sid] += 1
            arr = np.sum((requests["site"] == sid) & (requests["arrival_s"] >= a["t"]) & (requests["arrival_s"] < b["t"]))
            row[f"{sid}_it_kw"] = None if it_kw is None else round(it_kw, 4)
            row[f"{sid}_nodes"] = None if not sb else sb["nodes_on"] + sb["nodes_booting"] + sb["nodes_draining"]
            row[f"{sid}_rps"] = round(float(arr) / dt, 4)
            row[f"{sid}_price"] = None if g is None else g["price"]
            row[f"{sid}_carbon"] = None if g is None else g["carbon_intensity"]
            row[f"{sid}_cap_kw"] = None if g is None else g["power_cap_kw"]
        ts.append(row)
    ns = int(served.sum())
    last = rows[-1]["sites"] if rows else {}
    reasons: dict[str, int] = {}
    for r in requests["route_reason"]:
        if r:
            reasons[r] = reasons.get(r, 0) + 1
    skipped: dict[str, int] = {}
    for d in decisions:
        if d.get("result") == "skipped":
            skipped[d.get("reason", "")] = skipped.get(d.get("reason", ""), 0) + 1
    tt, ee = ttft[served & ~np.isnan(ttft)], e2e[served]
    summary = {
        "mode": "live", "duration_s": duration_s, "workload": trace.summary(), "grid": sources, "currency": currency,
        "rps": round(len(recs) / duration_s, 4), "arrived": len(recs), "served": ns,
        "rejected": int(np.sum(status == "rejected")), "failed": int(np.sum(status == "failed")),
        "slo_attainment": float(ok.sum() / ns) if ns else 0.0,
        "ttft_p50_s": float(np.percentile(tt, 50)) if len(tt) else None,
        "ttft_p99_s": float(np.percentile(tt, 99)) if len(tt) else None,
        "e2e_p50_s": float(np.percentile(ee, 50)) if len(ee) else None,
        **{k: round(v, 6) for k, v in tot.items()},
        "wh_per_request": round(tot["facility_kwh"] * 1000 / max(ns, 1), 4),
        "sites": {k: {kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in per_site.items()},
        "route_reasons": dict(sorted(reasons.items())), "skipped_cycles": skipped,
        "signal_coverage": round(priced / total_fac, 4) if total_fac else None,
        "energy_coverage": {sid: (last.get(sid) or {}).get("energy_coverage") for sid in sites},
        "missing_state_intervals": {k: v for k, v in missing_intervals.items() if v},
    }
    return RunResult(summary, ts, requests, decisions)
