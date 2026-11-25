"""Measuring the power characteristics of a real server: keep fixed concurrency levels on one real server
of a site and record power and inter-token latency.

For every concurrency level c, c clients send streaming requests in a loop (fixed input and output
length, ignore_eos); after warmup_s, during duration_s:
    * every second the measured power (it_power_w) and the requests in service of the server are read
      from /state of the site; samples with missing power are excluded from the mean, and their share
      is recorded;
    * requests completed in the window give the mean inter-token latency (after the first token), TTFT
      and output throughput.
Level 0 reads power only: the online idle power. Optionally the power while off and the boot time are
measured: the server is switched off through the admin interface of the site, its power is read for
OFF_WINDOW_S seconds while off (inference instance stopped, machine running: off_w of the server type),
then it is switched on and timed until it serves again.

The power scope follows the measurement configuration of the site (power_scope in /state: node is the
whole server or host part included; accelerators covers accelerators only). The site should carry no
other load during the measurement; a change of the server state (switched off, draining) is an error.
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
from .live import VOCAB

SAMPLE_S = 1.0  # sampling period of power and requests in service
OFF_WINDOW_S = 60.0  # reading time while off (some BMCs update every few seconds and fluctuate)


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


def _mean(values) -> float | None:
    return statistics.fmean(values) if (values := list(values)) else None


async def _site_state(client: httpx.AsyncClient, site: str) -> dict:
    r = await client.get(site + "/state")
    r.raise_for_status()
    return r.json()


async def _node_state(client: httpx.AsyncClient, site: str, node: str) -> tuple[dict, dict]:
    s = await _site_state(client, site)
    if (n := next((n for n in s.get("nodes", []) if n["id"] == node), None)) is None:
        raise ConfigError(f"site {s.get('site')} has no server {node}")
    return s, n


async def pick_node(client: httpx.AsyncClient, site: str, node: str | None) -> tuple[dict, dict]:
    """The server to measure; without node the site must have exactly one real server that is on."""
    s = await _site_state(client, site)
    if not (real := [n for n in s.get("nodes", []) if n.get("power_source") == "sensors"]):
        raise ConfigError(f"site {s.get('site')} has no real server with measured power")
    ids = [n["id"] for n in real]
    if node is None:
        if len(on := [n for n in real if n["state"] == "on"]) != 1:
            raise ConfigError(f"site {s.get('site')} has {len(on)} real servers that are on ({ids}); choose one with --node")
        return s, on[0]
    if (n := next((n for n in real if n["id"] == node), None)) is None:
        raise ConfigError(f"site {s.get('site')} has no real server {node} ({ids})")
    return s, n


async def measure_level(client: httpx.AsyncClient, site: str, node: str, c: int, warmup_s: float, duration_s: float,
                        n_in: int, n_out: int, model: str, rng: random.Random) -> Level:
    lv = Level(c)
    url = site + "/v1/chat/completions"
    done: list[dict] = []
    stop = asyncio.Event()

    async def worker() -> None:
        while not stop.is_set():
            body = {"model": model, "messages": [{"role": "user", "content": " ".join(rng.choices(VOCAB, k=n_in))}],
                    "max_tokens": n_out, "ignore_eos": True, "stream": True, "stream_options": {"include_usage": True}}
            t0, stamps, tokens = time.monotonic(), [], None
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
                done.append({"t1": stamps[-1], "ttft": stamps[0] - t0, "n": n,
                             "itl": (stamps[-1] - stamps[0]) / (n - 1) if n > 1 else None})

    tasks = [asyncio.create_task(worker()) for _ in range(c)]
    try:
        await asyncio.sleep(warmup_s if c else 0)
        start = time.monotonic()
        end = start + duration_s
        while time.monotonic() < end:
            _, n = await _node_state(client, site, node)
            if n["state"] != "on":
                raise ConfigError(f"server {node} changed to {n['state']} during the measurement "
                                  "(is the site applying capacity instructions?)")
            lv.samples.append((n.get("it_power_w"), n.get("busy", 0)))
            await asyncio.sleep(SAMPLE_S)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    powers = [p for p, _ in lv.samples if p is not None]
    lv.power_coverage = len(powers) / len(lv.samples) if lv.samples else 0.0
    lv.power_w = _mean(powers)
    lv.busy_mean = _mean(b for _, b in lv.samples) or 0.0
    inwin = [d for d in done if start <= d["t1"] <= end]
    lv.requests = len(inwin)
    lv.itl_s = _mean(d["itl"] for d in inwin if d["itl"] is not None)
    lv.ttft_s = _mean(d["ttft"] for d in inwin)
    lv.throughput_tokens_per_s = sum(d["n"] for d in inwin) / duration_s
    return lv


async def _wait_state(client: httpx.AsyncClient, site: str, node: str, state: str, timeout_s: float, hint: str) -> None:
    deadline = time.monotonic() + timeout_s
    while (await _node_state(client, site, node))[1]["state"] != state:
        if time.monotonic() > deadline:
            raise ConfigError(f"server {node} did not reach {state} within {timeout_s:g} s{hint}")
        await asyncio.sleep(0.5)


async def measure_boot(client: httpx.AsyncClient, site: str, node: str, total: int,
                       timeout_s: float = 900.0) -> tuple[float, float | None]:
    """Switch all servers off through the admin interface, read the power while off, switch them on and
    time until the server serves again (state on). Returns (boot time, power while off or None)."""
    url = site + "/admin/capacity"
    hint = " (stop the capacity controller during the measurement)"
    (await client.post(url, json={"active_nodes": 0}, headers=admin_headers())).raise_for_status()
    await _wait_state(client, site, node, "off", 120.0, hint)
    powers, end = [], time.monotonic() + OFF_WINDOW_S
    while time.monotonic() < end:
        if (n := (await _node_state(client, site, node))[1])["state"] != "off":
            raise ConfigError(f"server {node} changed to {n['state']} while its power off was measured{hint}")
        if n.get("it_power_w") is not None:
            powers.append(n["it_power_w"])
        await asyncio.sleep(SAMPLE_S)
    t0 = time.monotonic()
    (await client.post(url, json={"active_nodes": total}, headers=admin_headers())).raise_for_status()
    await _wait_state(client, site, node, "on", timeout_s, "")
    return time.monotonic() - t0, _mean(powers)


def flat_power_warning(levels: list[Level]) -> str | None:
    """Warning when the mean power of all levels (level 0 included) differs by less than 2 W and less
    than 1 % of the mean: the sensor may not follow the load."""
    if len(ps := [x.power_w for x in levels if x.power_w is not None]) < 2:
        return None
    lo, hi = min(ps), max(ps)
    if hi - lo < max(2.0, 0.01 * statistics.fmean(ps)):
        return (f"the power readings of all levels are almost equal ({lo:.1f}-{hi:.1f} W): the sensor may not follow the "
                "load (e.g. a BMC reading that is not updated), so the measurement should not define a server type. "
                "Check with cpc sensors before and under load, or use RAPL (measurement.host: cpu_dram)")
    return None


async def run_measure(site: str, node: str | None, levels: list[int], warmup_s: float, duration_s: float, n_in: int,
                      n_out: int, model: str, boot: bool, seed: int = 0, log=print,
                      transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict, list[Level]]:
    """transport is for tests only."""
    site = site.rstrip("/")
    rng = random.Random(seed)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), trust_env=False, transport=transport) as client:
        s, n = await pick_node(client, site, node)
        node = n["id"]
        if n["state"] != "on":
            raise ConfigError(f"server {node} is not on ({n['state']})")
        if bad := [c for c in levels if not 0 <= c <= n["slots"]]:
            raise ConfigError(f"concurrency {bad} exceeds the limit {n['slots']} of server {node}")
        meta = {"site": s["site"], "node": node, "slots": n["slots"], "power_scope": s.get("power_scope"),
                "sensors": sorted((s.get("components", {}).get("sensors") or {}).keys()),
                "input_tokens": n_in, "output_tokens": n_out, "warmup_s": warmup_s, "duration_s": duration_s}
        out = []
        for c in levels:
            log(f"  concurrency {c}: warm-up {warmup_s if c else 0:g} s, measurement {duration_s:g} s ...")
            lv = await measure_level(client, site, node, c, warmup_s, duration_s, n_in, n_out, model, rng)
            p = "missing" if lv.power_w is None else f"{lv.power_w:.1f} W"
            itl = "-" if lv.itl_s is None else f"{lv.itl_s * 1000:.1f} ms"
            log(f"    power {p} ({lv.power_coverage:.0%} complete)  in service {lv.busy_mean:.2f}  inter-token {itl}  "
                f"throughput {lv.throughput_tokens_per_s:.1f}/s  requests {lv.requests}  errors {lv.errors}")
            out.append(lv)
        if boot:
            log(f"  power while off and boot time: switching off (reading power for {OFF_WINDOW_S:g} s), then on ...")
            try:
                boot_s, off_w = await measure_boot(client, site, node, s["nodes_total"])
                meta["boot_s"] = round(boot_s, 1)
                if off_w is not None:
                    meta["off_w"] = round(off_w, 1)
                log(f"    power while off {'missing' if off_w is None else f'{off_w:.1f} W'}, "
                    f"boot to service {meta['boot_s']:g} s")
            except ConfigError as e:  # a failed boot measurement keeps the power levels; the reason goes to the dataset
                meta["boot_error"] = str(e)
                log(f"    boot-time measurement failed: {e}")
    return meta, out
