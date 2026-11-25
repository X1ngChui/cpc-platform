"""Workload datasets (kind: workload): loading, transformation and assignment of source regions.

Requirements (kind specification, see profiles.py): each row of the resource requests is one
request with the core fields arrival_s, input_tokens and output_tokens, ordered by arrival_s.
Extension fields selected by workload.attributes reach policies as request attributes (RequestInfo.attrs).

Transformations (TraceTransform, all explicit parameters):
    start_s, duration_s  time window (original trace seconds); a window outside the data is an error
    time_scale           time compression: t' = (t - start_s) / time_scale
    rate_scale           arrival-rate scaling: <= 1 keeps each request independently with this
                         probability; > 1 superimposes ceil(rate_scale) thinned copies, each keeping a
                         request with probability rate_scale / ceil(rate_scale) (integer factors repeat
                         every request at the same time)
    seed                 random seed of the scaling and the source assignment
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
    t: np.ndarray  # arrival times relative to the origin (s), non-decreasing
    input_tokens: np.ndarray
    output_tokens: np.ndarray
    attrs: dict[str, list] = field(default_factory=dict)  # selected extension fields: name -> value per request
    origin: datetime | None = None  # absolute time of t = 0 (cpc.origin of the dataset)
    ref: str = ""
    notes: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def duration_s(self) -> float:
        return float(self.t[-1]) if len(self.t) else 0.0

    def take(self, idx: np.ndarray) -> WorkloadTrace:
        return replace(self, t=self.t[idx], input_tokens=self.input_tokens[idx], output_tokens=self.output_tokens[idx],
                       attrs={k: [v[i] for i in idx] for k, v in self.attrs.items()})

    def attrs_of(self, i: int) -> dict:
        return {k: v[i] for k, v in self.attrs.items()}

    def summary(self) -> dict:
        n, dur = len(self), self.duration_s
        mean = lambda a: round(float(a.mean()), 1) if n else None  # noqa: E731
        return {"ref": self.ref, "requests": n, "duration_s": round(dur, 1), "mean_rps": round(n / dur, 3) if dur else None,
                "input_tokens_mean": mean(self.input_tokens), "output_tokens_mean": mean(self.output_tokens),
                "origin": self.origin.isoformat() if self.origin else None, "attributes": sorted(self.attrs), **self.notes}


def _values(col: pa.ChunkedArray) -> list:
    if pa.types.is_list(col.type) or pa.types.is_string(col.type):
        return col.to_pylist()
    return col.to_numpy(zero_copy_only=False).tolist()


def load_trace(d: Descriptor, attributes: list[str] | tuple[str, ...] = ()) -> WorkloadTrace:
    if d.kind != "workload":
        raise DatasetError(f"dataset {d.ref} has kind {d.kind}; workload is required")
    if dup := sorted(set(attributes) & set(CORE)):
        raise DatasetError(f"workload.attributes must not include the core fields {dup}")
    t = read_table(d, "requests", [*CORE, *attributes])
    arr = t.column("arrival_s").to_numpy().astype(float)
    if np.any(np.diff(arr) < 0):
        raise DatasetError(f"dataset {d.ref}: requests must be ordered by arrival_s")
    origin = datetime.fromisoformat(str(d.cpc["origin"]).replace("Z", "+00:00")) if "origin" in d.cpc else None
    return WorkloadTrace(t=arr, input_tokens=t.column("input_tokens").to_numpy(),
                         output_tokens=t.column("output_tokens").to_numpy(),
                         attrs={a: _values(t.column(a)) for a in attributes}, origin=origin, ref=d.ref)


@dataclass(frozen=True)
class TraceTransform:
    start_s: float = 0.0
    duration_s: float | None = None
    time_scale: float = 1.0
    rate_scale: float = 1.0
    seed: int = 1

    def apply(self, tr: WorkloadTrace) -> WorkloadTrace:
        if self.time_scale <= 0 or self.rate_scale <= 0:
            raise ValueError("time_scale and rate_scale must be positive")
        end = tr.duration_s
        if not 0 <= self.start_s < end:
            raise DatasetError(f"window start {self.start_s} s is outside {tr.ref}, which covers [0, {end:.0f}] s")
        stop = end if self.duration_s is None else self.start_s + self.duration_s
        if stop > end + 1e-9:
            raise DatasetError(f"window [{self.start_s}, {stop}] s is outside {tr.ref}, which covers [0, {end:.0f}] s")
        upper = tr.t <= stop if self.duration_s is None else tr.t < stop  # without a duration the last request is included
        out = tr.take(np.nonzero((tr.t >= self.start_s) & upper)[0])
        out.t = (out.t - self.start_s) / self.time_scale
        if out.origin is not None:
            out.origin += timedelta(seconds=self.start_s)
        out.notes = tr.notes | {"window_s": [self.start_s, stop], "time_scale": self.time_scale, "rate_scale": self.rate_scale}
        if self.rate_scale != 1.0:
            rng = np.random.default_rng(self.seed)
            k = math.ceil(self.rate_scale)
            p = self.rate_scale / k
            sel = np.concatenate([np.nonzero(rng.random(len(out)) < p)[0] for _ in range(k)])
            out = out.take(sel[np.argsort(out.t[sel], kind="stable")])
        return out


def assign_sources(n: int, sources: list[str], shares: list[float], seed: int) -> np.ndarray:
    """Assign n requests to source regions at random in proportion to their shares."""
    p = np.asarray(shares, dtype=float)
    if (p < 0).any() or p.sum() <= 0:
        raise ValueError(f"invalid source shares: {shares}")
    rng = np.random.default_rng(seed + 7919)
    return np.asarray(sources)[rng.choice(len(sources), size=n, p=p / p.sum())]


def load_workload(scn) -> WorkloadTrace:
    """Load the workload dataset of a scenario and apply its transformations."""
    w = scn.workload
    d = load_descriptor(w.dataset, scn.base_dir)
    tr = TraceTransform(w.start_s, w.duration_s, w.time_scale, w.rate_scale, w.seed).apply(load_trace(d, w.attributes))
    if not len(tr):
        raise DatasetError(f"workload {tr.ref} has no requests in the selected window")
    return tr
