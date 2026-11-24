"""能耗感知的设备层：统一的功率读数结构、传感器接口、注册表与能耗积分。

每种硬件一个传感器类，全部输出同一种 Reading：
    sensor   传感器实例名（默认为类型名 nvml、rapl、ipmi……；同类型多个实例时由配置的 name 区分）
    device   设备标识（gpu0、package-0、npu0-0、server……），在同一传感器内唯一
    kind     设备类别：gpu / npu / cpu / dram / soc / server / platform / other
    vendor   厂商：nvidia / amd / intel / huawei / generic
    power_w  瞬时（或驱动给出的短时平均）功率，W；读取失败或尚无数据时为 None
    energy_j 累计能耗计数（若硬件提供；否则由 EnergyIntegrator 按功率积分），J
    util_pct 利用率（若可得）
    errors   本次读取中失败的字段及原因，例如 {"power_w": "NVMLError_GpuIsLost"}

错误处理约定：
- 构造阶段确定设备集合以及各字段所用的接口（能力探测）；必需能力不可用时抛出 SensorError，使启动失败；
- read() 整体失败（命令执行失败、接口不可达、输出无法解析）时抛出异常；
- 单个设备或单个字段读取失败时，对应值为 None，原因记入 Reading.errors；不以 0 或其他数据源替代。

站点代理、通用导出器（cpc exporter）都只依赖这一层，新增硬件只需新增一个传感器类并注册。
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..health import describe
from ..registry import Registry

log = logging.getLogger("cpc.sensors")

KINDS = ("gpu", "npu", "cpu", "dram", "soc", "server", "platform", "other")


class SensorError(Exception):
    """传感器不可用（初始化或能力探测失败），或输出无法解析。"""


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
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.sensor, self.device)

    def to_dict(self) -> dict:
        return asdict(self)


class Sensor:
    """传感器基类。子类实现 read()；可选实现 detect()、capabilities() 与 set_power_limit()。"""

    name = "base"  # 类型名（注册名）
    requires = ""  # 文档用途：依赖的驱动、库或接口
    verified = False  # 是否已在真实硬件上验证（文档用途）
    supports_power_limit = False

    def __init__(self, **params: Any):
        self.params = params
        self.id = self.name  # 实例名；make_sensor 按配置的 name 覆盖

    @classmethod
    def detect(cls) -> bool:
        """本机是否具备该传感器的前提条件。仅用于 cpc sensors --list 的信息展示，平台不据此自动选择传感器。"""
        return False

    def read(self) -> list[Reading]:
        raise NotImplementedError

    def capabilities(self) -> dict[str, Any]:
        """启动时探测到的能力（各设备可读的字段、所用接口等），写入状态快照。"""
        return {}

    def close(self) -> None:
        return None

    # 可选能力：功率封顶。ratio 为相对最大上限的比例
    def set_power_limit(self, device: str, ratio: float) -> None:
        raise NotImplementedError(f"{self.name} 不支持功率封顶")

    @staticmethod
    def field(r: Reading, attr: str, fn: Callable[[], Any], errors: type[BaseException] | tuple = Exception) -> None:
        """读取一个字段：成功则写入 r.<attr>，失败则记入 r.errors[attr]（值保持 None）。"""
        try:
            setattr(r, attr, fn())
        except errors as e:  # noqa: BLE001 —— 调用方给出具体的异常类型
            r.errors[attr] = describe(e)


SENSORS: Registry[Sensor] = Registry("sensors")
register_sensor = SENSORS.register


def make_sensor(spec: dict | str) -> Sensor:
    """spec 形如 {"type": "nvml"} 或 {"type": "ipmi", "name": "bmc", "host": "10.0.0.5", ...}，也可以直接给类型名。

    构造失败（依赖库缺失、驱动不可用、能力探测失败）统一抛出 SensorError。
    """
    if isinstance(spec, str):
        spec = {"type": spec}
    spec = dict(spec)
    typ = spec.pop("type")
    inst = spec.pop("name", None) or typ
    cls = SENSORS.get(typ)
    try:
        s = cls(**spec)
    except Exception as e:  # noqa: BLE001 —— 构造阶段的任何失败都使启动失败，并带上传感器名
        msg = str(e) if isinstance(e, SensorError) else describe(e)
        raise SensorError(f"传感器 {inst}（{typ}）初始化失败：{msg}") from e
    s.id = inst
    return s


class EnergyIntegrator:
    """累计能耗与功率采样完整率。

    - 有硬件计数器的读数直接使用计数器；计数器下降（驱动重载等）时，把此前的累计量作为偏移继续累加，并计为一次复位；
    - 无计数器的读数按功率分段恒定（左端点）积分；
    - 区间 [t_k, t_{k+1}) 的功率取 t_k 时刻的采样；该采样缺失（功率为 None 或应有的读数缺席）时，
      该区间不积分，计入缺失时长 missing_s；否则计入 measured_s。

    expected 为应当出现的读数键 (sensor, device) 集合；某次采样中缺席的键按功率缺失处理。
    计数器回绕（如 RAPL 的 energy_uj 达到 max_energy_range_uj 后归零）由各传感器自行处理。
    """

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], tuple[float, float | None, float]] = {}  # key -> (t, power, energy)
        self._offset: dict[tuple[str, str], float] = {}
        self._counter: set[tuple[str, str]] = set()
        self.resets: dict[tuple[str, str], int] = {}
        self.measured_s: dict[tuple[str, str], float] = {}
        self.missing_s: dict[tuple[str, str], float] = {}

    def update(self, readings: list[Reading], now: float | None = None,
               expected: set[tuple[str, str]] | None = None) -> list[Reading]:
        now = time.monotonic() if now is None else now
        present = {r.key: r for r in readings}
        for key in set(present) | set(expected or ()):
            r = present.get(key)
            last = self._last.get(key)
            dt = 0.0
            if last is not None:
                dt = max(0.0, now - last[0])
                bucket = self.measured_s if last[1] is not None else self.missing_s
                bucket[key] = bucket.get(key, 0.0) + dt
            if r is None:
                if last is not None:
                    e = last[2] if key in self._counter else last[2] + (last[1] * dt if last[1] is not None else 0.0)
                    self._last[key] = (now, None, e)
                continue
            if r.energy_j is not None:
                self._counter.add(key)
                off = self._offset.get(key, 0.0)
                if last is not None and r.energy_j + off < last[2] - 1e-6:
                    off = self._offset[key] = last[2]
                    self.resets[key] = self.resets.get(key, 0) + 1
                    log.warning("设备 %s/%s 的能耗计数器下降，按复位处理", *key,
                                extra={"event": "energy_counter_reset", "sensor": key[0], "device": key[1]})
                e = r.energy_j + off
            else:
                e = 0.0 if last is None else last[2] + (last[1] * dt if last[1] is not None else 0.0)
            r.energy_j = e
            self._last[key] = (now, r.power_w, e)
        return readings

    def coverage(self, key: tuple[str, str]) -> float | None:
        m, x = self.measured_s.get(key, 0.0), self.missing_s.get(key, 0.0)
        return None if m + x == 0 else m / (m + x)
