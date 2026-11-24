"""电网信号数据集（kind: grid_signal）：按区域与时段的信号值。

数据要求（类别规范）：资源 signals 为长表，每行给出一个区域（region）在一个时段 [start, end) 内一种信号（signal）的值（value）；
同一区域、同一信号的时段互不重叠。描述文件的 cpc.signals 声明每种信号的单位（如 price: {unit: CNY/kWh}、
carbon_intensity: {unit: kgCO2/kWh, basis: lifecycle}）。时段之间的空隙与覆盖范围之外视为数据不可用。

平台使用 price 与 carbon_intensity；其他信号（如可再生能源占比、边际碳强度、负荷限制）可由场景声明为扩展信号传给策略。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pyarrow.compute as pc

from .descriptor import DatasetError, Descriptor
from .table import read_table


class SignalUnavailable(DatasetError):
    """某区域某时刻的信号不可用（时段空隙、覆盖范围之外或区域未收录）。"""


@dataclass(frozen=True)
class SignalValue:
    value: float
    label: str | None = None


@dataclass
class _Series:
    start: list[int]  # 微秒（UTC）
    end: list[int]
    value: np.ndarray
    label: list[str | None] | None


def _us(t: datetime) -> int:
    if t.tzinfo is None:
        raise ValueError(f"信号查询时间须带时区：{t}")
    return int(t.timestamp() * 1_000_000)


class SignalSet:
    """一个电网信号数据集在内存中的索引：(信号, 区域) → 按起点排序的时段序列。"""

    def __init__(self, d: Descriptor):
        if d.kind != "grid_signal":
            raise DatasetError(f"数据集 {d.ref} 的类别为 {d.kind}，需要 grid_signal")
        self.d = d
        self.meta: dict[str, dict] = dict(d.cpc["signals"])
        cols = ["region", "start", "end", "signal", "value"]
        has_label = d.resource("signals").field("label") is not None
        t = read_table(d, "signals", cols + (["label"] if has_label else []))
        bad = pc.sum(pc.less_equal(t.column("end"), t.column("start"))).as_py()
        if bad:
            raise DatasetError(f"数据集 {d.ref} 有 {bad} 个时段的 end 不晚于 start")
        undeclared = sorted(set(pc.unique(t.column("signal")).to_pylist()) - set(self.meta))
        if undeclared:
            raise DatasetError(f"数据集 {d.ref} 含未在 cpc.signals 中声明的信号 {undeclared}")
        start = t.column("start").cast("int64").to_numpy()
        end = t.column("end").cast("int64").to_numpy()
        value = t.column("value").to_numpy(zero_copy_only=False).astype(float)
        sig = t.column("signal").to_pylist()
        reg = t.column("region").to_pylist()
        lab = t.column("label").to_pylist() if has_label else None
        groups: dict[tuple[str, str], list[int]] = {}
        for i, key in enumerate(zip(sig, reg)):
            groups.setdefault(key, []).append(i)
        self.series: dict[tuple[str, str], _Series] = {}
        for key, idx in groups.items():
            idx = np.array(idx)
            idx = idx[np.argsort(start[idx], kind="stable")]
            s, e = start[idx], end[idx]
            if len(idx) > 1 and np.any(s[1:] < e[:-1]):
                raise DatasetError(f"数据集 {d.ref} 中信号 {key[0]}、区域 {key[1]} 的时段有重叠")
            self.series[key] = _Series(s.tolist(), e.tolist(), value[idx], [lab[i] for i in idx] if lab else None)

    def signals(self) -> list[str]:
        return sorted(self.meta)

    def regions(self, signal: str) -> list[str]:
        return sorted(r for s, r in self.series if s == signal)

    def unit(self, signal: str) -> str:
        return str(self.meta[signal]["unit"])

    def coverage(self, signal: str, region: str) -> tuple[datetime, datetime]:
        s = self.series[(signal, region)]
        f = lambda us: datetime.fromtimestamp(us / 1e6, timezone.utc)  # noqa: E731
        return f(s.start[0]), f(s.end[-1])

    def value(self, signal: str, region: str, t: datetime) -> SignalValue:
        s = self.series.get((signal, region))
        if s is None:
            raise SignalUnavailable(f"数据集 {self.d.ref} 没有区域 {region} 的信号 {signal}")
        us = _us(t)
        i = bisect.bisect_right(s.start, us) - 1
        if i < 0 or us >= s.end[i]:
            raise SignalUnavailable(f"数据集 {self.d.ref} 中区域 {region} 的信号 {signal} 在 {t.isoformat()} 没有数据")
        return SignalValue(float(s.value[i]), s.label[i] if s.label else None)
