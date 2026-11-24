"""负载生成器：向网关发送 OpenAI 兼容请求。

两种模式：
1. 合成负载：非齐次泊松到达，λ(t) = rps × (1 + A·cos(2π(h−峰值时刻)/24))，h 取信号时钟的小时数；
   来源区域按 share 抽样；输入/输出 token 数服从对数正态分布。
2. Trace 回放：读取 Azure LLM 推理 trace 格式（列 TIMESTAMP, ContextTokens, GeneratedTokens）的 CSV。

请求体带 max_tokens 与 ignore_eos=true（vLLM 扩展参数），使真实模型输出长度可控，这是推理压测的通行做法。
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from ..config import CST, Scenario, TokenDist
from ..models import SimClock, diurnal_factor

VOCAB = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
         "upsilon phi chi psi omega data center power grid energy carbon price route node").split()


def lognormal(rng: np.random.Generator, d: TokenDist) -> int:
    mu = math.log(d.mean) - d.sigma**2 / 2
    return int(min(max(rng.lognormal(mu, d.sigma), d.min), d.max))


@dataclass
class Arrival:
    t: float  # 相对开始的真实秒数
    source: str
    n_in: int
    n_out: int


def synth_arrivals(scn: Scenario, clock: SimClock, t_begin_wall: float, duration_s: float, rps: float | None = None) -> list[Arrival]:
    w = scn.workload
    rng = np.random.default_rng(w.seed)
    rps = rps if rps is not None else w.rps
    lam_max = rps * (1 + abs(w.diurnal_amplitude))
    srcs = [s.id for s in scn.sources]
    shares = np.array([s.share for s in scn.sources], dtype=float)
    shares /= shares.sum()
    out, t = [], 0.0
    while True:
        t += rng.exponential(1.0 / lam_max)
        if t >= duration_s:
            break
        st = clock.at_wall(t_begin_wall + t).astimezone(CST)
        lam = rps * diurnal_factor(st.hour + st.minute / 60, w.diurnal_amplitude, w.peak_hour)
        if rng.random() * lam_max <= lam:
            out.append(Arrival(t, str(rng.choice(srcs, p=shares)), lognormal(rng, w.input_tokens), lognormal(rng, w.output_tokens)))
    return out


def trace_arrivals(scn: Scenario, path: str | Path, duration_s: float, time_scale: float = 1.0) -> list[Arrival]:
    """Azure LLM inference trace（2023/2024）格式：TIMESTAMP,ContextTokens,GeneratedTokens。"""
    rng = random.Random(scn.workload.seed)
    srcs = [s.id for s in scn.sources]
    shares = [s.share for s in scn.sources]
    out, t0 = [], None
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["TIMESTAMP"].strip()[:26]).timestamp()
            t0 = ts if t0 is None else t0
            t = (ts - t0) / time_scale
            if t >= duration_s:
                break
            out.append(Arrival(t, rng.choices(srcs, shares)[0], int(row["ContextTokens"]), max(1, int(row["GeneratedTokens"]))))
    return out


def make_body(a: Arrival, rng: random.Random, stream: bool, model: str = "cpc") -> dict:
    prompt = " ".join(rng.choice(VOCAB) for _ in range(a.n_in))
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": a.n_out,
            "ignore_eos": True, "stream": stream}
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


async def run(gateway: str, scn: Scenario, duration_s: float, grid: str | None = None, rps: float | None = None,
              stream: bool = True, out: str | None = None, trace: str | None = None, time_scale: float = 1.0,
              model: str = "cpc", max_concurrency: int = 4000) -> dict:
    clock = SimClock(start=scn.clock.start, speedup=scn.clock.speedup)
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0),
                                 limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=500)) as client:
        if grid:
            try:
                clock = SimClock.from_dict((await client.get(grid.rstrip("/") + "/v1/clock")).json())
            except httpx.HTTPError:
                pass
        t_begin = time.time()
        arrivals = trace_arrivals(scn, trace, duration_s, time_scale) if trace else synth_arrivals(scn, clock, t_begin, duration_s, rps)
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
                        ttft = None
                        async with client.stream("POST", url, json=body, headers={"x-cpc-source": a.source}) as r:
                            rec["status"] = r.status_code
                            rec["site"] = r.headers.get("x-cpc-site")
                            async for _ in r.aiter_raw():
                                if ttft is None:
                                    ttft = time.monotonic() - t0
                        rec["ttft_s"] = ttft
                    else:
                        r = await client.post(url, json=body, headers={"x-cpc-source": a.source})
                        rec["status"] = r.status_code
                        rec["site"] = r.headers.get("x-cpc-site")
                        if "x-cpc-energy-j" in r.headers:
                            rec["energy_j"] = float(r.headers["x-cpc-energy-j"])
                except httpx.HTTPError as e:
                    rec["status"] = -1
                    rec["error"] = type(e).__name__
                rec["e2e_s"] = time.monotonic() - t0
                results.append(rec)

        tasks = []
        for a in arrivals:
            delay = t_begin + a.t - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(a)))
        await asyncio.gather(*tasks)

    summary = summarize(results, scn, stream)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return summary


def summarize(results: list[dict], scn: Scenario, stream: bool) -> dict:
    ok = [r for r in results if r.get("status") == 200]
    s: dict = {"sent": len(results), "ok": len(ok), "failed": len(results) - len(ok)}
    if ok:
        e2e = np.array([r["e2e_s"] for r in ok])
        s.update(e2e_p50=float(np.percentile(e2e, 50)), e2e_p99=float(np.percentile(e2e, 99)))
        if stream:
            ttft = np.array([r["ttft_s"] for r in ok if r.get("ttft_s") is not None])
            tpot = np.array([(r["e2e_s"] - r["ttft_s"]) / max(r["n_out"] - 1, 1) for r in ok if r.get("ttft_s") is not None])
            if len(ttft):
                s.update(ttft_p50=float(np.percentile(ttft, 50)), ttft_p99=float(np.percentile(ttft, 99)),
                         slo_attainment=float(np.mean((ttft <= scn.slo.ttft_s) & (tpot <= scn.slo.tpot_s))))
        sites: dict[str, int] = {}
        for r in ok:
            sites[r.get("site") or "?"] = sites.get(r.get("site") or "?", 0) + 1
        s["site_share"] = {k: round(v / len(ok), 3) for k, v in sorted(sites.items())}
    return s
