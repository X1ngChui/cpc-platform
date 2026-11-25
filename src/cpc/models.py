"""Base models: server model (power and latency, interpolated from tables), wide-area latency model,
signal clock.

The server model is evaluated by the data-centre runtime, which both the simulator and the site
agent (for emulated servers) drive, so both modes share the same physical assumptions.
"""

from __future__ import annotations

import math
import time
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import NetworkConfig, NodeProfile, Scenario, SiteConfig, SourceRegion

# ---------------------------------------------------------------- power model


def interp(table: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation; values outside the table take the end points."""
    i = bisect_left(table, x, key=lambda p: p[0])
    if i == 0:
        return table[0][1]
    if i == len(table):
        return table[-1][1]
    (x0, y0), (x1, y1) = table[i - 1], table[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return min(max(v, lo), hi)


def idle_w(p: NodeProfile) -> float:
    """Online idle power (the table value at u = 0)."""
    return p.power_w[0][1]


def peak_w(p: NodeProfile) -> float:
    """Full-load power (the largest table value)."""
    return max(y for _, y in p.power_w)


def cap_limit_w(p: NodeProfile, cap_ratio: float) -> float:
    """Power limit at cap ratio c: idle + c x (peak - idle)."""
    return idle_w(p) + _clamp(cap_ratio, p.cap_min_ratio) * (peak_w(p) - idle_w(p))


def power_w(p: NodeProfile, state: str, occupancy: float, cap_ratio: float = 1.0) -> float:
    """Server IT power in state off / booting / on / draining; booting counts as online idle; capped."""
    match state:
        case "off":
            return p.off_w
        case "booting":
            return idle_w(p)
    v = interp(p.power_w, _clamp(occupancy))
    return v if cap_ratio >= 1.0 else min(v, cap_limit_w(p, cap_ratio))


# ---------------------------------------------------------------- performance model


def speed_factor(p: NodeProfile, cap_ratio: float) -> float:
    """Relative speed under a power cap: c ** cap_perf_exponent, c clamped to [cap_min_ratio, 1]."""
    return _clamp(cap_ratio, p.cap_min_ratio) ** p.cap_perf_exponent


def prefill_s(p: NodeProfile, input_tokens: int, cap_ratio: float = 1.0) -> float:
    return input_tokens / (p.prefill_tokens_per_s * speed_factor(p, cap_ratio))


def tpot_s(p: NodeProfile, occupancy: float, cap_ratio: float = 1.0) -> float:
    return interp(p.tpot_s, _clamp(occupancy)) / speed_factor(p, cap_ratio)


def service_s(p: NodeProfile, input_tokens: int, output_tokens: int, occupancy: float, cap_ratio: float = 1.0) -> float:
    return prefill_s(p, input_tokens, cap_ratio) + output_tokens * tpot_s(p, occupancy, cap_ratio)


# ---------------------------------------------------------------- wide-area latency

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def rtt_ms(src: SourceRegion, site: SiteConfig, net: NetworkConfig) -> float:
    d = haversine_km(src.lat, src.lon, site.lat, site.lon)
    return net.base_ms + 2.0 * d * net.route_factor / net.km_per_ms


def rtt_matrix(scn: Scenario) -> dict[str, dict[str, float]]:
    return {s.id: {d.id: round(rtt_ms(s, d, scn.network), 2) for d in scn.sites} for s in scn.sources}


# ---------------------------------------------------------------- signal clock


@dataclass
class SimClock:
    """Signal clock. speedup is 1 in real deployments; demonstrations may accelerate it
    (60: one real minute is one signal hour). Services synchronise the epoch from /v1/clock of the
    grid-signal service."""

    start: datetime
    speedup: float = 1.0
    epoch_wall: float = field(default_factory=time.time)

    def now(self) -> datetime:
        return self.at_wall(time.time())

    def at_wall(self, wall: float) -> datetime:
        return self.start + timedelta(seconds=(wall - self.epoch_wall) * self.speedup)

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "speedup": self.speedup, "epoch_wall": self.epoch_wall,
                "now": self.now().isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> SimClock:
        return cls(start=datetime.fromisoformat(d["start"]), speedup=float(d["speedup"]), epoch_wall=float(d["epoch_wall"]))
