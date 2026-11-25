"""Device layer: common power reading, sensor interface, registry and energy integration.

One sensor class per kind of hardware; all of them return the same Reading:
    sensor   sensor instance (the type name nvml, rapl, ipmi, ... by default; name tells instances of
             one type apart)
    device   device identifier (gpu0, package-0, server, ...), unique within the sensor
    kind     device class: gpu / npu / cpu / dram / soc / server / platform / other
    vendor   nvidia / amd / intel / huawei / generic
    power_w  instantaneous (or driver-averaged) power in W; None when unavailable
    energy_j accumulated energy counter if the hardware has one (otherwise integrated from power), J
    util_pct utilisation if available
    errors   fields that failed in this reading and why, e.g. {"power_w": "NVMLError_GpuIsLost"}

Error handling:
- the constructor determines the devices and the interface of each field (capability probing);
  a missing required capability raises SensorError and stops the startup;
- read() raises when the whole reading fails (command failed, interface unreachable, unparsable output);
- a failing device or field is None with the reason in Reading.errors, never 0 or another source.

The site agent and cpc sensors depend only on this layer: new hardware needs one new sensor class,
registered as an entry point in the group cpc.sensors.
"""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..health import HealthBoard, describe
from ..registry import Registry, accepted_params, did_you_mean

log = logging.getLogger("cpc.sensors")

KINDS = ("gpu", "npu", "cpu", "dram", "soc", "server", "platform", "other")
Key = tuple[str, str]  # (sensor, device)


class SensorError(Exception):
    """Sensor unavailable (initialisation or capability probing failed) or output unparsable."""


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
    def key(self) -> Key:
        return self.sensor, self.device

    def to_dict(self) -> dict:
        return asdict(self)


class Sensor:
    """Sensor base class. Subclasses implement read(); detect(), capabilities() and set_power_limit() are optional.

    Generic parameter max_w: plausible upper bound of power readings (W). Larger readings count as
    missing (power_w None, reason in errors), which removes sporadic glitches of BMCs and the like.
    """

    name = "base"  # type name (registered name)
    requires = ""  # documentation: driver, library or interface needed
    supports_power_limit = False

    def __init__(self, max_w: float | None = None, **params: Any):
        if max_w is not None and not max_w > 0:
            raise SensorError(f"max_w must be greater than 0, got {max_w}")
        if params:
            bad, ok = sorted(params), accepted_params(type(self))
            hint = did_you_mean(bad[0], ok) if len(bad) == 1 else ""
            raise SensorError(f"does not accept {', '.join(bad)}{hint}; accepted: {', '.join(ok)}")
        self.max_w = max_w
        self.id = self.name  # instance name; make_sensor sets the configured name

    def check_bounds(self, readings: list[Reading]) -> list[Reading]:
        """Power readings above max_w count as missing."""
        if self.max_w is not None:
            for r in readings:
                if r.power_w is not None and r.power_w > self.max_w:
                    r.errors["power_w"] = f"reading {r.power_w:g} W exceeds max_w = {self.max_w:g} W"
                    r.power_w = None
        return readings

    @classmethod
    def detect(cls) -> bool:
        """Whether this host meets the prerequisites (shown by cpc sensors --list; never used to choose sensors)."""
        return False

    def read(self) -> list[Reading]:
        raise NotImplementedError

    def capabilities(self) -> dict[str, Any]:
        """Capabilities probed at startup (readable fields per device, interfaces), shown in the state snapshot."""
        return {}

    def close(self) -> None:
        return None

    def set_power_limit(self, device: str, ratio: float) -> None:
        """Optional: set the power limit of a device to ratio x its maximum limit."""
        raise NotImplementedError(f"{self.name} does not support power capping")

    @staticmethod
    def field(r: Reading, attr: str, fn: Callable[[], Any], errors: type[BaseException] | tuple = Exception) -> None:
        """Read one field: on success set r.<attr>, on failure record r.errors[attr] (the value stays None)."""
        try:
            setattr(r, attr, fn())
        except errors as e:  # noqa: BLE001 - the caller names the exception types
            r.errors[attr] = describe(e)


SENSORS: Registry[Sensor] = Registry("sensors")
register_sensor = SENSORS.register


def make_sensor(spec: dict | str) -> Sensor:
    """spec is {"type": "nvml"}, {"type": "ipmi", "name": "bmc", ...} or just a type name.

    Any failure of the constructor (library missing, driver unavailable, probing failed) raises SensorError.
    """
    spec = {"type": spec} if isinstance(spec, str) else dict(spec)
    typ = spec.pop("type")
    inst = spec.pop("name", None) or typ
    cls = SENSORS.get(typ)
    try:
        s = cls(**spec)
    except Exception as e:  # noqa: BLE001 - any constructor failure stops the startup, naming the sensor
        raise SensorError(f"sensor {inst} ({typ}) failed to initialise: "
                          f"{e if isinstance(e, SensorError) else describe(e)}") from e
    s.id = inst
    return s


def read_all(sensors: list[Sensor], board: HealthBoard) -> list[Reading]:
    """Read every sensor; a failure affects only the readings of that sensor and is recorded in its health."""
    out: list[Reading] = []
    for s in sensors:
        try:
            rs = s.check_bounds(s.read())
        except Exception as e:  # noqa: BLE001 - isolate the fault of one sensor; the reason goes to its health
            board.fail(s.id, e)
            continue
        if errs := {f"{r.device}.{k}": v for r in rs for k, v in r.errors.items()}:
            board.fail(s.id, f"field reads failed: {errs}")
        else:
            board.ok(s.id)
        out += rs
    return out


class EnergyIntegrator:
    """Accumulated energy and completeness of power sampling.

    - readings with a hardware counter use it; when the counter drops (driver reload, ...) the
      previous total becomes an offset and a reset is counted;
    - readings without a counter integrate power as piecewise constant (left end point);
    - the interval [t_k, t_{k+1}) takes the power sampled at t_k; when that sample is missing (power
      None or the reading absent) the interval is not integrated and counts as missing_s, otherwise
      as measured_s.

    expected is the set of reading keys (sensor, device) that should appear; absent keys count as
    missing power. Counter wrap-around (e.g. RAPL energy_uj reaching max_energy_range_uj) is handled
    by each sensor.
    """

    def __init__(self) -> None:
        self._last: dict[Key, tuple[float, float | None, float]] = {}  # key -> (t, power, energy)
        self._offset: defaultdict[Key, float] = defaultdict(float)
        self._counter: set[Key] = set()
        self.resets: Counter[Key] = Counter()
        self.measured_s: defaultdict[Key, float] = defaultdict(float)
        self.missing_s: defaultdict[Key, float] = defaultdict(float)

    def _integrated(self, key: Key, dt: float) -> float:
        t, p, e = self._last[key]
        return e if key in self._counter or p is None else e + p * dt

    def update(self, readings: list[Reading], now: float | None = None, expected: set[Key] | None = None) -> list[Reading]:
        now = time.monotonic() if now is None else now
        present = {r.key: r for r in readings}
        for key in present.keys() | (expected or set()):
            r, last, dt = present.get(key), self._last.get(key), 0.0
            if last is not None:
                dt = max(0.0, now - last[0])
                (self.measured_s if last[1] is not None else self.missing_s)[key] += dt
            if r is None:
                if last is not None:
                    self._last[key] = (now, None, self._integrated(key, dt))
                continue
            if r.energy_j is not None:
                self._counter.add(key)
                if last is not None and r.energy_j + self._offset[key] < last[2] - 1e-6:
                    self._offset[key] = last[2]
                    self.resets[key] += 1
                    log.warning("energy counter of %s/%s dropped; treated as a reset", *key,
                                extra={"event": "energy_counter_reset", "sensor": key[0], "device": key[1]})
                r.energy_j += self._offset[key]
            else:
                r.energy_j = 0.0 if last is None else self._integrated(key, dt)
            self._last[key] = (now, r.power_w, r.energy_j)
        return readings

    def coverage(self, key: Key) -> float | None:
        m, x = self.measured_s.get(key, 0.0), self.missing_s.get(key, 0.0)
        return None if m + x == 0 else m / (m + x)
