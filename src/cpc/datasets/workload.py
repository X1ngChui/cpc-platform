"""推理负载数据集：请求到达时间、输入/输出 token 数，可选模型名与会话标识。

接入的公开数据集（清单中记录版本、来源、许可、校验和与引用）：
    azure-llm-2023-conv / -code   Azure LLM Inference Trace 2023（1 小时；Splitwise，ISCA 2024），CC-BY-4.0
    azure-llm-2024-conv / -code   Azure LLM Inference Trace 2024（1 周；DynamoLLM，HPCA 2025），CC-BY-4.0
    burstgpt-1                    BurstGPT v2.0 的 BurstGPT_1.csv（前 2 个月；KDD 2025），CC-BY-4.0
    mooncake-fast25-*             Mooncake FAST'25 trace（conversation / toolagent / synthetic），Apache-2.0
测试用：
    synthetic-poisson             日内波动的非齐次泊松到达 + 对数正态 token 长度（只用于单元测试与示例）

变换（TraceTransform，均为显式参数）：时间窗截取（start_s、duration_s，单位为原始 trace 秒）、
时间压缩（time_scale > 1 表示压缩）、到达率缩放（rate_scale：≤ 1 为独立稀疏化；> 1 为 ⌈rate_scale⌉ 个稀疏化副本的叠加，
每个副本保留概率 rate_scale / ⌈rate_scale⌉；rate_scale 为整数时各副本完全相同，即每个请求在同一时刻重复多次）。
来源区域分配：按场景 sources[].share 的比例随机分配（assign_sources）。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import numpy as np

from .base import Dataset, DatasetError, Manifest, RemoteFile, register_dataset


@dataclass
class WorkloadTrace:
    t: np.ndarray  # 相对到达时间（秒），升序
    input_tokens: np.ndarray
    output_tokens: np.ndarray
    model: np.ndarray | None = None  # 每个请求的模型名（数据集提供时）
    session: np.ndarray | None = None  # 会话标识（数据集提供时）
    origin: datetime | None = None  # t = 0 对应的绝对时间（数据集提供时）
    ref: str = ""  # 出处：数据集名@版本
    notes: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def duration_s(self) -> float:
        return float(self.t[-1]) if len(self.t) else 0.0

    def take(self, idx: np.ndarray) -> "WorkloadTrace":
        return replace(self, t=self.t[idx], input_tokens=self.input_tokens[idx], output_tokens=self.output_tokens[idx],
                       model=None if self.model is None else self.model[idx],
                       session=None if self.session is None else self.session[idx])

    def summary(self) -> dict:
        n = len(self)
        dur = self.duration_s
        return {"ref": self.ref, "requests": n, "duration_s": round(dur, 1), "mean_rps": round(n / dur, 3) if dur else None,
                "input_tokens_mean": round(float(self.input_tokens.mean()), 1) if n else None,
                "output_tokens_mean": round(float(self.output_tokens.mean()), 1) if n else None,
                "origin": self.origin.isoformat() if self.origin else None, **self.notes}


@dataclass(frozen=True)
class TraceTransform:
    start_s: float = 0.0  # 时间窗起点（原始 trace 秒）
    duration_s: float | None = None  # 时间窗长度（原始 trace 秒）；None 表示到 trace 末尾
    time_scale: float = 1.0  # 时间压缩倍数：变换后时间 = (t − start_s) / time_scale
    rate_scale: float = 1.0  # 到达率缩放倍数
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
        idx = np.nonzero((tr.t >= self.start_s) & (tr.t < stop))[0]
        out = tr.take(idx)
        out.t = (out.t - self.start_s) / self.time_scale
        if out.origin is not None:
            out.origin = out.origin + timedelta(seconds=self.start_s)
        out.notes = {**tr.notes, "window_s": [self.start_s, stop], "time_scale": self.time_scale, "rate_scale": self.rate_scale}
        if self.rate_scale != 1.0:
            out = self._rescale(out)
        return out

    def _rescale(self, tr: WorkloadTrace) -> WorkloadTrace:
        rng = np.random.default_rng(self.seed)
        k = math.ceil(self.rate_scale)
        p = self.rate_scale / k
        parts = [np.nonzero(rng.random(len(tr)) < p)[0] for _ in range(k)]
        idx = np.concatenate(parts)
        order = np.argsort(tr.t[idx], kind="stable")
        return tr.take(idx[order])


def assign_sources(n: int, sources: list[str], shares: list[float], seed: int) -> np.ndarray:
    """按比例把 n 个请求随机分配到来源区域。"""
    p = np.asarray(shares, dtype=float)
    if (p < 0).any() or p.sum() <= 0:
        raise ValueError(f"来源区域的 share 无效：{shares}")
    rng = np.random.default_rng(seed + 7919)
    return np.asarray(sources)[rng.choice(len(sources), size=n, p=p / p.sum())]


class WorkloadDataset(Dataset):
    """负载数据集：load_trace() 返回完整 trace（已校验文件）。"""

    def arrays(self) -> dict[str, np.ndarray]:
        raise NotImplementedError

    def load_trace(self) -> WorkloadTrace:
        a = self.arrays()
        origin = None
        if "origin_us" in a and int(a["origin_us"]) >= 0:
            origin = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(a["origin_us"]))
        model = session = None
        if "model_code" in a:
            model = a["model_names"][a["model_code"]]
        if "session" in a:
            session = a["session"]
        return WorkloadTrace(t=a["t"].astype(float), input_tokens=a["input_tokens"].astype(np.int64),
                             output_tokens=a["output_tokens"].astype(np.int64), model=model, session=session,
                             origin=origin, ref=self.ref, notes=self.notes())

    def notes(self) -> dict:
        return {}


def _chunks(f, n: int = 1_000_000):
    while lines := f.readlines(n * 48):
        yield lines


def _naive_utc(s: str) -> str:
    """去掉 UTC 偏移后缀（Azure 2024 trace 为 +00:00），截断到微秒（Azure 2023 trace 的小数部分为 7 位）。"""
    i = s.find("+", 10)
    if i >= 0:
        if s[i:] != "+00:00":
            raise DatasetError(f"时间戳的时区偏移不是 UTC：{s}")
        s = s[:i]
    return s[:26]


def _parse_timestamped_csv(path, ts_col: int, cols: list[int]) -> tuple[np.ndarray, list[np.ndarray]]:
    """解析时间戳列为 ISO 格式（UTC 或未注明时区）的 CSV，返回 Unix 微秒与指定整数列。"""
    ts_parts, col_parts = [], [[] for _ in cols]
    with open(path, encoding="utf-8") as f:
        header = f.readline()
        if not header:
            raise DatasetError(f"{path} 为空文件")
        for lines in _chunks(f):
            rows = [ln.rstrip("\n").split(",") for ln in lines if ln.strip()]
            ts = np.array([_naive_utc(r[ts_col]) for r in rows], dtype="datetime64[us]")
            ts_parts.append(ts.astype(np.int64))
            for k, c in enumerate(cols):
                col_parts[k].append(np.array([int(r[c]) for r in rows], dtype=np.int64))
    return np.concatenate(ts_parts), [np.concatenate(p) for p in col_parts]


class _AzureTrace(WorkloadDataset):
    """Azure LLM Inference Trace：列 TIMESTAMP, ContextTokens, GeneratedTokens。"""

    FILE: ClassVar[str]
    TZ_NOTE: ClassVar[str]

    def arrays(self) -> dict[str, np.ndarray]:
        def build():
            ts, (ctx, gen) = _parse_timestamped_csv(self.path(self.FILE), 0, [1, 2])
            order = np.argsort(ts, kind="stable")
            ts, ctx, gen = ts[order], ctx[order], gen[order]
            return {"t": (ts - ts[0]) / 1e6, "input_tokens": ctx, "output_tokens": gen, "origin_us": np.int64(ts[0])}

        return self.derived("trace", build, [self.FILE])

    def notes(self) -> dict:
        return {"timezone": self.TZ_NOTE}


AZURE_CITE_2023 = ("Patel P, Choukse E, Zhang C, et al. Splitwise: Efficient generative LLM inference using phase splitting. "
                   "ISCA 2024.")
AZURE_CITE_2024 = ("Stojkovic J, Zhang C, Goiri Í, et al. DynamoLLM: Designing LLM Inference Clusters for Performance and "
                   "Energy Efficiency. HPCA 2025.")
A23 = "https://raw.githubusercontent.com/Azure/AzurePublicDataset/790921015d50dd6aae7f7e47f39ba0e235ad6b08/data/"
A24 = "https://github.com/Azure/AzurePublicDataset/releases/download/dataset-llm-2024/"
ADOC = "https://github.com/Azure/AzurePublicDataset/blob/master/"


@register_dataset
class Azure2023Conv(_AzureTrace):
    FILE = "AzureLLMInferenceTrace_conv.csv"
    TZ_NOTE = "时间戳未注明时区（按 UTC 解析）"
    manifest = Manifest(
        name="azure-llm-2023-conv", version="7909210", kind="workload",
        title="Azure LLM Inference Trace 2023（对话服务，2023-11-16 采样 1 小时）",
        source=ADOC + "AzureLLMInferenceDataset2023.md", license="CC-BY-4.0", citation=AZURE_CITE_2023,
        files=(RemoteFile(FILE, A23 + FILE, "2f1e5b666d4e3055fdbba98598ce2ec307767b9064e03e2fa46676dbcc7d0bf8", 719188),))


@register_dataset
class Azure2023Code(_AzureTrace):
    FILE = "AzureLLMInferenceTrace_code.csv"
    TZ_NOTE = "时间戳未注明时区（按 UTC 解析）"
    manifest = Manifest(
        name="azure-llm-2023-code", version="7909210", kind="workload",
        title="Azure LLM Inference Trace 2023（代码补全服务，2023-11-16 采样 1 小时）",
        source=ADOC + "AzureLLMInferenceDataset2023.md", license="CC-BY-4.0", citation=AZURE_CITE_2023,
        files=(RemoteFile(FILE, A23 + FILE, "54e9a6d2a4bd06ba1e060304b900abbc74cbea53de96506e60fe5bb4f2277fb6", 320117),))


@register_dataset
class Azure2024Conv(_AzureTrace):
    FILE = "AzureLLMInferenceTrace_conv_1week.csv"
    TZ_NOTE = "UTC（时间戳带 +00:00）"
    manifest = Manifest(
        name="azure-llm-2024-conv", version="dataset-llm-2024", kind="workload",
        title="Azure LLM Inference Trace 2024（对话服务，2024-05-12 起 1 周）",
        source=ADOC + "AzureLLMInferenceDataset2024.md", license="CC-BY-4.0", citation=AZURE_CITE_2024,
        files=(RemoteFile(FILE, A24 + FILE, "a0cc9b969a9bbf0fd811802cbf4323edd3a209ace791e3799ad4f9207f213941", 1135195393),))


@register_dataset
class Azure2024Code(_AzureTrace):
    FILE = "AzureLLMInferenceTrace_code_1week.csv"
    TZ_NOTE = "UTC（时间戳带 +00:00）"
    manifest = Manifest(
        name="azure-llm-2024-code", version="dataset-llm-2024", kind="workload",
        title="Azure LLM Inference Trace 2024（代码服务，2024-05-10 起 1 周）",
        source=ADOC + "AzureLLMInferenceDataset2024.md", license="CC-BY-4.0", citation=AZURE_CITE_2024,
        files=(RemoteFile(FILE, A24 + FILE, "71de5c55cbc35f8f1ed0b6b7806b4cd1e9764b0058469725a6aac98023a1448f", 691989454),))


@register_dataset
class BurstGPT1(WorkloadDataset):
    """BurstGPT v2.0 的 BurstGPT_1.csv：前 2 个月的请求（ChatGPT 与 GPT-4，对话与 API 调用）。

    参数 include_failed：是否保留失败请求（数据集说明中响应 token 为 0 的记录），须显式给出。
    时间戳为自首日 0:00（数据集所在时区）起的秒数，数据集未给出绝对日期，origin 为 None。
    """

    FILE = "BurstGPT_1.csv"
    manifest = Manifest(
        name="burstgpt-1", version="v2.0", kind="workload",
        title="BurstGPT：ChatGPT 与 GPT-4 服务的请求 trace（前 2 个月）",
        source="https://github.com/HPMLL/BurstGPT", license="CC-BY-4.0",
        citation=("Wang Y, Chen Y, Li Z, et al. BurstGPT: A Real-World Workload Dataset to Optimize LLM Serving Systems. "
                  "KDD 2025. doi:10.1145/3711896.3737413"),
        files=(RemoteFile(FILE, "https://github.com/HPMLL/BurstGPT/releases/download/v2.0/" + FILE, "4bb3783693d0a435686fbfc885615d2349bd067239079fa4b749f2e679e12122", 52283111),))

    def __init__(self, root=None, include_failed: bool | None = None, **params):
        if include_failed is None:
            raise TypeError("须给出参数 include_failed（是否保留响应 token 为 0 的失败请求）")
        super().__init__(root, **params)
        self.include_failed = bool(include_failed)

    def arrays(self) -> dict[str, np.ndarray]:
        def build():
            t, req, resp, model = [], [], [], []
            with open(self.path(self.FILE), encoding="utf-8", newline="") as f:
                r = csv.reader(f)
                header = next(r)
                col = {h: i for i, h in enumerate(header)}
                for row in r:
                    t.append(float(row[col["Timestamp"]]))
                    req.append(int(row[col["Request tokens"]]))
                    resp.append(int(row[col["Response tokens"]]))
                    model.append(row[col["Model"]])
            names, codes = np.unique(np.array(model), return_inverse=True)
            t = np.array(t)
            order = np.argsort(t, kind="stable")
            return {"t": t[order] - t[order][0], "input_tokens": np.array(req)[order], "output_tokens": np.array(resp)[order],
                    "model_code": codes[order].astype(np.int16), "model_names": names, "origin_us": np.int64(-1)}

        a = self.derived("trace", build, [self.FILE])
        if not self.include_failed:
            keep = a["output_tokens"] > 0
            a = {k: (v[keep] if isinstance(v, np.ndarray) and v.shape == keep.shape else v) for k, v in a.items()}
        return a

    def notes(self) -> dict:
        return {"include_failed": self.include_failed, "timezone": "数据集所在时区，未给出绝对日期"}


class _Mooncake(WorkloadDataset):
    """Mooncake FAST'25 trace（JSONL：timestamp 毫秒、input_length、output_length、hash_ids）。"""

    FILE: ClassVar[str]

    def arrays(self) -> dict[str, np.ndarray]:
        def build():
            t, inp, out, blocks = [], [], [], []
            with open(self.path(self.FILE), encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        t.append(d["timestamp"] / 1000.0)
                        inp.append(d["input_length"])
                        out.append(d["output_length"])
                        blocks.append(len(d["hash_ids"]))
            t = np.array(t)
            order = np.argsort(t, kind="stable")
            return {"t": t[order] - t[order][0], "input_tokens": np.array(inp)[order], "output_tokens": np.array(out)[order],
                    "prefix_blocks": np.array(blocks)[order], "origin_us": np.int64(-1)}

        return self.derived("trace", build, [self.FILE])


MC = "https://raw.githubusercontent.com/kvcache-ai/Mooncake/3cca71daccf2a7afb8fe3f0295358f70e3a69fdb/FAST25-release/traces/"
MC_SUMS = {
    "conversation_trace.jsonl": ("b8cbb061a85206d729d91cdc2981f43c9e0d99209dce588d3af5f7934408b9df", 3029533),
    "synthetic_trace.jsonl": ("bd070915a98fc0ed264d7cfef2ce746002eb3076a695ec31ba2674c0111ec131", 1136313),
    "toolagent_trace.jsonl": ("48a2db1a13d3bc05e6330140c64f604ba366df20d3c9e128b5c35a01c1fa5f71", 4415857),
}
MC_CITE = "Qin R, Li Z, He W, et al. Mooncake: Trading More Storage for Less Computation — A KVCache-centric Architecture for Serving LLM Chatbot. FAST 2025."


def _mooncake(workload: str, title: str) -> type[_Mooncake]:
    f = f"{workload}_trace.jsonl"
    cls = type(f"Mooncake_{workload}", (_Mooncake,), {
        "FILE": f,
        "manifest": Manifest(name=f"mooncake-fast25-{workload}", version="3cca71d", kind="workload", title=title,
                             source="https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release", license="Apache-2.0",
                             citation=MC_CITE, files=(RemoteFile(f, MC + f, *MC_SUMS[f]),)),
    })
    return register_dataset(cls)


_mooncake("conversation", "Mooncake FAST'25：对话服务 1 小时采样")
_mooncake("toolagent", "Mooncake FAST'25：工具与智能体服务 1 小时采样")
_mooncake("synthetic", "Mooncake FAST'25：由公开数据集构造、泊松到达的合成负载")


@register_dataset
class SyntheticPoisson(WorkloadDataset):
    """合成负载（只用于单元测试与示例）：λ(h) = rps × (1 + A·cos(2π(h − peak_hour)/24))，h 为 origin 起算的当地小时；
    输入/输出 token 数服从对数正态分布（mean 为算术均值）。参数全部须显式给出。"""

    manifest = Manifest(name="synthetic-poisson", version="1", kind="workload", title="合成负载：日内波动的非齐次泊松到达",
                        source="cpc.datasets.workload.SyntheticPoisson", license="MIT", citation="", synthetic=True)

    def __init__(self, root=None, *, rps: float, duration_s: float, origin: datetime, diurnal_amplitude: float,
                 peak_hour: float, input_tokens: dict, output_tokens: dict, seed: int = 1, **params):
        super().__init__(root, **params)
        self.rps, self.duration_s, self.origin = rps, duration_s, origin
        self.amp, self.peak = diurnal_amplitude, peak_hour
        self.inp, self.out, self.seed = input_tokens, output_tokens, seed

    @staticmethod
    def _lognormal(rng, d: dict, n: int) -> np.ndarray:
        mu = math.log(d["mean"]) - d["sigma"] ** 2 / 2
        return np.clip(rng.lognormal(mu, d["sigma"], n), d.get("min", 1), d["max"]).astype(np.int64)

    def arrays(self) -> dict[str, np.ndarray]:
        rng = np.random.default_rng(self.seed)
        lam_max = self.rps * (1 + abs(self.amp))
        n = rng.poisson(lam_max * self.duration_s)
        t = np.sort(rng.uniform(0.0, self.duration_s, n))
        local = self.origin.astimezone(self.origin.tzinfo)
        h = (local.hour + local.minute / 60 + t / 3600.0) % 24
        lam = self.rps * np.maximum(0.0, 1.0 + self.amp * np.cos(2 * np.pi * (h - self.peak) / 24.0))
        t = t[rng.random(n) * lam_max <= lam]
        us = int((self.origin - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1e6)
        return {"t": t, "input_tokens": self._lognormal(rng, self.inp, len(t)), "output_tokens": self._lognormal(rng, self.out, len(t)),
                "origin_us": np.int64(us)}

    def notes(self) -> dict:
        return {"synthetic": True}


def load_workload(scn) -> WorkloadTrace:
    """按场景 workload 配置加载数据集并应用变换。合成数据集以场景时钟起点作为 origin。"""
    from .base import make_dataset

    w = scn.workload
    params = dict(w.params)
    if w.dataset == SyntheticPoisson.manifest.name:
        params.setdefault("origin", scn.clock.start)
    ds = make_dataset(w.dataset, **params)
    if not isinstance(ds, WorkloadDataset):
        raise DatasetError(f"数据集 {w.dataset} 不是负载数据集")
    tr = TraceTransform(w.start_s, w.duration_s, w.time_scale, w.rate_scale, w.seed).apply(ds.load_trace())
    if not len(tr):
        raise DatasetError(f"负载 {tr.ref} 在所选时间窗内没有请求")
    return tr

