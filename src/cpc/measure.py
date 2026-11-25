"""测量真实服务器的功率特性：在站点的一台真实服务器上依次保持固定并发，记录功率与 token 间隔。

每一级并发 c：c 个客户端循环发送流式请求（固定输入、输出长度，ignore_eos），预热 warmup_s 后在 duration_s 内
    * 每秒读取站点 /state 中该服务器的实测功率（it_power_w）与在服务请求数；读数缺失的采样不计入平均，缺失比例记录在结果中；
    * 由窗口内完成的请求计算平均 token 间隔（首 token 之后的平均间隔）、首 token 时延与输出吞吐。
并发 0 一级只读取功率，作为在线空载功率。可选地测量关机功率与开机时间：经站点管理接口关闭服务器，在关机状态下
读取功率 OFF_WINDOW_S 秒（推理实例停止而机器仍在运行时的功率，即服务器类型的 off_w），再开启并计时至其重新可服务。

功率的范围与站点的测量配置一致（/state 的 power_scope：node 为整机或主机部分，accelerators 为只含加速卡）。
测量期间站点不应承担其他负载；服务器状态在测量中发生变化（被关机、排空）时报错退出。
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass, field

import httpx

from .common import admin_headers
from .config import ConfigError
from .loadgen.client import VOCAB

SAMPLE_S = 1.0  # 功率与在服务请求数的采样周期
OFF_WINDOW_S = 60.0  # 关机状态下读取功率的时长（部分 BMC 的读数数秒才更新一次，且波动较大，窗口不宜过短）


@dataclass
class Level:
    concurrency: int
    power_w: float | None = None
    power_coverage: float = 0.0
    busy_mean: float = 0.0
    itl_s: float | None = None
    ttft_s: float | None = None
    throughput_tokens_per_s: float = 0.0
    requests: int = 0
    errors: int = 0
    samples: list = field(default_factory=list)


async def _node_state(client: httpx.AsyncClient, site: str, node: str) -> tuple[dict, dict]:
    r = await client.get(site + "/state")
    r.raise_for_status()
    s = r.json()
    for n in s.get("nodes", []):
        if n["id"] == node:
            return s, n
    raise ConfigError(f"站点 {s.get('site')} 中没有服务器 {node}")


async def pick_node(client: httpx.AsyncClient, site: str, node: str | None) -> tuple[dict, dict]:
    """确定被测服务器：未指定时要求站点中恰有一台开启的真实服务器。"""
    r = await client.get(site + "/state")
    r.raise_for_status()
    s = r.json()
    real = [n for n in s.get("nodes", []) if n.get("power_source") == "sensors"]
    if not real:
        raise ConfigError(f"站点 {s.get('site')} 没有实测功率的真实服务器")
    if node is None:
        on = [n for n in real if n["state"] == "on"]
        if len(on) != 1:
            raise ConfigError(f"站点 {s.get('site')} 有 {len(on)} 台开启的真实服务器（{[n['id'] for n in real]}），请用 --node 指定")
        return s, on[0]
    for n in real:
        if n["id"] == node:
            return s, n
    raise ConfigError(f"站点 {s.get('site')} 的真实服务器中没有 {node}（{[n['id'] for n in real]}）")


async def measure_level(client: httpx.AsyncClient, site: str, node: str, c: int, warmup_s: float, duration_s: float,
                        n_in: int, n_out: int, model: str, rng: random.Random) -> Level:
    lv = Level(c)
    url = site + "/v1/chat/completions"
    window: list[float] = []  # [开始, 结束]（monotonic）
    done: list[dict] = []
    stop = asyncio.Event()

    async def worker() -> None:
        while not stop.is_set():
            prompt = " ".join(rng.choice(VOCAB) for _ in range(n_in))
            body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": n_out,
                    "ignore_eos": True, "stream": True, "stream_options": {"include_usage": True}}
            t0 = time.monotonic()
            stamps: list[float] = []
            tokens = None
            try:
                async with client.stream("POST", url, json=body) as r:
                    if r.status_code != 200:
                        await r.aread()
                        lv.errors += 1
                        await asyncio.sleep(0.5)
                        continue
                    async for line in r.aiter_lines():
                        if not line.startswith("data: ") or line == "data: [DONE]":
                            continue
                        msg = json.loads(line[6:])
                        if msg.get("usage"):
                            tokens = msg["usage"].get("completion_tokens")
                        if any((ch.get("delta") or {}).get("content") for ch in msg.get("choices") or []):
                            stamps.append(time.monotonic())
            except (httpx.HTTPError, json.JSONDecodeError):
                if not stop.is_set():
                    lv.errors += 1
                    await asyncio.sleep(0.5)
                continue
            if stamps:
                n = tokens or len(stamps)
                done.append({"t0": t0, "t1": stamps[-1], "ttft": stamps[0] - t0, "n": n,
                             "itl": (stamps[-1] - stamps[0]) / (n - 1) if n > 1 else None})

    tasks = [asyncio.create_task(worker()) for _ in range(c)]
    try:
        await asyncio.sleep(warmup_s if c else 0)
        start = time.monotonic()
        window = [start, start + duration_s]
        while time.monotonic() < window[1]:
            s, n = await _node_state(client, site, node)
            if n["state"] != "on":
                raise ConfigError(f"测量中服务器 {node} 的状态变为 {n['state']}（站点是否在执行容量指令？）")
            lv.samples.append((n.get("it_power_w"), n.get("busy", 0)))
            await asyncio.sleep(SAMPLE_S)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    powers = [p for p, _ in lv.samples if p is not None]
    lv.power_coverage = len(powers) / len(lv.samples) if lv.samples else 0.0
    lv.power_w = statistics.fmean(powers) if powers else None
    lv.busy_mean = statistics.fmean(b for _, b in lv.samples) if lv.samples else 0.0
    inwin = [d for d in done if d["t1"] >= window[0] and d["t1"] <= window[1]]
    lv.requests = len(inwin)
    itls = [d["itl"] for d in inwin if d["itl"] is not None]
    lv.itl_s = statistics.fmean(itls) if itls else None
    lv.ttft_s = statistics.fmean(d["ttft"] for d in inwin) if inwin else None
    lv.throughput_tokens_per_s = sum(d["n"] for d in inwin) / duration_s
    return lv


async def measure_boot(client: httpx.AsyncClient, site: str, node: str, total: int,
                       timeout_s: float = 900.0) -> tuple[float, float | None]:
    """经站点管理接口关闭全部服务器，读取关机状态的功率，再开启并计时至被测服务器重新可服务（state 为 on）。

    返回（开机时间，关机功率）；关机期间功率读数全部缺失时关机功率为 None。
    """
    url = site + "/admin/capacity"
    r = await client.post(url, json={"active_nodes": 0}, headers=admin_headers())
    r.raise_for_status()
    deadline = time.monotonic() + 120.0
    while (await _node_state(client, site, node))[1]["state"] != "off":
        if time.monotonic() > deadline:
            raise ConfigError(f"服务器 {node} 在 120 s 内未关机（容量控制器是否仍在向站点下发指令？测量期间请停止控制器）")
        await asyncio.sleep(0.5)
    powers, end = [], time.monotonic() + OFF_WINDOW_S
    while time.monotonic() < end:
        n = (await _node_state(client, site, node))[1]
        if n["state"] != "off":
            raise ConfigError(f"测量关机功率时服务器 {node} 的状态变为 {n['state']}（测量期间请停止控制器）")
        if n.get("it_power_w") is not None:
            powers.append(n["it_power_w"])
        await asyncio.sleep(SAMPLE_S)
    off_w = statistics.fmean(powers) if powers else None
    t0 = time.monotonic()
    r = await client.post(url, json={"active_nodes": total}, headers=admin_headers())
    r.raise_for_status()
    while (await _node_state(client, site, node))[1]["state"] != "on":
        if time.monotonic() - t0 > timeout_s:
            raise ConfigError(f"服务器 {node} 在 {timeout_s:g} s 内未恢复服务")
        await asyncio.sleep(0.5)
    return time.monotonic() - t0, off_w


def flat_power_warning(levels: list[Level]) -> str | None:
    """各级（含并发 0）的平均功率之差不足 2 W 且不足均值的 1% 时，给出警告：传感器读数可能不随负载变化。"""
    ps = [x.power_w for x in levels if x.power_w is not None]
    if len(ps) < 2:
        return None
    lo, hi = min(ps), max(ps)
    if hi - lo < max(2.0, 0.01 * sum(ps) / len(ps)):
        return (f"各级的功率读数几乎相同（{lo:.1f}–{hi:.1f} W）：传感器读数可能不随负载变化（如 BMC 的读数长期不更新），"
                "测量结果不宜用于生成服务器类型。用 cpc sensors 在加负载前后读取以确认；可改用 RAPL（measurement.host: cpu_dram）")
    return None


async def run_measure(site: str, node: str | None, levels: list[int], warmup_s: float, duration_s: float, n_in: int,
                      n_out: int, model: str, boot: bool, seed: int = 0, log=print,
                      transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict, list[Level]]:
    """transport 仅供测试注入。"""
    site = site.rstrip("/")
    rng = random.Random(seed)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), trust_env=False, transport=transport) as client:
        s, n = await pick_node(client, site, node)
        node = n["id"]
        if n["state"] != "on":
            raise ConfigError(f"服务器 {node} 未开启（{n['state']}）")
        bad = [c for c in levels if c < 0 or c > n["slots"]]
        if bad:
            raise ConfigError(f"并发 {bad} 超出服务器 {node} 的并发上限 {n['slots']}")
        meta = {"site": s["site"], "node": node, "slots": n["slots"], "power_scope": s.get("power_scope"),
                "sensors": sorted((s.get("components", {}).get("sensors") or {}).keys()),
                "input_tokens": n_in, "output_tokens": n_out, "warmup_s": warmup_s, "duration_s": duration_s}
        out = []
        for c in levels:
            log(f"  并发 {c}：预热 {warmup_s if c else 0:g} s，测量 {duration_s:g} s …")
            lv = await measure_level(client, site, node, c, warmup_s, duration_s, n_in, n_out, model, rng)
            p = "缺失" if lv.power_w is None else f"{lv.power_w:.1f} W"
            itl = "—" if lv.itl_s is None else f"{lv.itl_s * 1000:.1f} ms"
            log(f"    功率 {p}（完整率 {lv.power_coverage:.0%}）  在服务 {lv.busy_mean:.2f}  token 间隔 {itl}  "
                f"吞吐 {lv.throughput_tokens_per_s:.1f}/s  请求 {lv.requests}  失败 {lv.errors}")
            out.append(lv)
        if boot:
            log(f"  关机功率与开机时间：关闭服务器（读取功率 {OFF_WINDOW_S:g} s）后重新开启 …")
            try:
                boot_s, off_w = await measure_boot(client, site, node, s["nodes_total"])
                meta["boot_s"] = round(boot_s, 1)
                if off_w is not None:
                    meta["off_w"] = round(off_w, 1)
                log(f"    关机功率 {'缺失' if off_w is None else f'{off_w:.1f} W'}，开机到可服务 {meta['boot_s']:g} s")
            except ConfigError as e:  # 开机时间测量失败不影响已完成的功率测量；原因写入数据集
                meta["boot_error"] = str(e)
                log(f"    开机时间测量失败：{e}")
    return meta, out
