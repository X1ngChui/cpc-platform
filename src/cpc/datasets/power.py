"""功率特性数据集：设备型号、负载（批大小 / 吞吐）与功率的对应关系，用于标定节点功率与性能模型。

接入的数据集：
    mlenergy-benchmark-v3-llm   ML.ENERGY Benchmark v3 的 LLM 运行汇总（runs/llm.parquet；H100、B200），Apache-2.0。
                                Hugging Face 上为受控访问（gated）：申请访问后以环境变量 HF_TOKEN 下载。

标定（calibrate）：对选定的（模型、GPU、任务、GPU 数）的各次运行，以平均批大小 / 并发槽数为占用率 u（u > 0）：
    单卡功率   P(u) = P_a + (P_max − P_a) · u^α            （最小二乘；α 在 [0.1, 2] 上网格搜索；P_a 为在服务时的功率下限）
    每 token 时延 TPOT(u) = d0 · (1 + k · u)                （线性最小二乘）
数据只含在服务期间的运行，不含空载（没有请求）时的功率：空载功率 gpu_idle_w 不由标定给出，须另行实测或显式给定。
主机功率、预填充吞吐不在数据中，标定结果不包含这些参数。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .base import Dataset, DatasetError, Manifest, RemoteFile, register_dataset

HF = "https://huggingface.co/datasets/ml-energy/benchmark-v3/resolve/f4db020a045a92cc212199f889e57d55f9fa6391/"


@dataclass(frozen=True)
class PowerPoint:
    gpu_model: str
    model_id: str
    task: str
    num_gpus: int
    max_num_seqs: int
    avg_batch_size: float
    power_w: float  # 全部 GPU 的平均总功率（W）
    itl_s: float  # 平均 token 间隔（s）
    output_tps: float  # 输出吞吐（token/s）
    energy_per_token_j: float
    stable: bool


class PowerProfileDataset(Dataset):
    def points(self, **filters) -> list[PowerPoint]:
        raise NotImplementedError


@register_dataset
class MlEnergyBenchmarkV3LLM(PowerProfileDataset):
    FILE = "llm.parquet"
    manifest = Manifest(
        name="mlenergy-benchmark-v3-llm", version="f4db020", kind="power_profile",
        title="ML.ENERGY Benchmark v3：LLM 推理运行的能耗、功率、吞吐与时延汇总（NVIDIA H100、B200）",
        source="https://huggingface.co/datasets/ml-energy/benchmark-v3", license="Apache-2.0",
        citation=("Chung J-W, Ma J J, Wu R, et al. The ML.ENERGY Benchmark: Toward Automated Inference Energy Measurement "
                  "and Optimization. NeurIPS Datasets and Benchmarks, 2025."),
        files=(RemoteFile(FILE, HF + "runs/llm.parquet", "f0cd992c0d0000f8eec6178835e45a8b7656c07d1cd1eac7039f47b125e65a8e", 242560,
                          auth="hf"),),
        notes="受控访问数据集：在 Hugging Face 页面申请访问（自动批准）后设置 HF_TOKEN。")

    def rows(self) -> list[dict]:
        self.verify()
        try:
            import pyarrow.parquet as pq
        except ImportError as e:
            raise DatasetError('读取 parquet 需要 pyarrow（pip install "cpc-platform[data]"）') from e
        return pq.read_table(self.path(self.FILE)).to_pylist()

    def points(self, model: str | None = None, gpu: str | None = None, task: str | None = None, num_gpus: int | None = None,
               stable_only: bool = True) -> list[PowerPoint]:
        out = []
        for r in self.rows():
            if (model and r["model_id"] != model) or (gpu and r["gpu_model"] != gpu) or (task and r["task"] != task):
                continue
            if num_gpus is not None and r["num_gpus"] != num_gpus:
                continue
            if stable_only and not r["is_stable"]:
                continue
            out.append(PowerPoint(r["gpu_model"], r["model_id"], r["task"], int(r["num_gpus"]), int(r["max_num_seqs"]),
                                  float(r["avg_batch_size"]), float(r["avg_power_watts"]), float(r["mean_itl_ms"]) / 1000.0,
                                  float(r["output_throughput_tokens_per_sec"]), float(r["energy_per_token_joules"]),
                                  bool(r["is_stable"])))
        return out


@dataclass
class Calibration:
    params: dict  # 可直接写入场景 node_profiles 的参数
    fit: dict  # 拟合诊断：点数、占用率范围、残差
    provenance: dict

    def to_dict(self) -> dict:
        return asdict(self)


def calibrate(points: list[PowerPoint], slots: int) -> Calibration:
    """按上述模型拟合节点参数。points 须为同一（模型、GPU、任务、GPU 数）的运行；只使用 max_num_seqs ≤ slots 的运行。"""
    keys = {(p.model_id, p.gpu_model, p.task, p.num_gpus) for p in points}
    if len(keys) != 1:
        raise ValueError(f"标定须针对同一组（模型、GPU、任务、GPU 数），当前为 {sorted(keys)}")
    pts = [p for p in points if p.max_num_seqs <= slots]
    if len(pts) < 3:
        raise ValueError(f"max_num_seqs ≤ {slots} 的运行只有 {len(pts)} 次，至少需要 3 次")
    model, gpu, task, ngpu = keys.pop()
    u = np.array([p.avg_batch_size / slots for p in pts])
    pw = np.array([p.power_w / ngpu for p in pts])
    itl = np.array([p.itl_s for p in pts])

    best = None
    for a in np.arange(0.1, 2.0001, 0.01):
        x = u ** a
        (i0, c), *_ = np.linalg.lstsq(np.vstack([np.ones_like(x), x]).T, pw, rcond=None)
        sse = float(np.sum((i0 + c * x - pw) ** 2))
        if i0 >= 0 and (best is None or sse < best[0]):
            best = (sse, float(a), float(i0), float(c))
    if best is None:
        raise ValueError("功率拟合没有得到非负的功率下限，数据不适用于该模型")
    sse, alpha, p_a, c = best
    (b0, b1), *_ = np.linalg.lstsq(np.vstack([np.ones_like(u), u]).T, itl, rcond=None)
    params = {
        "gpus": ngpu, "gpu_model": gpu, "slots": slots,
        "gpu_active_min_w": round(p_a, 1), "gpu_max_w": round(p_a + c, 1), "power_exponent": round(alpha, 2),
        "decode_s_per_token": round(float(b0), 5), "decode_slowdown": round(float(b1 / b0), 3),
    }
    fit = {
        "points": len(pts), "occupancy_range": [round(float(u.min()), 3), round(float(u.max()), 3)],
        "rmse_power_w": round(math.sqrt(sse / len(pts)), 2),
        "rmse_itl_ms": round(float(np.sqrt(np.mean((b0 + b1 * u - itl) ** 2))) * 1000, 3),
    }
    prov = {"model": model, "gpu": gpu, "task": task, "num_gpus": ngpu,
            "runs_max_num_seqs": sorted({p.max_num_seqs for p in pts})}
    return Calibration(params, fit, prov)
