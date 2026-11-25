"""Grid-signal datasets (kind: grid_signal): signal values per region and period.

Requirements (kind specification): the resource signals is a long table; each row gives the value
of one signal for one region during one period [start, end). Periods of the same region and signal
do not overlap. cpc.signals of the descriptor declares the unit of each signal (e.g. price:
{unit: CNY/kWh}, carbon_intensity: {unit: kgCO2/kWh, basis: lifecycle}). Gaps between periods and
times outside the covered range are unavailable.

The platform uses price and carbon_intensity; scenarios may pass other signals (renewable share,
marginal carbon intensity, load limits, ...) to policies as extra signals.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pyarrow.compute as pc

from .descriptor import DatasetError, Descriptor
from .table import read_table


class SignalUnavailable(DatasetError):
    """The signal of a region is unavailable at a time (gap, outside the covered range, region not covered)."""


@dataclass(frozen=True, slots=True)
class SignalValue:
    value: float
    label: str | None = None


@dataclass(slots=True)
class _Series:
    start: list[int]  # microseconds (UTC)
    end: list[int]
    value: np.ndarray
    label: list[str | None] | None


def _us(t: datetime) -> int:
    if t.tzinfo is None:
        raise ValueError(f"signal query time needs a UTC offset: {t}")
    return int(t.timestamp() * 1_000_000)


def _dt(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1e6, timezone.utc)


class SignalSet:
    """In-memory index of a grid-signal dataset: (signal, region) -> periods sorted by start."""

    def __init__(self, d: Descriptor):
        if d.kind != "grid_signal":
            raise DatasetError(f"dataset {d.ref} has kind {d.kind}; grid_signal is required")
        self.d = d
        self.meta: dict[str, dict] = dict(d.cpc["signals"])
        has_label = d.resource("signals").field("label") is not None
        t = read_table(d, "signals", ["region", "start", "end", "signal", "value"] + ["label"] * has_label)
        if bad := pc.sum(pc.less_equal(t.column("end"), t.column("start"))).as_py():
            raise DatasetError(f"dataset {d.ref}: {bad} periods do not end after they start")
        if undeclared := sorted(set(pc.unique(t.column("signal")).to_pylist()) - set(self.meta)):
            raise DatasetError(f"dataset {d.ref} contains signals not declared in cpc.signals: {undeclared}")
        start = t.column("start").cast("int64").to_numpy()
        end = t.column("end").cast("int64").to_numpy()
        value = t.column("value").to_numpy(zero_copy_only=False).astype(float)
        lab = t.column("label").to_pylist() if has_label else None
        groups: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
        for i, key in enumerate(zip(t.column("signal").to_pylist(), t.column("region").to_pylist())):
            groups[key].append(i)
        self.series: dict[tuple[str, str], _Series] = {}
        for key, rows in groups.items():
            idx = np.array(rows)[np.argsort(start[rows], kind="stable")]
            s, e = start[idx], end[idx]
            if np.any(s[1:] < e[:-1]):
                raise DatasetError(f"dataset {d.ref}: periods of signal {key[0]}, region {key[1]} overlap")
            self.series[key] = _Series(s.tolist(), e.tolist(), value[idx], [lab[i] for i in idx] if lab else None)

    def signals(self) -> list[str]:
        return sorted(self.meta)

    def regions(self, signal: str) -> list[str]:
        return sorted(r for s, r in self.series if s == signal)

    def unit(self, signal: str) -> str:
        return str(self.meta[signal]["unit"])

    def coverage(self, signal: str, region: str) -> tuple[datetime, datetime]:
        s = self.series[signal, region]
        return _dt(s.start[0]), _dt(s.end[-1])

    def value(self, signal: str, region: str, t: datetime) -> SignalValue:
        if (s := self.series.get((signal, region))) is None:
            raise SignalUnavailable(f"dataset {self.d.ref} has no signal {signal} for region {region}")
        us = _us(t)
        i = bisect.bisect_right(s.start, us) - 1
        if i < 0 or us >= s.end[i]:
            raise SignalUnavailable(f"dataset {self.d.ref}: no {signal} data for region {region} at {t.isoformat()}")
        return SignalValue(float(s.value[i]), s.label[i] if s.label else None)
