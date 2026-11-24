"""基础模型：节点功率模型、推理性能（时延）模型、广域网时延模型、信号时钟。

这些函数在"仿真站点"和"离散事件仿真器"中共用，保证两种模式下的物理假设一致。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import NetworkConfig, NodeProfile, Scenario, SiteConfig, SourceRegion

# ---------------------------------------------------------------- 功率模型


def gpu_power_w(p: NodeProfile, occupancy: float, cap_ratio: float = 1.0) -> float:
    """单卡功率。占用率为 0 时为空载功率 P_idle；否则 P = P_a + (P_cap − P_a)·occ^α，
    其中 P_a 为在服务时的功率下限 gpu_active_min_w（未给出时取 P_idle），P_cap = max(P_a, cap·P_max)。"""
    occ = min(max(occupancy, 0.0), 1.0)
    if occ == 0.0:
        return p.gpu_idle_w
    base = p.gpu_idle_w if p.gpu_active_min_w is None else p.gpu_active_min_w
    p_cap = max(base, cap_ratio * p.gpu_max_w)
    return base + (p_cap - base) * occ**p.power_exponent


def host_power_w(p: NodeProfile, occupancy: float) -> float:
    occ = min(max(occupancy, 0.0), 1.0)
    return p.host_idle_w + (p.host_max_w - p.host_idle_w) * occ


def node_power_w(p: NodeProfile, state: str, occupancy: float, cap_ratio: float = 1.0) -> tuple[float, float]:
    """返回 (节点 IT 功率, 其中 GPU 总功率)。state ∈ {off, booting, on, draining}。"""
    if state == "off":
        return p.standby_w, 0.0
    if state == "booting":
        g = p.gpus * p.gpu_idle_w
        return p.host_idle_w + g, g
    g = p.gpus * gpu_power_w(p, occupancy, cap_ratio)
    return host_power_w(p, occupancy) + g, g


def node_peak_w(p: NodeProfile, cap_ratio: float = 1.0) -> float:
    return node_power_w(p, "on", 1.0, cap_ratio)[0]


def node_idle_w(p: NodeProfile) -> float:
    return node_power_w(p, "on", 0.0, 1.0)[0]


# ---------------------------------------------------------------- 性能模型


def speed_factor(p: NodeProfile, cap_ratio: float) -> float:
    """功率封顶后的相对速度：cap^β（β<1：解码阶段受显存带宽限制，对频率不敏感）。"""
    cap = min(max(cap_ratio, p.cap_min_ratio), 1.0)
    return cap**p.cap_perf_exponent


def prefill_s(p: NodeProfile, input_tokens: int, cap_ratio: float = 1.0) -> float:
    return input_tokens / (p.prefill_tokens_per_s * speed_factor(p, cap_ratio))


def tpot_s(p: NodeProfile, occupancy: float, cap_ratio: float = 1.0) -> float:
    occ = min(max(occupancy, 0.0), 1.0)
    return p.decode_s_per_token * (1.0 + p.decode_slowdown * occ) / speed_factor(p, cap_ratio)


def service_s(p: NodeProfile, input_tokens: int, output_tokens: int, occupancy: float, cap_ratio: float = 1.0) -> float:
    return prefill_s(p, input_tokens, cap_ratio) + output_tokens * tpot_s(p, occupancy, cap_ratio)


# ---------------------------------------------------------------- 广域网时延


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def rtt_ms(src: SourceRegion, site: SiteConfig, net: NetworkConfig) -> float:
    ov = net.overrides.get(src.id, {})
    if site.id in ov:
        return float(ov[site.id])
    d = haversine_km(src.lat, src.lon, site.lat, site.lon)
    return net.base_ms + 2.0 * d * net.route_factor / net.km_per_ms


def rtt_matrix(scn: Scenario) -> dict[str, dict[str, float]]:
    return {s.id: {d.id: round(rtt_ms(s, d, scn.network), 2) for d in scn.sites} for s in scn.sources}


# ---------------------------------------------------------------- 信号时钟


@dataclass
class SimClock:
    """信号时钟。真实部署时 speedup=1；演示时可加速（如 60：真实 1 分钟 = 信号 1 小时）。

    多个服务之间以电网信号服务的 /v1/clock 为准同步 epoch。
    """

    start: datetime
    speedup: float = 1.0
    epoch_wall: float | None = None

    def __post_init__(self) -> None:
        if self.epoch_wall is None:
            self.epoch_wall = time.time()

    def now(self) -> datetime:
        return self.at_wall(time.time())

    def at_wall(self, wall: float) -> datetime:
        assert self.epoch_wall is not None
        return self.start + timedelta(seconds=(wall - self.epoch_wall) * self.speedup)

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "speedup": self.speedup,
            "epoch_wall": self.epoch_wall,
            "now": self.now().isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SimClock":
        return cls(start=datetime.fromisoformat(d["start"]), speedup=float(d["speedup"]), epoch_wall=float(d["epoch_wall"]))


def diurnal_factor(hour: float, amplitude: float, peak_hour: float) -> float:
    return max(0.0, 1.0 + amplitude * math.cos(2 * math.pi * (hour - peak_hour) / 24.0))
