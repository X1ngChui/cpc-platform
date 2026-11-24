"""负载数据集（kind: workload）：读取、变换与来源区域分配。

数据要求（类别规范，见 profiles.py）：资源 requests 的每行是一个请求，核心字段为 arrival_s、input_tokens、output_tokens，
按 arrival_s 非降序排列。扩展字段由场景 workload.attributes 显式选择后作为请求属性传给策略（RequestInfo.attrs）。

变换（TraceTransform，均为显式参数）：
    start_s, duration_s  时间窗（原始 trace 秒）；超出数据覆盖范围时报错
    time_scale           时间压缩：变换后时间 = (t − start_s) / time_scale
    rate_scale           到达率缩放：≤ 1 时每个请求以该概率独立保留；> 1 时叠加 ⌈rate_scale⌉ 个稀疏化副本，
                         每个副本保留概率 rate_scale / ⌈rate_scale⌉（取整数时各副本相同，请求在同一时刻重复）
    seed                 缩放与来源区域分配的随机种子
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import numpy as np
import pyarrow as pa

from .descriptor import DatasetError, Descriptor, load_descriptor
from .table import read_table

CORE = ("arrival_s", "input_tokens", "output_tokens")


@dataclass
class WorkloadTrace:
    t: np.ndarray  # 相对到达时间（秒），非降序
    input_tokens: np.ndarray
    output_tokens: np.ndarray
    attrs: dict[str, list] = field(default_factory=dict)  # 选定扩展字段：字段名 → 逐请求取值
    origin: datetime | None = None  # t = 0 对应的绝对时间（数据集 cpc.origin）
    ref: str = ""
    notes: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def duration_s(self) -> float:
        return float(self.t[-1]) if len(self.t) else 0.0

    def take(self, idx: np.ndarray) -> "WorkloadTrace":
        return replace(self, t=self.t[idx], input_tokens=self.input_tokens[idx], output_tokens=self.output_tokens[idx],
                       attrs={k: [v[i] for i in idx] for k, v in self.attrs.items()})

    def attrs_of(self, i: int) -> dict:
        return {k: v[i] for k, v in self.attrs.items()}

    def summary(self) -> dict:
        n, dur = len(self), self.duration_s
        return {"ref": self.ref, "requests": n, "duration_s": round(dur, 1), "mean_rps": round(n / dur, 3) if dur else None,
                "input_tokens_mean": round(float(self.input_tokens.mean()), 1) if n else None,
                "output_tokens_mean": round(float(self.output_tokens.mean()), 1) if n else None,
                "origin": self.origin.isoformat() if self.origin else None, "attributes": sorted(self.attrs), **self.notes}


def load_trace(d: Descriptor, attributes: list[str] | tuple[str, ...] = ()) -> WorkloadTrace:
    if d.kind != "workload":
        raise DatasetError(f"数据集 {d.ref} 的类别为 {d.kind}，需要 workload")
    attributes = list(attributes)
    dup = set(attributes) & set(CORE)
    if dup:
        raise DatasetError(f"workload.attributes 不应包含核心字段 {sorted(dup)}")
    t = read_table(d, "requests", list(CORE) + attributes)
    arr = t.column("arrival_s").to_numpy().astype(float)
    if len(arr) > 1 and np.any(np.diff(arr) < 0):
        raise DatasetError(f"数据集 {d.ref} 的 requests 须按 arrival_s 非降序排列")
    origin = None
    if "origin" in d.cpc:
        origin = datetime.fromisoformat(str(d.cpc["origin"]).replace("Z", "+00:00"))
    attrs = {}
    for a in attributes:
        col = t.column(a)
        attrs[a] = col.to_pylist() if pa.types.is_list(col.type) or pa.types.is_string(col.type) else col.to_numpy(zero_copy_only=False).tolist()
    return WorkloadTrace(t=arr, input_tokens=t.column("input_tokens").to_numpy(), output_tokens=t.column("output_tokens").to_numpy(),
                         attrs=attrs, origin=origin, ref=d.ref)


@dataclass(frozen=True)
class TraceTransform:
    start_s: float = 0.0
    duration_s: float | None = None
    time_scale: float = 1.0
    rate_scale: float = 1.0
    seed: int = 1

    def apply(self, tr: WorkloadTrace) -> WorkloadTrace:
        if self.time_scale <= 0 or self.rate_scale <= 0:
            raise ValueError("time_scale 与 rate_scale 须为正数")
        end = tr.duration_s
        if self.start_s < 0 or self.start_s >= end:
            raise DatasetError(f"时间窗起点 {self.start_s} s 超出 {tr.ref} 的覆盖范围 [0, {end:.0f}] s")
        stop = end if self.duration_s is None else self.start_s + self.duration_s
        if stop > end + 1e-9:
            raise DatasetError(f"时间窗 [{self.start_s}, {stop}] s 超出 {tr.ref} 的覆盖范围 [0, {end:.0f}] s")
        upper = tr.t <= stop if self.duration_s is None else tr.t < stop  # 未给出时长时包含最后一个请求
        idx = np.nonzero((tr.t >= self.start_s) & upper)[0]
        out = tr.take(idx)
        out.t = (out.t - self.start_s) / self.time_scale
        if out.origin is not None:
            out.origin = out.origin + timedelta(seconds=self.start_s)
        out.notes = {**tr.notes, "window_s": [self.start_s, stop], "time_scale": self.time_scale, "rate_scale": self.rate_scale}
        if self.rate_scale != 1.0:
            rng = np.random.default_rng(self.seed)
            k = math.ceil(self.rate_scale)
            p = self.rate_scale / k
            sel = np.concatenate([np.nonzero(rng.random(len(out)) < p)[0] for _ in range(k)])
            out = out.take(sel[np.argsort(out.t[sel], kind="stable")])
        return out


def assign_sources(n: int, sources: list[str], shares: list[float], seed: int) -> np.ndarray:
    """按比例把 n 个请求随机分配到来源区域。"""
    p = np.asarray(shares, dtype=float)
    if (p < 0).any() or p.sum() <= 0:
        raise ValueError(f"来源区域的 share 无效：{shares}")
    rng = np.random.default_rng(seed + 7919)
    return np.asarray(sources)[rng.choice(len(sources), size=n, p=p / p.sum())]


def load_workload(scn) -> WorkloadTrace:
    """按场景 workload 配置读取数据集并应用变换。"""
    w = scn.workload
    d = load_descriptor(w.dataset, scn.base_dir)
    tr = TraceTransform(w.start_s, w.duration_s, w.time_scale, w.rate_scale, w.seed).apply(load_trace(d, w.attributes))
    if not len(tr):
        raise DatasetError(f"负载 {tr.ref} 在所选时间窗内没有请求")
    return tr
