"""通用功率导出器（Prometheus）：把传感器层的读数以统一指标暴露出来，可部署在任何服务器/笔记本上。

统一指标（所有硬件）：
  cpc_device_power_watts{sensor,device,kind,vendor,model,host[,site]}     W
  cpc_device_energy_joules_total{...}                                     J（硬件计数器或按功率积分）
  cpc_device_utilization_percent{...}                                     %
  cpc_host_power_watts{host[,site]}                                       W（整机读数优先，否则为各部件之和）
NVIDIA GPU 另外输出与 dcgm-exporter 同名的指标，便于复用现成的仪表盘与规则：
  DCGM_FI_DEV_POWER_USAGE、DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION（mJ）、DCGM_FI_DEV_GPU_UTIL

后台线程按固定周期采样（默认 1 s）并积分能耗，抓取时返回最近一次结果。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

from .base import EnergyIntegrator, Reading, Sensor

log = logging.getLogger("cpc.exporter")


def host_total(readings: list[Reading]) -> float:
    server = [r.power_w for r in readings if r.kind in ("server", "platform") and r.power_w is not None]
    if server:
        return float(sum(server))
    return float(sum(r.power_w or 0.0 for r in readings if r.kind in ("gpu", "npu", "soc", "cpu", "dram")))


class SensorCollector(Collector):
    def __init__(self, sensors: list[Sensor], site: str | None = None, interval_s: float = 1.0, hostname: str | None = None):
        self.sensors = sensors
        self.site = site
        self.host = hostname or socket.gethostname()
        self.interval = interval_s
        self.integ = EnergyIntegrator()
        self.readings: list[Reading] = []
        self.lock = threading.Lock()
        self.sample()
        threading.Thread(target=self._loop, daemon=True).start()

    def sample(self) -> None:
        out: list[Reading] = []
        for s in self.sensors:
            try:
                out += s.read()
            except Exception as e:  # noqa: BLE001
                log.warning("sensor %s failed: %s", s.name, e)
        out = self.integ.update(out)
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
        h = GaugeMetricFamily("cpc_host_power_watts", "主机总功率（W）", labels=["host"] + extra)
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
        h.add_metric([self.host] + lv_site, host_total(rs))
        yield from (p, e, u, h, dp, de, du)


def serve(sensors: list[Sensor], port: int = 9400, addr: str = "0.0.0.0", site: str | None = None, interval_s: float = 1.0) -> None:
    REGISTRY.register(SensorCollector(sensors, site, interval_s))
    start_http_server(port, addr=addr)
    log.info("exporter on %s:%d/metrics with sensors %s", addr, port, [s.name for s in sensors])
    while True:
        time.sleep(3600)
