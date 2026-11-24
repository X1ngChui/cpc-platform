"""真实服务器的功率测量（传感器层 → 服务器）。仿真服务器的功率由服务器模型给出，不经过本模块。

SensorPower 从传感器层（cpc.sensors）读取实测功率并归属到真实服务器：
    * 加速卡（gpu/npu/soc）读数按 upstream 声明的设备归属到各服务器；
    * 主机部分（CPU、内存、主板等）的确定方式由站点配置 measurement.host 显式给出：
        server   = 整机读数（server/platform）− 本机全部加速卡读数；
        cpu_dram = CPU 与内存读数之和；
        excluded = 不计主机部分（服务器 IT 功率只含加速卡，快照中 power_scope = accelerators）；
    * 主机部分在未关机的真实服务器之间平均分摊（全部关机时在全部真实服务器间分摊）；
    * 任一组成部分的读数缺失时，相应服务器的功率为 None，缺失项记入 power_missing；不以 0、模型值或其他传感器替代。

启动检查（start）：每个传感器读数成功；服务器声明的加速卡均有读数；主机功率方式所需的读数存在。任一不满足即抛出 SensorError。
"""

from __future__ import annotations

import logging
from prometheus_client import CollectorRegistry

from ..config import MeasurementConfig
from ..datacenter import Server
from ..health import HealthBoard
from ..sensors.base import EnergyIntegrator, Reading, Sensor, SensorError, make_sensor
from ..sensors.exporter import read_all

log = logging.getLogger("cpc.site.power")

ACCEL = ("gpu", "npu", "soc")
HOST_KINDS = {"server": ("server", "platform"), "cpu_dram": ("cpu", "dram"), "excluded": ()}


def node_devices(n: Server) -> list[str]:
    return n.upstream.device_names if n.upstream is not None else []


class SensorPower:
    name = "sensors"
    gpu_energy_from_source = True

    def __init__(self, site_id: str, cfg: MeasurementConfig, registry: CollectorRegistry | None = None,
                 sensors: list[Sensor] | None = None):
        """sensors 仅供测试注入；正常情况下按 cfg.sensors 构造（构造失败即启动失败）。"""
        self.site = site_id
        self.host = cfg.host
        self.scope = "accelerators" if cfg.host == "excluded" else "node"
        self.sensors = sensors if sensors is not None else [make_sensor(s) for s in cfg.sensors]
        self.board = HealthBoard("sensor", registry, {"site": site_id})
        for s in self.sensors:
            self.board.register(s.id)
        self.integ = EnergyIntegrator()
        self.readings: list[Reading] = []
        self.expected: set[tuple[str, str]] = set()
        self.accel_keys: dict[str, tuple[str, str]] = {}  # 设备名 → 读数键
        self.all_accel: set[tuple[str, str]] = set()
        self.host_keys: set[tuple[str, str]] = set()
        self.models: dict[str, str] = {}

    def start(self, nodes: list[Server]) -> None:
        rs = read_all(self.sensors, self.board)
        down = {k: h.last_error for k, h in self.board.items.items() if not h.up}
        if down:
            raise SensorError(f"站点 {self.site} 的功率传感器启动检查失败：{down}")
        accel: dict[str, Reading] = {}
        for r in rs:
            if r.kind in ACCEL:
                if r.device in accel:
                    raise SensorError(f"站点 {self.site}：加速卡设备名 {r.device} 同时出现在传感器 {accel[r.device].sensor} 与 {r.sensor} 中")
                accel[r.device] = r
        need = {d for n in nodes for d in node_devices(n)}
        missing = sorted(need - set(accel))
        if missing:
            raise SensorError(f"站点 {self.site}：服务器声明的加速卡 {missing} 没有读数（传感器提供的加速卡：{sorted(accel)}）")
        self.accel_keys = {d: accel[d].key for d in need}
        self.all_accel = {r.key for r in accel.values()}
        self.models = {d: accel[d].model for d in need}
        self.host_keys = {r.key for r in rs if r.kind in HOST_KINDS[self.host]}
        if self.host != "excluded" and not self.host_keys:
            raise SensorError(f"站点 {self.site}：measurement.host = {self.host} 需要 {HOST_KINDS[self.host]} 类读数，但配置的传感器未提供")
        self.expected = {r.key for r in rs}
        self.readings = self.integ.update(rs, expected=self.expected)
        log.info("站点 %s 功率传感器：%s；主机部分：%s", self.site, [s.id for s in self.sensors], self.host,
                 extra={"event": "power_source_ready", "site": self.site})

    def _sum(self, by_key: dict[tuple[str, str], Reading], keys: set[tuple[str, str]]) -> float | None:
        vals = [by_key[k].power_w if k in by_key else None for k in keys]
        return None if any(v is None for v in vals) else float(sum(vals))

    def host_total(self, by_key: dict[tuple[str, str], Reading]) -> float | None:
        if self.host == "excluded":
            return 0.0
        total = self._sum(by_key, self.host_keys)
        if total is None or self.host == "cpu_dram":
            return total
        accel = self._sum(by_key, self.all_accel)
        return None if accel is None else total - accel

    def sample(self, nodes: list[Server]) -> None:
        rs = self.integ.update(read_all(self.sensors, self.board), expected=self.expected)
        self.readings = rs
        by_key = {r.key: r for r in rs}
        for n in nodes:
            devs = node_devices(n)
            rd = [by_key.get(self.accel_keys[d]) for d in devs]
            powers = [r.power_w if r else None for r in rd]
            n.detail = {"devices": devs, "power_w": powers, "util_pct": [r.util_pct if r else None for r in rd],
                        "model": [self.models[d] for d in devs],
                        "energy_mj": [r.energy_j * 1000.0 if r and r.energy_j is not None else None for r in rd]}
            n.power_missing = [f"device:{d}" for d, p in zip(devs, powers) if p is None]
            n.gpu_power_w = None if n.power_missing else float(sum(powers))
        host = self.host_total(by_key)
        live = [n for n in nodes if n.state != "off"] or nodes
        for n in nodes:
            if host is None:
                n.power_missing.append("host")
                share = None
            else:
                share = host / len(live) if n in live else 0.0
            n.it_power_w = None if n.gpu_power_w is None or share is None else n.gpu_power_w + share

    def sensor_for(self, device: str) -> Sensor:
        sid = self.accel_keys[device][0]
        return next(s for s in self.sensors if s.id == sid)

    def set_power_limit(self, device: str, ratio: float) -> None:
        self.sensor_for(device).set_power_limit(device, ratio)

    def status(self) -> dict:
        cov = {}
        for k in sorted(self.expected):
            c = self.integ.coverage(k)
            cov[f"{k[0]}/{k[1]}"] = None if c is None else round(c, 4)
        return {
            "power": {"host": self.host, "scope": self.scope},
            "sensors": {k: {**v, "capabilities": next(s.capabilities() for s in self.sensors if s.id == k)}
                        for k, v in self.board.snapshot().items()},
            "sample_coverage": cov,
        }
