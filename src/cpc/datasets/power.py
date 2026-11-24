"""功率特性数据集（kind: power_profile）：设备在给定并发下的平均功率与 token 间隔；节点参数标定。

数据要求（类别规范）：资源 measurements 的每行是一次稳态测量，核心字段为 device、device_count、model、concurrency、
power_w（全部设备的平均总功率）、itl_s（平均 token 间隔）。

标定（calibrate）：对同一（device、device_count、model）的测量，以 concurrency / 并发槽数为占用率 u（u > 0）：
    单卡功率      P(u) = P_a + (P_max − P_a) · u^α       （最小二乘；α 在 [0.1, 2] 上网格搜索；P_a 为在服务时的功率下限）
    token 间隔    TPOT(u) = d0 · (1 + k · u)              （线性最小二乘）
测量只覆盖在服务期间，空载功率、主机功率与预填充吞吐不由标定给出。
curves=True 时另给出查表式参数 gpu_power_curve、tpot_curve（各测量点的占用率与单卡功率、token 间隔，按占用率排序）。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .descriptor import DatasetError, Descriptor
from .table import read_table

CORE = ("device", "device_count", "model", "concurrency", "power_w", "itl_s")


@dataclass(frozen=True)
class PowerPoint:
    device: str
    device_count: int
    model: str
    concurrency: float
    power_w: float  # 全部设备的平均总功率
    itl_s: float
    attrs: dict


def load_points(d: Descriptor, where: dict[str, object] | None = None) -> list[PowerPoint]:
    """读取测量；where 为字段等值筛选条件（字段须在描述文件中声明）。"""
    if d.kind != "power_profile":
        raise DatasetError(f"数据集 {d.ref} 的类别为 {d.kind}，需要 power_profile")
    where = where or {}
    extra = [k for k in where if k not in CORE]
    t = read_table(d, "measurements", list(CORE) + extra).to_pylist()
    out = []
    for r in t:
        if all(r.get(k) == v for k, v in where.items()):
            out.append(PowerPoint(r["device"], int(r["device_count"]), r["model"], float(r["concurrency"]), float(r["power_w"]),
                                  float(r["itl_s"]), {k: r[k] for k in extra}))
    return out


@dataclass
class Calibration:
    params: dict  # 可写入场景 node_profiles 的参数
    fit: dict  # 拟合诊断
    provenance: dict

    def to_dict(self) -> dict:
        return asdict(self)


def calibrate(points: list[PowerPoint], slots: int, curves: bool = False) -> Calibration:
    """拟合节点参数。points 须为同一（device、device_count、model）；只使用 concurrency ≤ slots 的测量。"""
    if not points:
        raise DatasetError("没有符合选择条件的测量")
    keys = {(p.device, p.device_count, p.model) for p in points}
    if len(keys) != 1:
        raise DatasetError(f"标定须针对同一组（device、device_count、model），当前为 {sorted(keys)}")
    pts = [p for p in points if 0 < p.concurrency <= slots]
    if len(pts) < 3:
        raise DatasetError(f"0 < concurrency ≤ {slots} 的测量只有 {len(pts)} 次，至少需要 3 次")
    device, n, model = keys.pop()
    u = np.array([p.concurrency / slots for p in pts])
    pw = np.array([p.power_w / n for p in pts])
    itl = np.array([p.itl_s for p in pts])
    best = None
    for a in np.arange(0.1, 2.0001, 0.01):
        x = u ** a
        (i0, c), *_ = np.linalg.lstsq(np.vstack([np.ones_like(x), x]).T, pw, rcond=None)
        sse = float(np.sum((i0 + c * x - pw) ** 2))
        if i0 >= 0 and (best is None or sse < best[0]):
            best = (sse, float(a), float(i0), float(c))
    if best is None:
        raise DatasetError("功率拟合没有得到非负的功率下限，测量不适用于该模型")
    sse, alpha, p_a, c = best
    (b0, b1), *_ = np.linalg.lstsq(np.vstack([np.ones_like(u), u]).T, itl, rcond=None)
    params = {"gpus": n, "gpu_model": device, "slots": slots, "gpu_active_min_w": round(p_a, 1), "gpu_max_w": round(p_a + c, 1),
              "power_exponent": round(alpha, 2), "decode_s_per_token": round(float(b0), 5), "decode_slowdown": round(float(b1 / b0), 3)}
    fit = {"points": len(pts), "occupancy_range": [round(float(u.min()), 3), round(float(u.max()), 3)],
           "rmse_power_w": round(math.sqrt(sse / len(pts)), 2),
           "rmse_itl_ms": round(float(np.sqrt(np.mean((b0 + b1 * u - itl) ** 2))) * 1000, 3)}
    if curves:
        order = np.argsort(u, kind="stable")
        params["gpu_power_curve"] = [[round(float(u[i]), 4), round(float(pw[i]), 1)] for i in order]
        params["tpot_curve"] = [[round(float(u[i]), 4), round(float(itl[i]), 5)] for i in order]
    return Calibration(params, fit, {"device": device, "device_count": n, "model": model})
