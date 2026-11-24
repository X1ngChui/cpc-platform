"""真实 GPU 站点检查：压测期间按秒记录站点实测功率，然后通过容量指令关停/重启推理实例。

前提：已按 configs/scenario.local-gpu.yaml 以本机模式启动服务（cpc up -s configs/scenario.local-gpu.yaml），
站点 lab 的上游是本机 vLLM（容器名 cpc-vllm-local）。

用法：python scripts/real_gpu_check.py [--site lab] [--rate-scale 0.4] [--duration 60] [--restart]
输出：logs/realgpu/power_trace.csv（时间、在途请求数、GPU 功率、IT 功率）与摘要。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cpc.common import admin_headers  # noqa: E402
from cpc.config import load_endpoints, load_scenario  # noqa: E402
from cpc.loadgen.client import run as loadgen  # noqa: E402


async def sample(url: str, stop: asyncio.Event, rows: list[dict]) -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
        t0 = time.time()
        while not stop.is_set():
            s = (await c.get(url + "/state")).json()
            n = s["nodes"][0]
            rows.append({"t": round(time.time() - t0, 1), "busy": s["busy"], "queue": s["queue_len"], "state": n["state"],
                         "gpu_w": n["gpu_power_w"], "it_w": n["it_power_w"], "source": n["power_source"]})
            await asyncio.sleep(1.0)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenario", default=str(ROOT / "configs/scenario.local-gpu.yaml"))
    ap.add_argument("--site", default="lab")
    ap.add_argument("--rate-scale", type=float, default=None, help="覆盖场景负载的到达率缩放")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--restart", action="store_true", help="压测后用容量指令关停再重启推理实例")
    args = ap.parse_args()
    scn = load_scenario(args.scenario)
    ep = load_endpoints(None, scn, local=True)
    url = ep.sites[args.site]
    rows: list[dict] = []
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample(url, stop, rows))
    await asyncio.sleep(5)  # 空载基线
    # 只压测该站点：直接向站点代理发请求（与经网关相同的 OpenAI 接口）
    if args.rate_scale is not None:
        scn.workload.rate_scale = args.rate_scale
    summary = await loadgen(url, scn, args.duration, stream=True, model="cpc")
    await asyncio.sleep(5)
    events = []
    if args.restart:
        async with httpx.AsyncClient(trust_env=False, timeout=10, headers=admin_headers()) as c:
            await c.post(url + "/admin/capacity", json={"active_nodes": 0})
            events.append(("off", rows[-1]["t"] if rows else 0))
            await asyncio.sleep(15)
            await c.post(url + "/admin/capacity", json={"active_nodes": 1})
            events.append(("on", rows[-1]["t"] if rows else 0))
            t0 = time.time()
            while (await c.get(url + "/state")).json()["nodes_on"] == 0 and time.time() - t0 < 600:
                await asyncio.sleep(2)
            events.append(("ready", rows[-1]["t"] if rows else 0))
            await asyncio.sleep(5)
    stop.set()
    await sampler
    out = ROOT / "logs/realgpu/power_trace.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    idle = [r["gpu_w"] for r in rows[:5]]
    load = [r["gpu_w"] for r in rows if r["busy"] >= 4]
    print(json.dumps({"loadgen": summary, "gpu_w_idle_mean": round(sum(idle) / len(idle), 1),
                      "gpu_w_under_load_mean": round(sum(load) / len(load), 1) if load else None,
                      "gpu_w_max": max(r["gpu_w"] for r in rows), "events": events, "trace": str(out)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
