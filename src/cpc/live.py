"""Live experiments (cpc live): replay the workload of a scenario through the gateway, sample the sites
and grid signals periodically, and write a result package in the same format as simulations.

Two ways to run:
- locally: without service addresses all services start in this process (as with cpc up) and stop
  at the end;
- deployed: with an endpoints file the running services (Docker Compose, cloud servers, ...) are used;
  only the replay and the sampling run here.

Replay: arrival times are the relative times of the transformed trace, replayed in real time; source
regions follow sources[].share with the same seed as the simulator. Request bodies set max_tokens and
ignore_eos=true (an extension of vLLM and llama.cpp) so that real models produce the output length of
the trace, as is usual for inference benchmarks. Prompts consist of short common English words: each
is one token for common tokenizers, and the site agent's estimate (max(words, characters / 4)) is the
word count too, so real servers, emulated servers and the simulator see the input length of the
trace. Extension fields selected by workload.attributes are sent as x-cpc-attr-<field> headers, as
policies see them in simulation.

Accounting follows the simulator (see cpc.results):
- requests come from the replay: arrival, source, site, server, status (served: HTTP 200; rejected:
  HTTP 503; failed: other status, connection failure or incomplete stream), routing reason, TTFT
  (streaming), end-to-end latency, time per output token and whether the SLO was met;
- the time series per sampling period: IT power of each site (difference of the accumulated site
  energy), servers not off, arrival rate, price, carbon intensity and power limit;
- energy is the difference of the accumulated site energy (integrated over valid power); cost and
  carbon use the grid signal at the start of each sampling interval. Intervals without a site state
  or signal are not accounted, and the summary gives the coverage.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

from .common import SSECheck, StartupError, admin_headers, attr_headers, sync_clock
from .config import ConfigError, Endpoints, Scenario
from .datasets.workload import WorkloadTrace, assign_sources, load_workload
from .results import RunResult

READY_TIMEOUT_S = 180.0
NAN = float("nan")

# Common English words of at most 3 characters: characters / 4 never exceeds the word count, and each
# word (with its leading space) is one token for Qwen and similar tokenizers.
VOCAB = "the of and to in is it on at as by be or an we he if up so no do my me go us am".split()

# ---------------------------------------------------------------- replay


@dataclass(slots=True)
class Arrival:
    t: float  # real seconds from the start
    source: str
    n_in: int
    n_out: int
    attrs: dict

    def headers(self) -> dict[str, str]:
        return {"x-cpc-source": self.source, **attr_headers(self.attrs)}


def arrivals_from(trace: WorkloadTrace, scn: Scenario, duration_s: float) -> list[Arrival]:
    """Arrivals within duration_s, with the source assignment of the simulator (same seed)."""
    keep = np.nonzero(trace.t < duration_s)[0]
    srcs = assign_sources(len(keep), [s.id for s in scn.sources], [s.share for s in scn.sources], scn.workload.seed)
    return [Arrival(float(trace.t[i]), str(src), max(1, int(trace.input_tokens[i])), max(1, int(trace.output_tokens[i])),
                    trace.attrs_of(i)) for i, src in zip(keep, srcs)]


def make_body(a: Arrival, rng: random.Random, stream: bool, model: str = "cpc") -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": " ".join(rng.choices(VOCAB, k=a.n_in))}],
            "max_tokens": a.n_out, "ignore_eos": True, "stream": stream}
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def _route_headers(r: httpx.Response) -> dict:
    return {"status": r.status_code, "site": r.headers.get("x-cpc-site"), "route_reason": r.headers.get("x-cpc-route-reason"),
            "server": r.headers.get("x-cpc-node")}


async def replay(gateway: str, scn: Scenario, duration_s: float, grid: str | None = None, stream: bool = True,
                 model: str = "cpc", max_concurrency: int = 4000, trace: WorkloadTrace | None = None,
                 ) -> tuple[WorkloadTrace, list[dict]]:
    """Replay the requests at the relative times of the transformed trace (loaded from the scenario if
    not given); returns the trace and the request records in arrival order.

    Record fields: t (scheduled arrival, s), source, n_in, n_out, status (HTTP status; -1 on connection
    failure), site, server, route_reason, ttft_s (streaming), e2e_s, error.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0), trust_env=False,
                                 limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=500)) as client:
        if grid:
            await sync_clock(client, grid, 10.0)  # the grid-signal service of the platform must be reachable
        trace = trace if trace is not None else load_workload(scn)
        if trace.duration_s < duration_s:
            raise ConfigError(f"workload {trace.ref} covers only {trace.duration_s:.0f} s after its transformations, "
                              f"less than the duration {duration_s:.0f} s; reduce --duration")
        arrivals = arrivals_from(trace, scn, duration_s)
        t_begin = time.time()
        rng = random.Random(scn.workload.seed)
        sem = asyncio.Semaphore(max_concurrency)
        results: list[dict] = []
        url = gateway.rstrip("/") + "/v1/chat/completions"

        async def one(a: Arrival) -> None:
            body = make_body(a, rng, stream, model)
            async with sem:
                t0 = time.monotonic()
                rec = {"t": round(a.t, 4), "source": a.source, "n_in": a.n_in, "n_out": a.n_out}
                try:
                    if stream:
                        ttft, check = None, SSECheck()
                        async with client.stream("POST", url, json=body, headers=a.headers()) as r:
                            rec |= _route_headers(r)
                            async for chunk in r.aiter_raw():
                                check.feed(chunk)
                                if ttft is None:
                                    ttft = time.monotonic() - t0
                        rec["ttft_s"] = ttft
                        if r.status_code == 200 and check.problem is not None:  # error event or no proper end: failed
                            rec["status"], rec["error"] = 502, check.problem
                    else:
                        rec |= _route_headers(await client.post(url, json=body, headers=a.headers()))
                except httpx.HTTPError as e:
                    rec["status"], rec["error"] = -1, type(e).__name__
                rec["e2e_s"] = time.monotonic() - t0
                results.append(rec)

        tasks = []
        for a in arrivals:
            if (delay := t_begin + a.t - time.time()) > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(a)))
        await asyncio.gather(*tasks)
    results.sort(key=lambda r: r["t"])
    return trace, results


# ---------------------------------------------------------------- sampling


async def _json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url, timeout=5.0)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def wait_ready(client: httpx.AsyncClient, ep: Endpoints, timeout_s: float = READY_TIMEOUT_S) -> None:
    pending = {"grid": ep.grid, "gateway": ep.gateway, "controller": ep.controller, **{f"site:{k}": v for k, v in ep.sites.items()}}
    deadline = time.monotonic() + timeout_s
    while pending:
        for name, url in list(pending.items()):
            with suppress(httpx.HTTPError):
                if (await client.get(url + "/readyz", timeout=3.0)).status_code == 200:
                    del pending[name]
        if not pending:
            return
        if time.monotonic() > deadline:
            raise StartupError(f"services not ready within {timeout_s:.0f} s: {sorted(pending)}")
        await asyncio.sleep(1.0)


class Sampler:
    """Samples site summaries, grid signals and the last controller decision periodically."""

    def __init__(self, client: httpx.AsyncClient, ep: Endpoints, period_s: float):
        self.client, self.ep, self.period = client, ep, period_s
        self.rows: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.t0 = time.monotonic()

    async def sample(self) -> None:
        t = time.monotonic() - self.t0
        sites = await asyncio.gather(*(_json(self.client, u + "/state") for u in self.ep.sites.values()))
        sig = await _json(self.client, self.ep.grid + "/v1/signals")
        if (ctl := await _json(self.client, self.ep.controller + "/state")) and (last := ctl.get("last")):
            self.decisions[json.dumps(last, sort_keys=True, default=str)] = last
        self.rows.append({"t": t, "sites": dict(zip(self.ep.sites, sites)), "signals": sig})

    async def loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.sample()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.period)
        await self.sample()


async def run_live(scn: Scenario, ep: Endpoints, duration_s: float, *, sample_s: float = 10.0, model: str = "cpc",
                   stream: bool = True, decisions_log: Path | None = None) -> tuple[RunResult, dict]:
    """Replay and sample with running services; returns the result and run information (the policies used)."""
    trace = load_workload(scn)
    if trace.duration_s < duration_s:
        raise StartupError(f"workload {trace.ref} covers only {trace.duration_s:.0f} s after its transformations, "
                           f"less than the duration {duration_s:.0f} s")
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
    policies = {"routing": gw.get("policy"), "capacity": ctl.get("policy")}
    res = _result(scn, trace, recs, sampler.rows, decisions, duration_s, stream)
    res.summary |= policies
    return res, policies


# ---------------------------------------------------------------- result


def _status(code: int) -> str:
    return "served" if code == 200 else "rejected" if code == 503 else "failed"


def _requests(scn: Scenario, recs: list[dict], stream: bool) -> dict[str, np.ndarray]:
    col = lambda key, default=None, dtype=object: np.array([r.get(key) or default for r in recs], dtype=dtype)  # noqa: E731
    status = np.array([_status(r.get("status", -1)) for r in recs], dtype=object)
    ttft = np.array([NAN if r.get("ttft_s") is None else r["ttft_s"] for r in recs], dtype=float)
    e2e = np.array([r.get("e2e_s", NAN) for r in recs], dtype=float)
    n_out = np.array([r["n_out"] for r in recs], dtype=np.int64)
    tpot = np.where(np.isnan(ttft), NAN, (e2e - ttft) / np.maximum(n_out - 1, 1))
    served = status == "served"
    if stream:
        ok = served & (ttft <= scn.slo.ttft_s) & (tpot <= scn.slo.tpot_s)
    else:
        ok = served & (e2e <= scn.slo.ttft_s + n_out * scn.slo.tpot_s)
    return {"arrival_s": np.array([r["t"] for r in recs], dtype=float), "source": col("source"), "site": col("site", ""),
            "server": col("server", ""), "status": status, "route_reason": col("route_reason", ""),
            "input_tokens": np.array([r["n_in"] for r in recs], dtype=np.int64), "output_tokens": n_out,
            "ttft_s": ttft, "e2e_s": e2e, "tpot_s": tpot, "slo_ok": ok}


def _result(scn: Scenario, trace, recs: list[dict], rows: list[dict], decisions: list[dict], duration_s: float,
            stream: bool) -> RunResult:
    sites = [s.id for s in scn.sites]
    pue = {s.id: s.pue for s in scn.sites}
    requests = _requests(scn, recs, stream)
    served, status, ttft, e2e = (requests["status"] == "served"), requests["status"], requests["ttft_s"], requests["e2e_s"]
    keys = ("it_kwh", "facility_kwh", "cost", "carbon_kg")
    tot = dict.fromkeys(keys, 0.0)
    per_site = {sid: {**dict.fromkeys(keys, 0.0), "requests": int(np.sum(served & (requests["site"] == sid)))} for sid in sites}
    priced = total_fac = 0.0
    missing_intervals: Counter[str] = Counter()
    currency, sources, ts = None, {}, []
    for a, b in zip(rows, rows[1:]):
        if (dt := b["t"] - a["t"]) <= 0:
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
                it_kw, fac = e_kwh * 3600 / dt, e_kwh * pue[sid]
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
            row |= {f"{sid}_it_kw": None if it_kw is None else round(it_kw, 4),
                    f"{sid}_nodes": sb["nodes_on"] + sb["nodes_booting"] + sb["nodes_draining"] if sb else None,
                    f"{sid}_rps": round(float(arr) / dt, 4),
                    f"{sid}_price": None if g is None else g["price"],
                    f"{sid}_carbon": None if g is None else g["carbon_intensity"],
                    f"{sid}_cap_kw": None if g is None else g["power_cap_kw"]}
        ts.append(row)
    ns = int(served.sum())
    last = rows[-1]["sites"] if rows else {}
    tt, ee = ttft[served & ~np.isnan(ttft)], e2e[served]
    pct = lambda a, q: float(np.percentile(a, q)) if len(a) else None  # noqa: E731
    summary = {
        "mode": "live", "duration_s": duration_s, "workload": trace.summary(), "grid": sources, "currency": currency,
        "rps": round(len(recs) / duration_s, 4), "arrived": len(recs), "served": ns,
        "rejected": int(np.sum(status == "rejected")), "failed": int(np.sum(status == "failed")),
        "slo_attainment": float(requests["slo_ok"].sum() / ns) if ns else 0.0,
        "ttft_p50_s": pct(tt, 50), "ttft_p99_s": pct(tt, 99), "e2e_p50_s": pct(ee, 50),
        **{k: round(v, 6) for k, v in tot.items()},
        "wh_per_request": round(tot["facility_kwh"] * 1000 / max(ns, 1), 4),
        "sites": {k: {kk: round(vv, 6) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in per_site.items()},
        "route_reasons": dict(sorted(Counter(r for r in requests["route_reason"] if r).items())),
        "skipped_cycles": dict(Counter(d.get("reason", "") for d in decisions if d.get("result") == "skipped")),
        "signal_coverage": round(priced / total_fac, 4) if total_fac else None,
        "energy_coverage": {sid: (last.get(sid) or {}).get("energy_coverage") for sid in sites},
        "missing_state_intervals": dict(missing_intervals),
    }
    return RunResult(summary, ts, requests, decisions)
