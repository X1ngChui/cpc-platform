"""内置传感器（Linux）：每类测量对象一种，互不重叠；其他设备用 command 适配，或以 entry points 注册自己的传感器。

| 类型      | 测量对象                         | 接口                                   | 读数种类         |
|-----------|----------------------------------|----------------------------------------|------------------|
| nvml      | NVIDIA GPU                       | NVIDIA 官方 NVML（nvidia-ml-py）       | gpu              |
| rapl      | CPU 封装与内存（Intel / AMD）    | 内核 powercap sysfs（RAPL 能耗计数器） | cpu、dram        |
| ipmi      | 服务器整机（本机 BMC）           | ipmitool dcmi power reading            | server           |
| command   | 其他任意设备                     | 子进程 + 正则                          | 由配置给出       |

可选能力（如 NVML 的累计能耗计数器与利用率）在构造阶段探测一次，探测结果见 capabilities()；运行期不切换接口。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .base import Reading, Sensor, SensorError, register_sensor


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def _read(path: Path | str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class _CounterPower:
    """由累计能量计数器的差分得到功率。

    计数器下降时：已知计数范围（max_range）则按回绕计算增量；未知则视为计数器复位，
    该区间的功率记为缺失（None），累计量不增加。首次读数没有前一时刻，功率为 None。
    """

    def __init__(self) -> None:
        self.last: dict[str, tuple[float, float]] = {}  # key -> (t, raw)
        self.total: dict[str, float] = {}  # key -> 累计能量（与 raw 同单位）
        self.resets: dict[str, int] = {}

    def update(self, key: str, raw: float, max_range: float | None, now: float) -> tuple[float | None, float, bool]:
        """返回 (功率, 累计量, 本次是否检测到计数器复位)。"""
        prev = self.last.get(key)
        self.last[key] = (now, raw)
        total = self.total.setdefault(key, 0.0)
        if prev is None:
            return None, total, False
        dt = now - prev[0]
        delta = raw - prev[1]
        if delta < 0:
            if not max_range:
                self.resets[key] = self.resets.get(key, 0) + 1
                return None, total, True
            delta = raw + max_range - prev[1]
        self.total[key] = total + delta
        return (delta / dt if dt > 0 else None), self.total[key], False


@register_sensor("nvml")
class NvmlSensor(Sensor):
    """NVIDIA GPU（NVML）：功率为必需能力；累计能耗计数器（Volta 及以后）与利用率为可选能力，构造时探测。

    支持功率封顶（set_power_limit，需要 root）。gpus 参数选择 GPU 序号，缺省为本机全部 GPU。
    """

    requires = "NVIDIA 驱动 + nvidia-ml-py（pip install -e \".[nvml]\"）"
    supports_power_limit = True

    def __init__(self, gpus: list[int] | None = None, **params):
        super().__init__(**params)
        try:
            import pynvml  # nvidia-ml-py
        except ImportError as e:
            raise SensorError("nvml：未安装 nvidia-ml-py（pip install -e \".[nvml]\"）") from e
        self.nv = pynvml
        self.nv.nvmlInit()
        n = self.nv.nvmlDeviceGetCount()
        self.idx = list(range(n)) if gpus is None else list(gpus)
        bad = [i for i in self.idx if not 0 <= i < n]
        if bad or not self.idx:
            raise SensorError(f"nvml：GPU 序号 {bad or self.idx} 无效（本机共 {n} 张 GPU）")
        self.h = {i: self.nv.nvmlDeviceGetHandleByIndex(i) for i in self.idx}
        self.names: dict[int, str] = {}
        self.caps: dict[int, dict[str, bool]] = {}
        for i, h in self.h.items():
            nm = self.nv.nvmlDeviceGetName(h)
            self.names[i] = nm.decode() if isinstance(nm, bytes) else nm
            try:
                self.nv.nvmlDeviceGetPowerUsage(h)
            except self.nv.NVMLError as e:
                raise SensorError(f"nvml：gpu{i} 不提供功率读数（{e}）") from e
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
        except Exception:  # noqa: BLE001 —— 仅用于信息展示
            return False

    def capabilities(self) -> dict:
        return {f"gpu{i}": {"power": True, **c} for i, c in self.caps.items()}

    def read(self) -> list[Reading]:
        out = []
        err = self.nv.NVMLError
        for i, h in self.h.items():
            r = Reading(self.id, f"gpu{i}", "gpu", "nvidia", self.names[i])
            self.field(r, "power_w", lambda: self.nv.nvmlDeviceGetPowerUsage(h) / 1000.0, err)
            if self.caps[i]["energy"]:
                self.field(r, "energy_j", lambda: self.nv.nvmlDeviceGetTotalEnergyConsumption(h) / 1000.0, err)  # mJ → J
            if self.caps[i]["util"]:
                self.field(r, "util_pct", lambda: float(self.nv.nvmlDeviceGetUtilizationRates(h).gpu), err)
            out.append(r)
        return out

    def set_power_limit(self, device: str, ratio: float) -> None:
        h = self.h[int(device.removeprefix("gpu"))]
        lo, hi = self.nv.nvmlDeviceGetPowerManagementLimitConstraints(h)
        self.nv.nvmlDeviceSetPowerManagementLimit(h, int(max(lo, min(hi, ratio * hi))))  # 需要 root


@register_sensor("rapl")
class RaplSensor(Sensor):
    """CPU 封装（package）、内存（dram）、平台（psys）能耗计数器（Linux powercap RAPL，Intel 与 AMD Zen）。

    许多发行版的 energy_uj 仅 root 可读。Docker 默认以空目录屏蔽容器内的 /sys/devices/virtual/powercap，
    容器内读取时把宿主机的该目录挂载到其他路径（如 /host/powercap），并以 root 参数指向它；root 可以是
    /sys/class/powercap 的平铺布局（intel-rapl:N、intel-rapl:N:M），也可以是 /sys/devices/virtual/powercap 的层级布局。
    core/uncore 是 package 的子集，不单独计入。功率由计数器差分得到，首次读数的功率为 None。
    """

    requires = "内核 powercap（裸金属；多数云虚拟机不提供）"
    KIND = {"package": "cpu", "dram": "dram", "psys": "platform"}

    def __init__(self, root: str = "/sys/class/powercap", **params):
        super().__init__(**params)
        self.root = Path(root)
        self.cp = _CounterPower()
        self.zones: list[tuple[str, str, Path, float | None]] = []
        found = {p.name: p for pat in ("intel-rapl:*", "*/intel-rapl:*", "*/intel-rapl:*/intel-rapl:*")
                 for p in self.root.glob(pat)}  # 顶层域 intel-rapl:N 与子域 intel-rapl:N:M；两种布局按域名去重
        for z in (found[k] for k in sorted(found)):
            try:
                name = _read(z / "name")
            except OSError:
                continue
            base = name.split("-")[0]
            if base not in self.KIND:
                continue
            parent = z.name.split(":")[1]
            dev = name if base == "package" else f"{name}-{parent}"
            try:
                mx = float(_read(z / "max_energy_range_uj"))
            except (OSError, ValueError):
                mx = None
            try:
                float(_read(z / "energy_uj"))
            except (OSError, ValueError) as e:
                raise SensorError(f"rapl：{z / 'energy_uj'} 不可读（{e}）；通常需要 root 权限") from e
            self.zones.append((dev, self.KIND[base], z, mx))
        if not self.zones:
            raise SensorError(f"rapl：{self.root} 下没有 package / dram / psys 域")

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
                p, tot, reset = self.cp.update(dev, float(_read(z / "energy_uj")), mx, now)
                r.energy_j = tot / 1e6
                if reset:
                    r.errors["energy_j"] = "能耗计数器下降且回绕范围未知（复位）"
                if p is not None:
                    r.power_w = p / 1e6  # 首次采样没有前一时刻，功率为 None
            except (OSError, ValueError) as e:
                r.errors["energy_j"] = repr(e)
            out.append(r)
        return out


@register_sensor("ipmi")
class IpmiSensor(Sensor):
    """服务器整机功率：本机 BMC 的 DCMI 读数（ipmitool dcmi power reading，经 /dev/ipmi0）。

    部分 BMC 偶发返回异常读数，宜设置通用参数 max_w（超过上限的读数记为缺失）。
    """

    requires = "ipmitool 与 /dev/ipmi0（BMC 支持 DCMI）"
    PAT = re.compile(r"Instantaneous power reading:\s*([0-9.]+)\s*Watts", re.I)
    CMD = ["ipmitool", "dcmi", "power", "reading"]

    def __init__(self, device: str = "server", **params):
        super().__init__(**params)
        if shutil.which("ipmitool") is None:
            raise SensorError("ipmi：ipmitool 不在 PATH 中")
        if not os.path.exists("/dev/ipmi0"):
            raise SensorError("ipmi：没有 /dev/ipmi0（容器内需映射该设备，如 docker run --device /dev/ipmi0）")
        self.device = device

    @classmethod
    def detect(cls) -> bool:
        return shutil.which("ipmitool") is not None and os.path.exists("/dev/ipmi0")

    @classmethod
    def parse(cls, text: str) -> float:
        m = cls.PAT.search(text)
        if m is None:
            raise SensorError(f"无法从 ipmitool 输出中解析功率：{text.strip()[:200]!r}")
        return float(m.group(1))

    def read(self) -> list[Reading]:
        return [Reading(self.id, self.device, "server", "generic", "BMC", power_w=self.parse(_run(self.CMD, 10)))]


@register_sensor("command")
class CommandSensor(Sensor):
    """其他设备：运行命令并用正则提取功率（W）。正则的第 1 个捕获组为数值；多处匹配视为多个设备（<device>0、<device>1……）。

    kind 为读数种类（gpu、npu、soc 为加速卡，cpu、dram、server、platform 为主机部分），决定读数在服务器功率中的归属。
    示例：{type: command, cmd: ["cat", "/run/pdu/watts"], regex: "([0-9.]+)", kind: server, device: pdu}
    """

    requires = "无（子进程）"

    def __init__(self, cmd: list[str] | str, regex: str, kind: str = "other", vendor: str = "generic", device: str | None = None,
                 scale: float = 1.0, timeout: float = 5.0, **params):
        super().__init__(**params)
        self.cmd = cmd if isinstance(cmd, list) else cmd.split()
        self.pat = re.compile(regex, re.M)
        self.kind, self.vendor, self.device, self.scale, self.timeout = kind, vendor, device, scale, timeout

    def parse(self, text: str) -> list[Reading]:
        vals = [float(m.group(1)) * self.scale for m in self.pat.finditer(text)]
        if not vals:
            raise SensorError(f"命令输出中没有与 {self.pat.pattern!r} 匹配的数值")
        base = self.device or self.kind
        return [Reading(self.id, base if len(vals) == 1 and self.device else f"{base}{i}", self.kind, self.vendor, power_w=v)
                for i, v in enumerate(vals)]

    def read(self) -> list[Reading]:
        return self.parse(_run(self.cmd, self.timeout))
