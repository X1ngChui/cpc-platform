"""功率特性数据集（kind: power_profile）：设备在给定并发下的平均功率与 token 间隔；由测量生成服务器类型的表。

数据要求（类别规范）：资源 measurements 的每行是一次稳态测量，核心字段为 device、device_count、model、concurrency、
power_w（全部设备的平均总功率）、itl_s（平均 token 间隔）。

生成服务器类型（profile_from_points）：对同一（device、device_count、model）的测量，以 u = concurrency / slots 为占用率：
    power_w 表   [[0, 在线空载功率], [u_i, power_w_i + 主机功率], ...]
    tpot_s 表    [[u_i, itl_s_i], ...]
测量只覆盖在服务期间：在线空载功率（idle_w）与主机部分功率（host_w，加到每个测量点上）须由使用者给出；
预填充吞吐与开机时间也不在测量中，给出时一并写入。
"""

from __future__ import annotations

from dataclasses import dataclass

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
    rows = read_table(d, "measurements", list(CORE) + extra).to_pylist()
    out = []
    for r in rows:
        if all(r.get(k) == v for k, v in where.items()):
            out.append(PowerPoint(r["device"], int(r["device_count"]), r["model"], float(r["concurrency"]), float(r["power_w"]),
                                  float(r["itl_s"]), {k: r[k] for k in extra}))
    return out


def profile_from_points(points: list[PowerPoint], slots: int, idle_w: float, host_w: float = 0.0,
                        prefill_tokens_per_s: float | None = None, boot_s: float | None = None, source: str = "") -> dict:
    """由同一组测量生成服务器类型（场景 node_profiles 中的一项）。只使用 0 < concurrency ≤ slots 的测量。"""
    if not points:
        raise DatasetError("没有符合选择条件的测量")
    keys = {(p.device, p.device_count, p.model) for p in points}
    if len(keys) != 1:
        raise DatasetError(f"须选择同一组（device、device_count、model）的测量，当前为 {sorted(keys)}")
    pts = sorted((p for p in points if 0 < p.concurrency <= slots), key=lambda p: p.concurrency)
    if not pts:
        raise DatasetError(f"没有 0 < concurrency ≤ {slots} 的测量")
    us = [round(p.concurrency / slots, 4) for p in pts]
    if len(set(us)) != len(us):
        raise DatasetError(f"多次测量的占用率相同（{us}）：请用 --where 进一步选择（如 task）")
    device, n, model = keys.pop()
    prof: dict = {
        "description": f"{n}×{device}，{model}；在服务时的功率与 token 间隔取自 {source or '功率特性数据集'}，"
                       f"在线空载功率 {idle_w:g} W 与主机功率 {host_w:g} W 为给定值",
        "slots": slots,
        "power_w": [[0.0, round(idle_w, 1)]] + [[u, round(p.power_w + host_w, 1)] for u, p in zip(us, pts)],
        "tpot_s": [[u, round(p.itl_s, 5)] for u, p in zip(us, pts)],
    }
    if prefill_tokens_per_s is not None:
        prof["prefill_tokens_per_s"] = prefill_tokens_per_s
    if boot_s is not None:
        prof["boot_s"] = boot_s
    return prof
