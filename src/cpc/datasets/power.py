"""Power-profile datasets (kind: power_profile): mean power and inter-token latency of a device at a
given concurrency; server-type tables generated from the measurements.

Requirements (kind specification): each row of the resource measurements is one steady-state
measurement with the core fields device, device_count, model, concurrency, power_w (mean total power
of all devices) and itl_s (mean inter-token latency).

Server type (profile_from_points): for the measurements of one (device, device_count, model), with
occupancy u = concurrency / slots:
    power_w   [[0, online idle power], [u_i, power_w_i + host power], ...]
    tpot_s    [[u_i, itl_s_i], ...]
The measurements cover the serving periods only: the online idle power (idle_w) and the host power
(host_w, added to every point) come from the user; prefill throughput and boot time are not part of
the measurements either and are written when given.
"""

from __future__ import annotations

from dataclasses import dataclass

from .descriptor import DatasetError, Descriptor
from .table import read_table

CORE = ("device", "device_count", "model", "concurrency", "power_w", "itl_s")


@dataclass(frozen=True, slots=True)
class PowerPoint:
    device: str
    device_count: int
    model: str
    concurrency: float
    power_w: float  # mean total power of all devices
    itl_s: float
    attrs: dict


def load_points(d: Descriptor, where: dict[str, object] | None = None) -> list[PowerPoint]:
    """Read the measurements; where gives equality filters on fields declared in the descriptor."""
    if d.kind != "power_profile":
        raise DatasetError(f"dataset {d.ref} has kind {d.kind}; power_profile is required")
    where = where or {}
    extra = [k for k in where if k not in CORE]
    return [PowerPoint(r["device"], int(r["device_count"]), r["model"], float(r["concurrency"]), float(r["power_w"]),
                       float(r["itl_s"]), {k: r[k] for k in extra})
            for r in read_table(d, "measurements", [*CORE, *extra]).to_pylist()
            if all(r.get(k) == v for k, v in where.items())]


def profile_from_points(points: list[PowerPoint], slots: int, idle_w: float, host_w: float = 0.0,
                        prefill_tokens_per_s: float | None = None, boot_s: float | None = None, source: str = "",
                        idle_measured: bool = False, off_w: float | None = None) -> dict:
    """Server type (an entry of node_profiles) from one group of measurements with 0 < concurrency <= slots.

    idle_measured says the online idle power was measured (concurrency 0 of cpc measure); it only
    changes the description.
    """
    if not points:
        raise DatasetError("no measurement matches the selection")
    if len(keys := {(p.device, p.device_count, p.model) for p in points}) != 1:
        raise DatasetError(f"select measurements of one (device, device_count, model), got {sorted(keys)}")
    if not (pts := sorted((p for p in points if 0 < p.concurrency <= slots), key=lambda p: p.concurrency)):
        raise DatasetError(f"no measurement with 0 < concurrency <= {slots}")
    us = [round(p.concurrency / slots, 4) for p in pts]
    if len(set(us)) != len(us):
        raise DatasetError(f"several measurements have the same occupancy ({us}); narrow the selection with --where "
                           "(e.g. task)")
    device, n, model = keys.pop()
    parts = [f"{n}x{device}, {model}; power and inter-token latency from {source or 'a power-profile dataset'}",
             f"online idle power {idle_w:g} W {'measured' if idle_measured else 'given'}"]
    if host_w:
        parts.append(f"host power {host_w:g} W given")
    prof: dict = {
        "description": ", ".join(parts),
        "slots": slots,
        "power_w": [[0.0, round(idle_w, 1)]] + [[u, round(p.power_w + host_w, 1)] for u, p in zip(us, pts)],
        "tpot_s": [[u, round(p.itl_s, 5)] for u, p in zip(us, pts)],
    }
    for key, value in (("prefill_tokens_per_s", prefill_tokens_per_s), ("boot_s", boot_s)):
        if value is not None:
            prof[key] = value
    if off_w is not None:
        prof["off_w"] = round(off_w, 1)
    return prof
