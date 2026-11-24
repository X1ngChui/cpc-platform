"""能耗感知的设备层：统一的功率读数结构、传感器接口、注册表与自动探测。

每种硬件/平台一个传感器类，全部输出同一种 Reading：
    sensor   传感器类型名（nvml、rapl、ipmi……）
    device   设备标识（gpu0、package-0、npu0-0、server……），在同一 sensor 内唯一
    kind     设备类别：gpu / npu / cpu / dram / soc / server / platform / other
    vendor   厂商：nvidia / amd / intel / huawei / apple / generic
    power_w  瞬时（或驱动给出的短时平均）功率，W
    energy_j 累计能耗计数（若硬件提供；否则由上层按功率积分），J
    util_pct 利用率（若可得）

站点代理、通用导出器（cpc exporter）都只依赖这一层，新增硬件只需新增一个传感器类并注册。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

log = logging.getLogger("cpc.sensors")

KINDS = ("gpu", "npu", "cpu", "dram", "soc", "server", "platform", "other")


@dataclass
class Reading:
    sensor: str
    device: str
    kind: str
    vendor: str = "generic"
    model: str = ""
    power_w: float | None = None
    energy_j: float | None = None
    util_pct: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Sensor:
    """传感器基类。子类实现 read()；可选实现 detect()（类方法，判断本机是否可用）与 set_power_limit()。"""

    name = "base"
    platforms = "any"  # 文档用途：支持的平台
    verified = False  # 是否已在真实硬件上验证（文档用途）

    def __init__(self, **params: Any):
        self.params = params

    @classmethod
    def detect(cls) -> bool:
        return False

    def read(self) -> list[Reading]:
        raise NotImplementedError

    def close(self) -> None:
        return None

    # 可选能力：功率封顶。ratio 为相对最大上限的比例
    def set_power_limit(self, device: str, ratio: float) -> None:
        raise NotImplementedError(f"{self.name} 不支持功率封顶")


SENSORS: dict[str, type[Sensor]] = {}


def register_sensor(name: str) -> Callable[[type[Sensor]], type[Sensor]]:
    def deco(cls: type[Sensor]) -> type[Sensor]:
        cls.name = name
        SENSORS[name] = cls
        return cls

    return deco


def make_sensor(spec: dict | str) -> Sensor:
    """spec 形如 {"type": "nvml"} 或 {"type": "ipmi", "host": "10.0.0.5", ...}，也可以直接给类型名。"""
    if isinstance(spec, str):
        spec = {"type": spec}
    spec = dict(spec)
    typ = spec.pop("type")
    if typ not in SENSORS:
        raise KeyError(f"未知传感器类型 {typ}，可选：{sorted(SENSORS)}")
    return SENSORS[typ](**spec)


# 自动探测顺序：同一类设备优先用更精确/更直接的接口；后者仅在前者不可用时启用
AUTO_ORDER = [
    ("nvml", None), ("nvidia-smi", "nvml"),
    ("amdsmi", None), ("hwmon", None),  # hwmon 覆盖 amdgpu / Intel i915/xe；与 amdsmi 同时可用时会重复，见下
    ("ascend-dcmi", None),
    ("rapl", None),
    ("ipmi", None),
    ("powermetrics", None),
    ("lhm", None),
]


def autodetect() -> list[Sensor]:
    """探测本机可用的传感器（不需要任何参数的那些）。"""
    from . import builtin  # noqa: F401  确保内置传感器已注册

    found: list[Sensor] = []
    names: set[str] = set()
    for name, skip_if in AUTO_ORDER:
        if skip_if and skip_if in names:
            continue
        if name == "hwmon" and "amdsmi" in names:
            # amdsmi 已覆盖 AMD GPU，hwmon 只保留 Intel GPU
            cls = SENSORS[name]
            try:
                if cls.detect():
                    found.append(cls(drivers=["i915", "xe"]))
                    names.add(name)
            except Exception as e:  # noqa: BLE001
                log.debug("detect %s failed: %s", name, e)
            continue
        cls = SENSORS.get(name)
        if cls is None:
            continue
        try:
            if cls.detect():
                found.append(cls())
                names.add(name)
        except Exception as e:  # noqa: BLE001
            log.debug("detect %s failed: %s", name, e)
    return found


class EnergyIntegrator:
    """为没有硬件能耗计数器的读数积分出累计能耗；对有计数器的读数直接透传。

    计数器回绕（如 RAPL 的 energy_uj 达到 max_energy_range_uj 后归零）由各传感器自行处理，
    这里只保证输出的 energy_j 单调不减。
    """

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], tuple[float, float, float]] = {}  # key -> (t, power, energy)
        self._offset: dict[tuple[str, str], float] = {}

    def update(self, readings: list[Reading], now: float | None = None) -> list[Reading]:
        now = time.monotonic() if now is None else now
        for r in readings:
            key = (r.sensor, r.device)
            last = self._last.get(key)
            if r.energy_j is not None:
                # 硬件计数：若出现下降（驱动重载等），把之前的累计量作为偏移继续累加
                if last is not None and r.energy_j + self._offset.get(key, 0.0) < last[2] - 1e-6:
                    self._offset[key] = last[2]
                e = r.energy_j + self._offset.get(key, 0.0)
            else:
                e = 0.0 if last is None else last[2] + (last[1] or 0.0) * (now - last[0])
            r.energy_j = e
            self._last[key] = (now, r.power_w or 0.0, e)
        return readings
