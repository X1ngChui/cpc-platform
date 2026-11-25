"""负载生成器：按场景的负载数据集（经显式变换）向网关发送 OpenAI 兼容请求。

请求到达时间取变换后 trace 的相对时间（秒），按真实时间回放；来源区域按场景 sources[].share 分配。
请求体带 max_tokens 与 ignore_eos=true（vLLM 扩展参数），使真实模型输出长度与 trace 一致，这是推理压测的通行做法。
场景 workload.attributes 选定的扩展字段以 x-cpc-attr-<字段名> 请求头发送，与仿真中策略可见的请求属性一致。

给出电网信号服务地址时，先确认其可达（失败则报错退出）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

from ..common import attr_headers, sync_clock
from ..config import Scenario
from ..datasets.workload import WorkloadTrace, assign_sources, load_workload

VOCAB = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
         "upsilon phi chi psi omega data center power grid energy carbon price route node").split()


@dataclass
class Arrival:
    t: float  # 相对开始的真实秒数
    source: str
    n_in: int
    n_out: int
    attrs: dict

    def headers(self) -> dict[str, str]:
        return {"x-cpc-source": self.source, **attr_headers(self.attrs)}


def arrivals_from(trace: WorkloadTrace, scn: Scenario, duration_s: float) -> list[Arrival]:
    """与仿真器相同的来源分配（同一种子）。"""
    keep = np.nonzero(trace.t < duration_s)[0]
    srcs = assign_sources(len(keep), [s.id for s in scn.sources], [s.share for s in scn.sources], scn.workload.seed)
    return [Arrival(float(trace.t[i]), str(src), max(1, int(trace.input_tokens[i])), max(1, int(trace.output_tokens[i])),
                    trace.attrs_of(i)) for i, src in zip(keep, srcs)]


def make_body(a: Arrival, rng: random.Random, stream: bool, model: str = "cpc") -> dict:
    prompt = " ".join(rng.choice(VOCAB) for _ in range(a.n_in))
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": a.n_out,
            "ignore_eos": True, "stream": stream}
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


async def run(gateway: str, scn: Scenario, duration_s: float, grid: str | None = None, stream: bool = True,
              out: str | None = None, model: str = "cpc", max_concurrency: int = 4000) -> dict:
    """回放负载并返回汇总；out 给出时把逐请求记录写为 JSONL。"""
    trace, results = await replay(gateway, scn, duration_s, grid, stream, model, max_concurrency)
    summary = summarize(results, scn, stream)
    summary["workload"] = trace.ref
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return summary


async def replay(gateway: str, scn: Scenario, duration_s: float, grid: str | None = None, stream: bool = True,
                 model: str = "cpc", max_concurrency: int = 4000, trace: WorkloadTrace | None = None,
                 ) -> tuple[WorkloadTrace, list[dict]]:
    """按变换后 trace 的相对时间回放请求，返回负载与逐请求记录（按到达顺序）。trace 缺省时按场景加载。

    记录字段：t（计划到达时间，秒）、source、n_in、n_out、status（HTTP 状态码；连接失败为 -1）、site、server、
    route_reason、ttft_s（流式请求）、e2e_s、error。
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=10.0),
                                 limits=httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=500),
                                 trust_env=False) as client:
        if grid:
            await sync_clock(client, grid, 10.0)  # 确认平台的电网信号服务可达（与其他服务使用同一信号时钟）
        if trace is None:
            trace = load_workload(scn)
        if trace.duration_s < duration_s:
            raise ValueError(f"负载 {trace.ref} 变换后只覆盖 {trace.duration_s:.0f} s，短于压测时长 {duration_s:.0f} s")
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
                        ttft = None
                        async with client.stream("POST", url, json=body, headers=a.headers()) as r:
                            rec["status"] = r.status_code
                            rec["site"] = r.headers.get("x-cpc-site")
                            rec["route_reason"] = r.headers.get("x-cpc-route-reason")
                            rec["server"] = r.headers.get("x-cpc-node")
                            async for _ in r.aiter_raw():
                                if ttft is None:
                                    ttft = time.monotonic() - t0
                        rec["ttft_s"] = ttft
                    else:
                        r = await client.post(url, json=body, headers=a.headers())
                        rec["status"] = r.status_code
                        rec["site"] = r.headers.get("x-cpc-site")
                        rec["route_reason"] = r.headers.get("x-cpc-route-reason")
                        rec["server"] = r.headers.get("x-cpc-node")
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
    results.sort(key=lambda r: r["t"])
    return trace, results


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
