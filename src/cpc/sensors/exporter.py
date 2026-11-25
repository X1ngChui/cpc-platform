"""通用功率导出器（Prometheus）：把传感器层的读数以统一指标暴露出来，可部署在任意 Linux 主机上。

传感器由命令行显式给出（--types 或 --spec）；启动时每个传感器须读数成功，否则导出器启动失败。

统一指标（所有硬件）：
  cpc_device_power_watts{sensor,device,kind,vendor,model,host[,site]}     W
  cpc_device_energy_joules_total{...}                                     J（硬件计数器或按功率积分）
  cpc_device_utilization_percent{...}                                     %
  cpc_sensor_up{sensor,host[,site]}                                       最近一次读取是否成功且无字段错误
  cpc_sensor_errors_total{sensor,host[,site]}                             读取失败或含字段错误的次数
NVIDIA GPU 另外输出与 dcgm-exporter 同名的指标，便于复用现成的仪表盘与规则：
  DCGM_FI_DEV_POWER_USAGE、DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION（mJ）、DCGM_FI_DEV_GPU_UTIL

读数缺失的字段不输出样本。后台线程按固定周期采样并积分能耗，抓取时返回最近一次结果。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from ..health import HealthBoard
from .base import EnergyIntegrator, Reading, Sensor, SensorError

log = logging.getLogger("cpc.exporter")


def read_all(sensors: list[Sensor], board: HealthBoard) -> list[Reading]:
    """依次读取各传感器；每个传感器的失败只影响其自身读数，并记入健康状态。"""
    out: list[Reading] = []
    for s in sensors:
        try:
            rs = s.check_bounds(s.read())
        except Exception as e:  # noqa: BLE001 —— 隔离单个传感器的故障，原因记入健康状态
            board.fail(s.id, e)
            continue
        errs = {f"{r.device}.{k}": v for r in rs for k, v in r.errors.items()}
        if errs:
            board.fail(s.id, f"字段读取失败：{errs}")
        else:
            board.ok(s.id)
        out += rs
    return out


class SensorCollector(Collector):
    def __init__(self, sensors: list[Sensor], site: str | None = None, interval_s: float = 1.0, hostname: str | None = None):
        self.sensors = sensors
        self.site = site
        self.host = hostname or socket.gethostname()
        self.interval = interval_s
        self.integ = EnergyIntegrator()
        self.board = HealthBoard("sensor", None)
        for s in sensors:
            self.board.register(s.id)
        self.readings: list[Reading] = []
        self.lock = threading.Lock()
        self.sample()
        down = {k: v.last_error for k, v in self.board.items.items() if not v.up}
        if down:
            raise SensorError(f"传感器启动检查失败：{down}")
        self.expected = {r.key for r in self.readings}
        threading.Thread(target=self._loop, daemon=True).start()

    def sample(self) -> None:
        out = read_all(self.sensors, self.board)
        out = self.integ.update(out, expected=getattr(self, "expected", None))
        with self.lock:
            self.readings = out

    def _loop(self) -> None:
        while True:
            time.sleep(self.interval)
            self.sample()

    def collect(self):
        with self.lock:
            rs = list(self.readings)
        extra = ["site"] if self.site else []
        lv_site = [self.site] if self.site else []
        lbl = ["sensor", "device", "kind", "vendor", "model", "host"] + extra
        p = GaugeMetricFamily("cpc_device_power_watts", "设备功率（W）", labels=lbl)
        e = CounterMetricFamily("cpc_device_energy_joules", "设备累计能耗（J）", labels=lbl)
        u = GaugeMetricFamily("cpc_device_utilization_percent", "设备利用率（%）", labels=lbl)
        up = GaugeMetricFamily("cpc_sensor_up", "传感器最近一次读取是否成功且无字段错误", labels=["sensor", "host"] + extra)
        err = CounterMetricFamily("cpc_sensor_errors", "传感器读取失败或含字段错误的次数", labels=["sensor", "host"] + extra)
        dl = ["gpu", "modelName", "Hostname"] + extra
        dp = GaugeMetricFamily("DCGM_FI_DEV_POWER_USAGE", "GPU power draw (W)", labels=dl)
        de = GaugeMetricFamily("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "GPU energy (mJ)", labels=dl)
        du = GaugeMetricFamily("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (%)", labels=dl)
        for r in rs:
            lv = [r.sensor, r.device, r.kind, r.vendor, r.model, self.host] + lv_site
            if r.power_w is not None:
                p.add_metric(lv, r.power_w)
            if r.energy_j is not None:
                e.add_metric(lv, r.energy_j)
            if r.util_pct is not None:
                u.add_metric(lv, r.util_pct)
            if r.vendor == "nvidia" and r.kind == "gpu":
                g = [r.device.removeprefix("gpu"), r.model, self.host] + lv_site
                if r.power_w is not None:
                    dp.add_metric(g, r.power_w)
                if r.energy_j is not None:
                    de.add_metric(g, r.energy_j * 1000.0)
                if r.util_pct is not None:
                    du.add_metric(g, r.util_pct)
        for name, h in self.board.items.items():
            up.add_metric([name, self.host] + lv_site, 1.0 if h.up else 0.0)
            err.add_metric([name, self.host] + lv_site, h.errors_total)
        yield from (p, e, u, up, err, dp, de, du)


def serve(sensors: list[Sensor], port: int = 9400, addr: str = "0.0.0.0", site: str | None = None, interval_s: float = 1.0) -> None:
    REGISTRY.register(SensorCollector(sensors, site, interval_s))
    start_http_server(port, addr=addr)
    log.info("exporter on %s:%d/metrics with sensors %s", addr, port, [s.id for s in sensors])
    while True:
        time.sleep(3600)
