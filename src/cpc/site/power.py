"""站点的功率来源（能耗感知的设备层 → 节点）。

- ModelPower：按节点状态与占用率用功率模型计算（仿真后端）。
- SensorPower：从传感器层（cpc.sensors）读取实测功率并归属到节点（真实后端）：
    * 加速卡（gpu/npu/soc）读数按 upstream.devices 归属到各节点（推理实例）；
    * 若有整机读数（server：IPMI/Redfish/PDU），主机部分 = 整机 − 全部加速卡；
      否则若有 CPU/内存读数（RAPL 等），主机部分 = 其和；都没有时用功率模型估计主机部分；
    * 主机部分在本机在线节点之间平均分摊。
"""

from __future__ import annotations

import logging
import time

from ..config import NodeProfile
from ..models import host_power_w, node_power_w
from ..sensors.base import EnergyIntegrator, Reading, Sensor
from .core import Node

log = logging.getLogger("cpc.site.power")

ACCEL = ("gpu", "npu", "soc")


class ModelPower:
    name = "model"

    def __init__(self, profile: NodeProfile):
        self.p = profile

    def sample(self, nodes: list[Node]) -> None:
        for n in nodes:
            occ = n.busy / n.slots if n.slots else 0.0
            it, gpu = node_power_w(self.p, n.state, occ, n.cap_ratio)
            n.it_power_w, n.gpu_power_w = it, gpu
            per = gpu / n.gpus if n.gpus else 0.0
            n.gpu_powers_w = [per] * n.gpus
            util = 100.0 * occ if n.state in ("on", "draining") else 0.0
            n.gpu_utils = [util] * n.gpus
            n.gpu_models = [self.p.gpu_model] * n.gpus
            n.power_source = "model"


def node_devices(n: Node) -> list[str]:
    if n.upstream is None:
        return []
    if n.upstream.devices:
        return list(n.upstream.devices)
    return [f"gpu{i}" for i in n.upstream.gpu_indices]


class SensorPower:
    name = "sensors"

    def __init__(self, profile: NodeProfile, sensors: list[Sensor]):
        self.p = profile
        self.sensors = sensors
        self.integ = EnergyIntegrator()
        self.readings: list[Reading] = []
        self._warned: dict[str, float] = {}

    def read_all(self) -> list[Reading]:
        out: list[Reading] = []
        for s in self.sensors:
            try:
                out += s.read()
            except Exception as e:  # noqa: BLE001 —— 单个传感器失败不影响服务
                if time.monotonic() - self._warned.get(s.name, -1e9) > 60:
                    log.warning("传感器 %s 读取失败：%s", s.name, e)
                    self._warned[s.name] = time.monotonic()
        return self.integ.update(out)

    def sample(self, nodes: list[Node]) -> None:
        self.readings = rs = self.read_all()
        accel = {r.device: r for r in rs if r.kind in ACCEL}
        server = sum(r.power_w or 0.0 for r in rs if r.kind in ("server", "platform"))
        parts = sum(r.power_w or 0.0 for r in rs if r.kind in ("cpu", "dram"))
        has_server = any(r.kind in ("server", "platform") and r.power_w is not None for r in rs)
        has_parts = any(r.kind in ("cpu", "dram") and r.power_w is not None for r in rs)
        for n in nodes:
            devs = [d for d in node_devices(n) if d in accel]
            n.gpu_powers_w = [accel[d].power_w or 0.0 for d in devs]
            n.gpu_utils = [accel[d].util_pct or 0.0 for d in devs]
            n.gpu_models = [accel[d].model for d in devs]
            n.gpu_power_w = sum(n.gpu_powers_w)
            n.power_source = "sensors"
        live = [n for n in nodes if n.state != "off"] or nodes[:1]
        if has_server:
            host_total = max(0.0, server - sum(r.power_w or 0.0 for r in accel.values()))
        elif has_parts:
            host_total = parts
        else:
            host_total = None
        for n in nodes:
            if host_total is not None:
                share = host_total / len(live) if n in live else 0.0
            else:
                occ = n.busy / n.slots if n.slots else 0.0
                share = 0.0 if n.state == "off" else host_power_w(self.p, occ)
                n.power_source = "sensors+model"
            n.it_power_w = n.gpu_power_w + share

    def set_power_limit(self, device: str, ratio: float) -> None:
        for s in self.sensors:
            if any(r.device == device for r in self.readings if r.sensor == s.name):
                s.set_power_limit(device, ratio)
                return
        raise KeyError(f"没有传感器管理设备 {device}")
