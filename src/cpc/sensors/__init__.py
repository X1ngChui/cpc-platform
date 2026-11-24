"""能耗感知设备层：多硬件/多平台功率传感器、自动探测与通用 Prometheus 导出器。"""

from .base import SENSORS, EnergyIntegrator, Reading, Sensor, autodetect, make_sensor, register_sensor
from . import builtin  # noqa: F401  注册内置传感器

__all__ = ["SENSORS", "EnergyIntegrator", "Reading", "Sensor", "autodetect", "make_sensor", "register_sensor"]
