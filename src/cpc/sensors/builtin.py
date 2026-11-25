"""Built-in sensors (Linux), one per kind of measured object without overlap. Other devices use command
or a sensor plugin registered as an entry point.

| type      | measures                          | interface                               | reading kinds  |
|-----------|-----------------------------------|-----------------------------------------|----------------|
| nvml      | NVIDIA GPU                        | NVIDIA NVML (nvidia-ml-py)              | gpu            |
| rapl      | CPU package and memory (Intel/AMD)| kernel powercap sysfs (RAPL counters)   | cpu, dram      |
| ipmi      | whole server (local BMC)          | ipmitool dcmi power reading             | server         |
| command   | any other device                  | subprocess + regular expression         | configured     |

Optional capabilities (e.g. the NVML energy counter and utilisation) are probed once in the
constructor and reported by capabilities(); the interface never changes at run time.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from .base import Reading, Sensor, SensorError, register_sensor

log = logging.getLogger("cpc.sensors")


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def _read(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _float_or_none(path: Path) -> float | None:
    try:
        return float(_read(path))
    except (OSError, ValueError):
        return None


class _CounterPower:
    """Power from the differences of an accumulated energy counter.

    When the counter drops: with a known range (max_range) the increment wraps around; otherwise the
    counter was reset, the power of that interval is missing (None) and the total does not grow. The
    first reading has no predecessor and no power.
    """

    def __init__(self) -> None:
        self.last: dict[str, tuple[float, float]] = {}  # key -> (t, raw)
        self.total: Counter[str] = Counter()  # key -> accumulated energy (unit of raw)
        self.resets: Counter[str] = Counter()

    def update(self, key: str, raw: float, max_range: float | None, now: float) -> tuple[float | None, float, bool]:
        """Returns (power, total, whether a counter reset was detected)."""
        prev, self.last[key] = self.last.get(key), (now, raw)
        if prev is None:
            return None, self.total[key], False
        dt, delta = now - prev[0], raw - prev[1]
        if delta < 0:
            if not max_range:
                self.resets[key] += 1
                return None, self.total[key], True
            delta += max_range
        self.total[key] += delta
        return (delta / dt if dt > 0 else None), self.total[key], False


@register_sensor("nvml")
class NvmlSensor(Sensor):
    """NVIDIA GPUs (NVML): power is required; the energy counter (Volta and later) and utilisation are
    optional capabilities probed in the constructor.

    Supports power capping (set_power_limit; root, or SYS_ADMIN in a container). gpus selects GPU
    indices, default all local GPUs.
    """

    requires = 'NVIDIA driver + nvidia-ml-py (pip install -e ".[nvml]")'
    supports_power_limit = True

    def __init__(self, gpus: list[int] | None = None, **params):
        super().__init__(**params)
        try:
            import pynvml  # nvidia-ml-py
        except ImportError as e:
            raise SensorError('nvml: nvidia-ml-py is not installed (pip install -e ".[nvml]")') from e
        self.nv = pynvml
        self.nv.nvmlInit()
        n = self.nv.nvmlDeviceGetCount()
        self.idx = list(range(n)) if gpus is None else list(gpus)
        if (bad := [i for i in self.idx if not 0 <= i < n]) or not self.idx:
            raise SensorError(f"nvml: invalid GPU indices {bad or self.idx} (this host has {n} GPUs)")
        self.h = {i: self.nv.nvmlDeviceGetHandleByIndex(i) for i in self.idx}
        self.names: dict[int, str] = {}
        self.caps: dict[int, dict[str, bool]] = {}
        for i, h in self.h.items():
            nm = self.nv.nvmlDeviceGetName(h)
            self.names[i] = nm.decode() if isinstance(nm, bytes) else nm
            try:
                self.nv.nvmlDeviceGetPowerUsage(h)
            except self.nv.NVMLError as e:
                raise SensorError(f"nvml: gpu{i} has no power reading ({e})") from e
            self.caps[i] = {"energy": self._supported(self.nv.nvmlDeviceGetTotalEnergyConsumption, h),
                            "util": self._supported(self.nv.nvmlDeviceGetUtilizationRates, h)}

    def _supported(self, fn, h) -> bool:
        try:
            fn(h)
            return True
        except self.nv.NVMLError_NotSupported:
            return False

    @classmethod
    def detect(cls) -> bool:
        try:
            import pynvml

            pynvml.nvmlInit()
            return pynvml.nvmlDeviceGetCount() > 0
        except Exception:  # noqa: BLE001 - informational only
            return False

    def capabilities(self) -> dict:
        return {f"gpu{i}": {"power": True, **c} for i, c in self.caps.items()}

    def read(self) -> list[Reading]:
        nv, err, out = self.nv, self.nv.NVMLError, []
        for i, h in self.h.items():
            r = Reading(self.id, f"gpu{i}", "gpu", "nvidia", self.names[i])
            self.field(r, "power_w", lambda: nv.nvmlDeviceGetPowerUsage(h) / 1000.0, err)  # mW -> W
            if self.caps[i]["energy"]:
                self.field(r, "energy_j", lambda: nv.nvmlDeviceGetTotalEnergyConsumption(h) / 1000.0, err)  # mJ -> J
            if self.caps[i]["util"]:
                self.field(r, "util_pct", lambda: float(nv.nvmlDeviceGetUtilizationRates(h).gpu), err)
            out.append(r)
        return out

    def set_power_limit(self, device: str, ratio: float) -> None:
        """Set the power limit to ratio x the maximum limit, clamped to the range of the device."""
        h = self.h[int(device.removeprefix("gpu"))]
        lo, hi = self.nv.nvmlDeviceGetPowerManagementLimitConstraints(h)  # mW
        if ratio * hi < lo:
            log.warning("%s: requested power limit %.0f W is below the device minimum %.0f W, which is used instead "
                        "(set cap_min_ratio of the server type to %.2f)", device, ratio * hi / 1000, lo / 1000, lo / hi)
        self.nv.nvmlDeviceSetPowerManagementLimit(h, int(max(lo, min(hi, ratio * hi))))


@register_sensor("rapl")
class RaplSensor(Sensor):
    """Energy counters of CPU packages, memory (dram) and platform (psys) via Linux powercap RAPL (Intel, AMD Zen).

    energy_uj is readable by root only on many distributions. Docker masks
    /sys/devices/virtual/powercap inside containers with an empty directory; mount the host directory
    elsewhere (e.g. /host/powercap) and point root at it. root may have the flat layout of
    /sys/class/powercap (intel-rapl:N, intel-rapl:N:M) or the nested layout of
    /sys/devices/virtual/powercap. core/uncore are part of the package and not counted separately.
    Power comes from counter differences, so the first reading has no power.
    """

    requires = "kernel powercap (bare metal; most cloud VMs do not provide it)"
    KIND = {"package": "cpu", "dram": "dram", "psys": "platform"}
    PATTERNS = ("intel-rapl:*", "*/intel-rapl:*", "*/intel-rapl:*/intel-rapl:*")  # domains and subdomains, both layouts

    def __init__(self, root: str = "/sys/class/powercap", **params):
        super().__init__(**params)
        self.root = Path(root)
        self.cp = _CounterPower()
        self.zones: list[tuple[str, str, Path, float | None]] = []
        found = {p.name: p for pat in self.PATTERNS for p in self.root.glob(pat)}  # deduplicated by domain name
        for z in (found[k] for k in sorted(found)):
            try:
                name = _read(z / "name")
            except OSError:
                continue
            if (base := name.split("-")[0]) not in self.KIND:
                continue
            dev = name if base == "package" else f"{name}-{z.name.split(':')[1]}"
            if _float_or_none(z / "energy_uj") is None:
                raise SensorError(f"rapl: {z / 'energy_uj'} is not readable (usually root is required)")
            self.zones.append((dev, self.KIND[base], z, _float_or_none(z / "max_energy_range_uj")))
        if not self.zones:
            raise SensorError(f"rapl: no package / dram / psys domain under {self.root}")

    @classmethod
    def detect(cls) -> bool:
        files = glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")
        return bool(files) and os.access(files[0], os.R_OK)

    def capabilities(self) -> dict:
        return {dev: {"kind": kind, "max_energy_range_uj": mx} for dev, kind, _, mx in self.zones}

    def read(self) -> list[Reading]:
        out, now = [], time.monotonic()
        for dev, kind, z, mx in self.zones:
            r = Reading(self.id, dev, kind, "intel/amd", "RAPL")
            try:
                p, total, reset = self.cp.update(dev, float(_read(z / "energy_uj")), mx, now)
                r.energy_j = total / 1e6
                if reset:
                    r.errors["energy_j"] = "energy counter dropped with an unknown range (reset)"
                if p is not None:
                    r.power_w = p / 1e6
            except (OSError, ValueError) as e:
                r.errors["energy_j"] = repr(e)
            out.append(r)
        return out


@register_sensor("ipmi")
class IpmiSensor(Sensor):
    """Whole-server power: DCMI reading of the local BMC (ipmitool dcmi power reading via /dev/ipmi0).

    Some BMCs return sporadic glitches; set the generic parameter max_w (larger readings count as missing).
    """

    requires = "ipmitool and /dev/ipmi0 (BMC with DCMI)"
    PAT = re.compile(r"Instantaneous power reading:\s*([0-9.]+)\s*Watts", re.I)
    CMD = ["ipmitool", "dcmi", "power", "reading"]

    def __init__(self, device: str = "server", **params):
        super().__init__(**params)
        if shutil.which("ipmitool") is None:
            raise SensorError("ipmi: ipmitool is not on PATH")
        if not os.path.exists("/dev/ipmi0"):
            raise SensorError("ipmi: /dev/ipmi0 is missing (map it into containers, e.g. docker run --device /dev/ipmi0)")
        self.device = device

    @classmethod
    def detect(cls) -> bool:
        return shutil.which("ipmitool") is not None and os.path.exists("/dev/ipmi0")

    @classmethod
    def parse(cls, text: str) -> float:
        if (m := cls.PAT.search(text)) is None:
            raise SensorError(f"cannot parse the power from the ipmitool output: {text.strip()[:200]!r}")
        return float(m.group(1))

    def read(self) -> list[Reading]:
        return [Reading(self.id, self.device, "server", "generic", "BMC", power_w=self.parse(_run(self.CMD, 10)))]


@register_sensor("command")
class CommandSensor(Sensor):
    """Any other device: run a command and extract power (W) with a regular expression whose first
    group is the value; several matches are several devices (<device>0, <device>1, ...).

    kind is the reading kind (gpu, npu, soc are accelerators; cpu, dram, server, platform are host
    parts) and decides how the reading enters server power.
    Example: {type: command, cmd: ["cat", "/run/pdu/watts"], regex: "([0-9.]+)", kind: server, device: pdu}
    """

    requires = "none (subprocess)"

    def __init__(self, cmd: list[str] | str, regex: str, kind: str = "other", vendor: str = "generic",
                 device: str | None = None, scale: float = 1.0, timeout: float = 5.0, **params):
        super().__init__(**params)
        self.cmd = cmd if isinstance(cmd, list) else cmd.split()
        self.pat = re.compile(regex, re.M)
        self.kind, self.vendor, self.device, self.scale, self.timeout = kind, vendor, device, scale, timeout

    def parse(self, text: str) -> list[Reading]:
        if not (vals := [float(m.group(1)) * self.scale for m in self.pat.finditer(text)]):
            raise SensorError(f"no value matching {self.pat.pattern!r} in the command output")
        base = self.device or self.kind
        return [Reading(self.id, base if len(vals) == 1 and self.device else f"{base}{i}", self.kind, self.vendor, power_w=v)
                for i, v in enumerate(vals)]

    def read(self) -> list[Reading]:
        return self.parse(_run(self.cmd, self.timeout))
