"""Measured power of real servers (sensor layer -> servers). Emulated servers take their power from the
server model and never pass through this module.

SensorPower reads the sensor layer (cpc.sensors) and attributes the power to real servers:
    * accelerator readings (gpu/npu/soc) go to the server whose upstream declares the device; servers
      without accelerators (CPU-only inference instances) have the host part only;
    * the host part (CPU, memory, mainboard, ...) is defined explicitly by measurement.host:
        server   = whole-server reading (server/platform) - all local accelerator readings
        cpu_dram = sum of CPU and memory readings
        excluded = not counted (server IT power covers accelerators only; power_scope = accelerators)
    * the host part is shared equally among the real servers that are not off (among all of them when
      all are off);
    * when any component is missing, the power of the affected servers is None and the component is
      listed in power_missing; it is never replaced by 0, a model value or another sensor.

Startup checks (start): every sensor reads successfully; every declared accelerator has a reading;
the readings needed by the host mode exist. Any failure raises SensorError.
"""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry

from ..config import MeasurementConfig
from ..datacenter import Server
from ..health import HealthBoard
from ..sensors.base import EnergyIntegrator, Key, Reading, Sensor, SensorError, make_sensor, read_all

log = logging.getLogger("cpc.site.power")

ACCEL = ("gpu", "npu", "soc")
HOST_KINDS = {"server": ("server", "platform"), "cpu_dram": ("cpu", "dram"), "excluded": ()}


def node_devices(n: Server) -> list[str]:
    return n.upstream.device_names if n.upstream is not None else []


def _sum(by_key: dict[Key, Reading], keys: set[Key]) -> float | None:
    vals = [r.power_w if (r := by_key.get(k)) else None for k in keys]
    return None if None in vals else float(sum(vals))


class SensorPower:
    name = "sensors"

    def __init__(self, site_id: str, cfg: MeasurementConfig, registry: CollectorRegistry | None = None,
                 sensors: list[Sensor] | None = None):
        """sensors is for tests only; normally the sensors are built from cfg.sensors (a failure stops the startup)."""
        self.site = site_id
        self.host = cfg.host
        self.scope = "accelerators" if cfg.host == "excluded" else "node"
        self.sensors = sensors if sensors is not None else [make_sensor(s) for s in cfg.sensors]
        self.board = HealthBoard("sensor", registry, {"site": site_id})
        for s in self.sensors:
            self.board.register(s.id)
        self.integ = EnergyIntegrator()
        self.readings: list[Reading] = []
        self.expected: set[Key] = set()
        self.accel_keys: dict[str, Key] = {}  # device name -> reading key
        self.all_accel: set[Key] = set()
        self.host_keys: set[Key] = set()
        self.models: dict[str, str] = {}

    def start(self, nodes: list[Server]) -> None:
        rs = read_all(self.sensors, self.board)
        if down := {k: h.last_error for k, h in self.board.items.items() if not h.up}:
            raise SensorError(f"site {self.site}: power sensors failed the startup check: {down}")
        accel: dict[str, Reading] = {}
        for r in rs:
            if r.kind in ACCEL:
                if r.device in accel:
                    raise SensorError(f"site {self.site}: accelerator {r.device} is reported by both "
                                      f"{accel[r.device].sensor} and {r.sensor}")
                accel[r.device] = r
        need = {d for n in nodes for d in node_devices(n)}
        if missing := sorted(need - accel.keys()):
            raise SensorError(f"site {self.site}: declared accelerators {missing} have no reading "
                              f"(the sensors report {sorted(accel)})")
        self.accel_keys = {d: accel[d].key for d in need}
        self.models = {d: accel[d].model for d in need}
        self.all_accel = {r.key for r in accel.values()}
        self.host_keys = {r.key for r in rs if r.kind in HOST_KINDS[self.host]}
        if self.host != "excluded" and not self.host_keys:
            raise SensorError(f"site {self.site}: measurement.host = {self.host} needs {HOST_KINDS[self.host]} readings, "
                              "which the configured sensors do not provide")
        self.expected = {r.key for r in rs}
        self.readings = self.integ.update(rs, expected=self.expected)
        log.info("site %s power sensors: %s; host part: %s", self.site, [s.id for s in self.sensors], self.host,
                 extra={"event": "power_source_ready", "site": self.site})

    def host_total(self, by_key: dict[Key, Reading]) -> float | None:
        match self.host:
            case "excluded":
                return 0.0
            case "cpu_dram":
                return _sum(by_key, self.host_keys)
        if (total := _sum(by_key, self.host_keys)) is None or (accel := _sum(by_key, self.all_accel)) is None:
            return None
        return total - accel

    def sample(self, nodes: list[Server]) -> None:
        self.readings = rs = self.integ.update(read_all(self.sensors, self.board), expected=self.expected)
        by_key = {r.key: r for r in rs}
        for n in nodes:
            devs = node_devices(n)
            rd = [by_key.get(self.accel_keys[d]) for d in devs]
            powers = [r.power_w if r else None for r in rd]
            n.detail = {"devices": devs, "power_w": powers, "util_pct": [r.util_pct if r else None for r in rd],
                        "model": [self.models[d] for d in devs],
                        "energy_mj": [r.energy_j * 1000.0 if r and r.energy_j is not None else None for r in rd]}
            n.power_missing = [f"device:{d}" for d, p in zip(devs, powers) if p is None]
            n.gpu_power_w = None if n.power_missing or not devs else float(sum(powers))
        host = self.host_total(by_key)
        live = [n for n in nodes if n.state != "off"] or nodes
        for n in nodes:
            if host is None:
                n.power_missing.append("host")
            share = None if host is None else host / len(live) if n in live else 0.0
            accel = n.gpu_power_w if node_devices(n) else 0.0
            n.it_power_w = None if accel is None or share is None else accel + share

    def sensor_for(self, device: str) -> Sensor:
        sid = self.accel_keys[device][0]
        return next(s for s in self.sensors if s.id == sid)

    def set_power_limit(self, device: str, ratio: float) -> None:
        self.sensor_for(device).set_power_limit(device, ratio)

    def status(self) -> dict:
        caps = {s.id: s.capabilities() for s in self.sensors}
        return {
            "power": {"host": self.host, "scope": self.scope},
            "sensors": {k: {**v, "capabilities": caps[k]} for k, v in self.board.snapshot().items()},
            "sample_coverage": {f"{k[0]}/{k[1]}": None if (c := self.integ.coverage(k)) is None else round(c, 4)
                                for k in sorted(self.expected)},
        }
