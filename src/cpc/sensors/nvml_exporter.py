"""轻量 NVML 功率导出器（Prometheus）。

用途：在没有部署 NVIDIA dcgm-exporter 的机器上（如 Windows 开发机、消费级显卡、无法安装 DCGM 的云主机）
采集 GPU 功率与累计能耗。指标名与 dcgm-exporter 保持一致，因此 Prometheus 规则与 Grafana 面板可通用：

  DCGM_FI_DEV_POWER_USAGE               W
  DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION  mJ（硬件累计计数，Volta 及以后架构支持）
  DCGM_FI_DEV_GPU_UTIL                  %
  DCGM_FI_DEV_SM_CLOCK                  MHz
  DCGM_FI_DEV_FB_USED                   MiB
  DCGM_FI_DEV_GPU_TEMP                  ℃
  cpc_gpu_power_limit_watts             W（当前功率上限）

采样发生在 Prometheus 抓取时（collect-on-scrape），无后台线程。
"""

from __future__ import annotations

import logging
import socket
import time

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from prometheus_client.registry import Collector

log = logging.getLogger("cpc.nvml")


class NvmlCollector(Collector):
    def __init__(self, site: str | None = None, hostname: str | None = None):
        import pynvml

        self.nv = pynvml
        self.nv.nvmlInit()
        self.site = site
        self.hostname = hostname or socket.gethostname()
        self.n = self.nv.nvmlDeviceGetCount()
        self.handles = [self.nv.nvmlDeviceGetHandleByIndex(i) for i in range(self.n)]
        self.meta = []
        for i, h in enumerate(self.handles):
            name = self.nv.nvmlDeviceGetName(h)
            uuid = self.nv.nvmlDeviceGetUUID(h)
            self.meta.append([str(i), uuid if isinstance(uuid, str) else uuid.decode(), name if isinstance(name, str) else name.decode()])
        log.info("NVML: %d GPU(s): %s", self.n, [m[2] for m in self.meta])

    def collect(self):
        labels = ["gpu", "UUID", "modelName", "Hostname"] + (["site"] if self.site else [])
        fam = {
            "p": GaugeMetricFamily("DCGM_FI_DEV_POWER_USAGE", "GPU power draw (W)", labels=labels),
            "e": GaugeMetricFamily("DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION", "GPU energy since driver load (mJ)", labels=labels),
            "u": GaugeMetricFamily("DCGM_FI_DEV_GPU_UTIL", "GPU utilization (%)", labels=labels),
            "c": GaugeMetricFamily("DCGM_FI_DEV_SM_CLOCK", "SM clock (MHz)", labels=labels),
            "m": GaugeMetricFamily("DCGM_FI_DEV_FB_USED", "Framebuffer used (MiB)", labels=labels),
            "t": GaugeMetricFamily("DCGM_FI_DEV_GPU_TEMP", "GPU temperature (C)", labels=labels),
            "l": GaugeMetricFamily("cpc_gpu_power_limit_watts", "GPU power limit (W)", labels=labels),
        }
        nv = self.nv
        for h, meta in zip(self.handles, self.meta):
            lv = meta + [self.hostname] + ([self.site] if self.site else [])
            readers = {
                "p": lambda: nv.nvmlDeviceGetPowerUsage(h) / 1000.0,
                "e": lambda: float(nv.nvmlDeviceGetTotalEnergyConsumption(h)),
                "u": lambda: float(nv.nvmlDeviceGetUtilizationRates(h).gpu),
                "c": lambda: float(nv.nvmlDeviceGetClockInfo(h, nv.NVML_CLOCK_SM)),
                "m": lambda: nv.nvmlDeviceGetMemoryInfo(h).used / 2**20,
                "t": lambda: float(nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)),
                "l": lambda: nv.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0,
            }
            for k, fn in readers.items():
                try:
                    fam[k].add_metric(lv, fn())
                except nv.NVMLError:
                    pass  # 该型号不支持此字段
        yield from fam.values()


def serve(port: int = 9400, addr: str = "0.0.0.0", site: str | None = None) -> None:
    REGISTRY.register(NvmlCollector(site=site))
    start_http_server(port, addr=addr)
    log.info("NVML exporter listening on %s:%d/metrics", addr, port)
    while True:
        time.sleep(3600)
