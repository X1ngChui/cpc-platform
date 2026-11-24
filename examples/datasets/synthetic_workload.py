"""合成负载（日内波动的非齐次泊松到达）→ workload 数据集。只用于单元测试、冒烟测试与功能演示，不作为实验数据。

到达率 λ(h) = rps × max(0, 1 + A·cos(2π(h − peak_hour)/24))，h 为 origin 所在时区的当地小时；
输入、输出 token 数服从对数正态分布（参数 mean 为算术均值，sigma 为对数标准差），截断到 [1, max]。

用法：
    python examples/datasets/synthetic_workload.py --out 目录 --origin 2026-07-15T00:00:00+08:00 --hours 48 --rps 12 \\
        --amplitude 0.5 --peak-hour 20 --input 800,0.8,8192 --output 200,0.7,1024 --seed 1
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa

from _util import WORKLOAD_CORE

from cpc.datasets.build import write_dataset


def _lognormal(rng, mean: float, sigma: float, cap: int, n: int) -> np.ndarray:
    mu = math.log(mean) - sigma ** 2 / 2
    return np.clip(rng.lognormal(mu, sigma, n), 1, cap).astype(np.int64)


def generate(out: str | Path, *, origin: datetime, hours: float, rps: float, amplitude: float, peak_hour: float,
             input_tokens: tuple[float, float, int], output_tokens: tuple[float, float, int], seed: int,
             name: str = "synthetic-workload"):
    if origin.tzinfo is None:
        raise ValueError("origin 须带时区")
    rng = np.random.default_rng(seed)
    dur = hours * 3600.0
    lam_max = rps * (1 + abs(amplitude))
    n = rng.poisson(lam_max * dur)
    t = np.sort(rng.uniform(0.0, dur, n))
    h = (origin.hour + origin.minute / 60 + t / 3600.0) % 24
    lam = rps * np.maximum(0.0, 1.0 + amplitude * np.cos(2 * np.pi * (h - peak_hour) / 24.0))
    t = t[rng.random(n) * lam_max <= lam]
    table = pa.table({"arrival_s": t, "input_tokens": _lognormal(rng, *input_tokens, len(t)),
                      "output_tokens": _lognormal(rng, *output_tokens, len(t))})
    params = {"hours": hours, "rps": rps, "amplitude": amplitude, "peak_hour": peak_hour, "input_tokens": list(input_tokens),
              "output_tokens": list(output_tokens), "seed": seed}
    return write_dataset(out, name=name, version="1", kind="workload", table=table, fields=WORKLOAD_CORE,
                         title="合成负载：日内波动的非齐次泊松到达（只用于测试与演示）", licenses=[{"name": "CC0-1.0"}],
                         cpc={"origin": origin.isoformat(), "synthetic": True,
                              "conversion": {"script": "examples/datasets/synthetic_workload.py", **params}})


def _triple(s: str) -> tuple[float, float, int]:
    a, b, c = s.split(",")
    return float(a), float(b), int(c)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--origin", required=True)
    ap.add_argument("--hours", type=float, required=True)
    ap.add_argument("--rps", type=float, required=True)
    ap.add_argument("--amplitude", type=float, required=True)
    ap.add_argument("--peak-hour", type=float, required=True)
    ap.add_argument("--input", type=_triple, required=True, help="均值,对数标准差,上限")
    ap.add_argument("--output", type=_triple, required=True, help="均值,对数标准差,上限")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--name", default="synthetic-workload")
    a = ap.parse_args()
    d = generate(a.out, origin=datetime.fromisoformat(a.origin), hours=a.hours, rps=a.rps, amplitude=a.amplitude,
                 peak_hour=a.peak_hour, input_tokens=a.input, output_tokens=a.output, seed=a.seed, name=a.name)
    print(f"{d.ref} → {d.root}")


if __name__ == "__main__":
    main()
