"""内置传感器（Linux）。

| 类型          | 硬件                                          | 接口                                         |
|---------------|-----------------------------------------------|----------------------------------------------|
| nvml          | NVIDIA GPU（数据中心卡与消费卡）              | NVIDIA 官方 NVML（nvidia-ml-py）             |
| nvidia-smi    | NVIDIA GPU                                    | nvidia-smi --query-gpu                       |
| amdsmi        | AMD Instinct / Radeon                         | AMD SMI Python 库（ROCm 自带）               |
| hwmon         | AMD GPU（amdgpu）、Intel 独显（i915/xe）      | 内核 hwmon sysfs（无需额外依赖）             |
| rapl          | Intel / AMD CPU 封装与内存                    | 内核 powercap sysfs（RAPL）                  |
| ascend-dcmi   | 华为昇腾 NPU                                  | 昇腾驱动 DCMI 库（libdcmi.so，ctypes 调用）  |
| ipmi          | 服务器整机功率（本机或远程 BMC）              | ipmitool dcmi power reading                  |
| redfish       | 服务器整机功率（BMC 的 DMTF Redfish 接口）    | HTTPS REST                                   |
| prometheus    | 任意已有导出器：dcgm-exporter、Kepler、        | Prometheus HTTP API（PromQL）               |
|               | node_exporter、ipmi_exporter、NPU-Exporter…   |                                              |
| command       | 任意厂商命令行工具（寒武纪 cnmon、海光 hy-smi、 | 子进程 + 正则                                |
|               | 摩尔线程 mthreads-gmi、昆仑芯 xpu-smi……）      |                                              |
| http-json     | 智能插座/PDU/自研采集板等 HTTP JSON 接口      | HTTP GET + 字段路径                          |

同一硬件存在多种接口或字段名时（例如 hwmon 的 power1_input / power1_average、Redfish 的
EnvironmentMetrics / Power、不同 ROCm 版本的 AMD SMI 字段名），在构造阶段探测一次并固定使用其一，
探测结果见 capabilities()；运行期不在接口之间切换。
"""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .base import Reading, Sensor, SensorError, register_sensor

log = logging.getLogger("cpc.sensors")


def _run(cmd: list[str], timeout: float = 5.0, env: dict[str, str] | None = None) -> str:
    full_env = {**os.environ, **env} if env else None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=full_env).stdout


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


# ======================================================================= NVIDIA


@register_sensor("nvml")
class NvmlSensor(Sensor):
    """NVIDIA NVML：功率为必需能力；累计能耗计数器（Volta 及以后）与利用率为可选能力，构造时探测。"""

    requires = "NVIDIA 驱动 + nvidia-ml-py"
    verified = True
    supports_power_limit = True

    def __init__(self, gpus: list[int] | None = None, **params):
        super().__init__(**params)
        import pynvml  # nvidia-ml-py

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


@register_sensor("nvidia-smi")
class NvidiaSmiSensor(Sensor):
    requires = "NVIDIA 驱动（nvidia-smi）"
    verified = True
    QUERY = "index,name,power.draw,utilization.gpu"

    def __init__(self, **params):
        super().__init__(**params)
        if shutil.which("nvidia-smi") is None:
            raise SensorError("nvidia-smi 不在 PATH 中")

    @classmethod
    def detect(cls) -> bool:
        return shutil.which("nvidia-smi") is not None

    @staticmethod
    def parse(text: str, sensor: str = "nvidia-smi") -> list[Reading]:
        out = []
        for line in text.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                raise SensorError(f"nvidia-smi 输出格式不符：{line!r}")
            r = Reading(sensor, f"gpu{parts[0]}", "gpu", "nvidia", parts[1])
            for attr, s in (("power_w", parts[2]), ("util_pct", parts[3])):
                try:
                    setattr(r, attr, float(s))
                except ValueError:
                    r.errors[attr] = s  # "[N/A]"、"[Unknown Error]" 等
            out.append(r)
        return out

    def read(self) -> list[Reading]:
        return self.parse(_run(["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"]), self.id)


# ======================================================================= AMD / Intel GPU


@register_sensor("amdsmi")
class AmdSmiSensor(Sensor):
    """AMD SMI Python 库（ROCm 6+ 自带）。不同 ROCm 版本的字段名不同，构造时按设备探测并固定所用字段。"""

    requires = "ROCm（amdsmi）"
    POWER_KEYS = ("current_socket_power", "average_socket_power", "socket_power")
    ENERGY_KEYS = ("energy_accumulator", "power")

    def __init__(self, **params):
        super().__init__(**params)
        import amdsmi  # type: ignore

        self.a = amdsmi
        self.a.amdsmi_init()
        self.h = self.a.amdsmi_get_processor_handles()
        if not self.h:
            raise SensorError("amdsmi：未发现 AMD GPU")
        self.cp = _CounterPower()
        self.power_key: dict[int, str] = {}
        self.energy_key: dict[int, str | None] = {}
        self.models: dict[int, str] = {}
        for i, h in enumerate(self.h):
            info = self.a.amdsmi_get_power_info(h)
            key = next((k for k in self.POWER_KEYS if self._num(info.get(k)) is not None), None)
            if key is None:
                raise SensorError(f"amdsmi：gpu{i} 的功率信息不含 {self.POWER_KEYS} 中的任一字段（实际字段 {sorted(info)}）")
            self.power_key[i] = key
            try:
                e = self.a.amdsmi_get_energy_count(h)
                self.energy_key[i] = next((k for k in self.ENERGY_KEYS if self._num(e.get(k)) is not None), None)
                if self.energy_key[i] is not None and self._num(e.get("counter_resolution")) is None:
                    raise SensorError(f"amdsmi：gpu{i} 的能耗计数缺少 counter_resolution")
            except self.a.AmdSmiException:
                self.energy_key[i] = None
            self.models[i] = str(self.a.amdsmi_get_gpu_asic_info(h).get("market_name", ""))

    @classmethod
    def detect(cls) -> bool:
        try:
            import amdsmi  # type: ignore

            amdsmi.amdsmi_init()
            return len(amdsmi.amdsmi_get_processor_handles()) > 0
        except Exception:  # noqa: BLE001 —— 仅用于信息展示
            return False

    @staticmethod
    def _num(v) -> float | None:
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _req(self, d: dict, key: str) -> float:
        v = self._num(d.get(key))
        if v is None:
            raise SensorError(f"字段 {key} 缺失或非数值：{d.get(key)!r}")
        return v

    def capabilities(self) -> dict:
        return {f"gpu{i}": {"power_field": self.power_key[i], "energy_field": self.energy_key[i]} for i in self.power_key}

    def read(self) -> list[Reading]:
        out, now = [], time.monotonic()
        err = (self.a.AmdSmiException, SensorError)
        for i, h in enumerate(self.h):
            r = Reading(self.id, f"gpu{i}", "gpu", "amd", self.models[i])
            self.field(r, "power_w", lambda: self._req(self.a.amdsmi_get_power_info(h), self.power_key[i]), err)
            if self.energy_key[i] is not None:
                def energy() -> float:
                    e = self.a.amdsmi_get_energy_count(h)
                    raw = self._req(e, self.energy_key[i]) * self._req(e, "counter_resolution")  # µJ
                    _, tot, reset = self.cp.update(r.device, raw, None, now)
                    if reset:
                        raise SensorError("能耗计数器下降（复位）")
                    return tot / 1e6

                self.field(r, "energy_j", energy, err)
            self.field(r, "util_pct", lambda: self._req(self.a.amdsmi_get_gpu_activity(h), "gfx_activity"), err)
            out.append(r)
        return out


@register_sensor("hwmon")
class HwmonSensor(Sensor):
    """Linux 内核 hwmon：amdgpu（power1_input / power1_average，µW）与 Intel i915/xe（energy1_input，µJ）。

    每个设备的功率来源在构造时确定：power1_input、power1_average、energy1_input 差分，按此顺序取第一个存在的接口。
    """

    requires = "内核 hwmon（amdgpu / i915 / xe）"
    VENDOR = {"amdgpu": "amd", "i915": "intel", "xe": "intel"}
    POWER_FILES = ("power1_input", "power1_average", "energy1_input")

    def __init__(self, root: str = "/sys/class/hwmon", drivers: list[str] | None = None, devices: list[str] | None = None,
                 **params):
        super().__init__(**params)
        self.root = Path(root)
        self.drivers = drivers or ["amdgpu", "i915", "xe"]
        self.cp = _CounterPower()
        found: list[tuple[str, Path, str]] = []
        count: dict[str, int] = {}
        for d in sorted(self.root.glob("hwmon*")):
            try:
                drv = _read(d / "name")
            except OSError:
                continue
            if drv in self.drivers:
                k = count.get(drv, 0)
                count[drv] = k + 1
                found.append((f"{drv}{k}", d, drv))
        if devices is not None:
            unknown = sorted(set(devices) - {f[0] for f in found})
            if unknown:
                raise SensorError(f"hwmon：未找到设备 {unknown}（已发现 {[f[0] for f in found]}）")
            found = [f for f in found if f[0] in devices]
        if not found:
            raise SensorError(f"hwmon：{self.root} 下没有驱动为 {self.drivers} 的设备")
        self.devs: list[tuple[str, Path, str, str]] = []  # (device id, hwmon dir, driver, power source)
        for dev, d, drv in found:
            src = next((f for f in self.POWER_FILES if (d / f).exists()), None)
            if src is None:
                raise SensorError(f"hwmon：设备 {dev}（{d}）没有功率或能量接口；可用 devices 参数只选择有接口的设备")
            self.devs.append((dev, d, drv, src))

    @classmethod
    def detect(cls) -> bool:
        return any(_safe_read(p) in cls.VENDOR for p in glob.glob("/sys/class/hwmon/hwmon*/name"))

    def capabilities(self) -> dict:
        return {dev: {"power_source": src, "energy_counter": (d / "energy1_input").exists()} for dev, d, _, src in self.devs}

    def read(self) -> list[Reading]:
        out, now = [], time.monotonic()
        err = (OSError, ValueError)
        for dev, d, drv, src in self.devs:
            r = Reading(self.id, dev, "gpu", self.VENDOR.get(drv, "generic"), drv)
            if src != "energy1_input":
                self.field(r, "power_w", lambda: int(_read(d / src)) / 1e6, err)
            if (d / "energy1_input").exists():
                try:
                    p, tot, reset = self.cp.update(dev, int(_read(d / "energy1_input")), None, now)
                    r.energy_j = tot / 1e6
                    if reset:
                        r.errors["energy_j"] = "能耗计数器下降（复位）"
                    if src == "energy1_input" and p is not None:
                        r.power_w = p / 1e6  # 首次采样没有前一时刻，功率为 None
                except err as e:
                    r.errors["energy_j"] = repr(e)
            busy = d / "device" / "gpu_busy_percent"
            if busy.exists():
                self.field(r, "util_pct", lambda: float(_read(busy)), err)
            out.append(r)
        return out


def _safe_read(p: str) -> str:
    try:
        return _read(p)
    except OSError:
        return ""


# ======================================================================= CPU (RAPL)


@register_sensor("rapl")
class RaplSensor(Sensor):
    """Linux powercap RAPL：Intel 与 AMD（Zen）CPU 的封装（package）、内存（dram）、平台（psys）能耗计数器。

    出于安全原因，许多发行版的 energy_uj 仅 root 可读；容器内需挂载 /sys/class/powercap 并具备权限。
    core/uncore 是 package 的子集，不单独计入，避免重复。功率由计数器差分得到，首次读数的功率为 None。
    """

    requires = "内核 powercap（裸金属；多数云虚拟机不提供 RAPL）"
    KIND = {"package": "cpu", "dram": "dram", "psys": "platform"}

    def __init__(self, root: str = "/sys/class/powercap", **params):
        super().__init__(**params)
        self.root = Path(root)
        self.cp = _CounterPower()
        self.zones: list[tuple[str, str, Path, float | None]] = []
        for z in sorted(self.root.glob("intel-rapl:*")):  # 顶层域 intel-rapl:N 与子域 intel-rapl:N:M
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


# ======================================================================= Huawei Ascend


@register_sensor("ascend-dcmi")
class AscendDcmiSensor(Sensor):
    """华为昇腾 NPU：通过昇腾驱动自带的 DCMI 库（华为《DCMI API 参考》）读取芯片功耗。

    调用顺序：dcmi_init → dcmi_get_card_list → dcmi_get_device_id_in_card →
    dcmi_get_device_power_info（单位 0.1 W）/ dcmi_get_device_utilization_rate（类型 2 = AI Core）。
    在 Kubernetes 昇腾集群中，也可部署华为官方的 NPU-Exporter，再用 prometheus 传感器读取。
    """

    requires = "昇腾驱动（libdcmi.so）"
    LIB_PATHS = ["/usr/local/Ascend/driver/lib64/driver/libdcmi.so", "/usr/local/dcmi/libdcmi.so",
                 "/opt/Ascend/driver/lib64/driver/libdcmi.so"]

    def __init__(self, lib: str | None = None, **params):
        super().__init__(**params)
        path = lib or next((p for p in self.LIB_PATHS if os.path.exists(p)), None)
        if path is None:
            raise SensorError(f"ascend-dcmi：未找到 libdcmi.so（查找位置 {self.LIB_PATHS}；可用 lib 参数指定）")
        self.lib = ctypes.CDLL(path)
        if self.lib.dcmi_init() != 0:
            raise SensorError("ascend-dcmi：dcmi_init 失败")
        n = ctypes.c_int(0)
        cards = (ctypes.c_int * 64)()
        if self.lib.dcmi_get_card_list(ctypes.byref(n), cards, 64) != 0:
            raise SensorError("ascend-dcmi：dcmi_get_card_list 失败")
        self.devs: list[tuple[int, int]] = []
        for k in range(n.value):
            card = cards[k]
            dmax, mcu, cpu = ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(0)
            if self.lib.dcmi_get_device_id_in_card(card, ctypes.byref(dmax), ctypes.byref(mcu), ctypes.byref(cpu)) != 0:
                raise SensorError(f"ascend-dcmi：dcmi_get_device_id_in_card({card}) 失败")
            self.devs += [(card, d) for d in range(dmax.value)]
        if not self.devs:
            raise SensorError("ascend-dcmi：未发现 NPU")

    @classmethod
    def detect(cls) -> bool:
        return any(os.path.exists(p) for p in cls.LIB_PATHS)

    def read(self) -> list[Reading]:
        out = []
        for card, dev in self.devs:
            r = Reading(self.id, f"npu{card}-{dev}", "npu", "huawei", "Ascend")
            p = ctypes.c_int(0)
            rc = self.lib.dcmi_get_device_power_info(card, dev, ctypes.byref(p))
            if rc == 0:
                r.power_w = p.value / 10.0
            else:
                r.errors["power_w"] = f"dcmi_get_device_power_info 返回 {rc}"
            u = ctypes.c_uint(0)
            rc = self.lib.dcmi_get_device_utilization_rate(card, dev, 2, ctypes.byref(u))
            if rc == 0:
                r.util_pct = float(u.value)
            else:
                r.errors["util_pct"] = f"dcmi_get_device_utilization_rate 返回 {rc}"
            out.append(r)
        return out


# ======================================================================= 服务器整机


@register_sensor("ipmi")
class IpmiSensor(Sensor):
    """整机功率：ipmitool dcmi power reading（本机 /dev/ipmi0，或通过 host/user 访问远程 BMC）。

    远程 BMC 的密码通过环境变量 IPMI_PASSWORD 传给 ipmitool（-E 选项），不出现在进程参数中。
    """

    requires = "ipmitool；BMC 支持 DCMI"
    PAT = re.compile(r"Instantaneous power reading:\s*([0-9.]+)\s*Watts", re.I)

    def __init__(self, host: str | None = None, user: str | None = None, password: str | None = None,
                 interface: str = "lanplus", device: str = "server", **params):
        super().__init__(**params)
        if shutil.which("ipmitool") is None:
            raise SensorError("ipmitool 不在 PATH 中")
        self.cmd = ["ipmitool"]
        self.env: dict[str, str] | None = None
        if host:
            self.cmd += ["-I", interface, "-H", host, "-U", user or "", "-E"]
            if password:
                self.env = {"IPMI_PASSWORD": password}
        self.cmd += ["dcmi", "power", "reading"]
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
        return [Reading(self.id, self.device, "server", "generic", "BMC", power_w=self.parse(_run(self.cmd, 10, self.env)))]


@register_sensor("redfish")
class RedfishSensor(Sensor):
    """整机功率：DMTF Redfish。每个机箱使用的资源在构造时确定（endpoint 参数显式指定，或探测）：
    environment_metrics = Chassis/{id}/EnvironmentMetrics 的 PowerWatts.Reading；
    power               = 旧版 Chassis/{id}/Power 的 PowerControl[0].PowerConsumedWatts。"""

    requires = "BMC 的 Redfish 接口（网络）"
    PATHS = {"environment_metrics": "/EnvironmentMetrics", "power": "/Power"}

    def __init__(self, url: str, username: str | None = None, password: str | None = None, verify: bool | str = True,
                 chassis: list[str] | None = None, endpoint: str | None = None, **params):
        """verify：True（默认，校验证书）、CA 证书文件路径，或 False（BMC 自签名证书时显式关闭，会打印警告）。"""
        super().__init__(**params)
        import httpx

        if endpoint is not None and endpoint not in self.PATHS:
            raise SensorError(f"redfish：endpoint 只能为 {sorted(self.PATHS)}")
        if verify is False:
            log.warning("Redfish %s：已关闭 TLS 证书校验，仅应在可信内网中使用", url)
        auth = (username, password or os.environ.get("REDFISH_PASSWORD", "")) if username else None
        self.c = httpx.Client(base_url=url.rstrip("/"), auth=auth, verify=verify, timeout=10.0, trust_env=False)
        if chassis:
            members = [f"/redfish/v1/Chassis/{c}" for c in chassis]
        else:
            r = self.c.get("/redfish/v1/Chassis")
            r.raise_for_status()
            members = [m["@odata.id"] for m in r.json().get("Members", [])]
        if not members:
            raise SensorError(f"redfish：{url} 没有机箱资源")
        self.endpoints: dict[str, str] = {}
        for path in members:
            kinds = [endpoint] if endpoint else list(self.PATHS)
            for k in kinds:
                r = self.c.get(path + self.PATHS[k])
                if r.status_code == 200 and self.parse(k, r.json()) is not None:
                    self.endpoints[path] = k
                    break
            if path not in self.endpoints:
                raise SensorError(f"redfish：机箱 {path} 不提供功率读数（尝试 {kinds}）")

    @staticmethod
    def parse(kind: str, d: dict) -> float | None:
        if kind == "environment_metrics":
            v = (d.get("PowerWatts") or {}).get("Reading")
        else:
            pc = d.get("PowerControl") or []
            v = pc[0].get("PowerConsumedWatts") if pc else None
        return float(v) if v is not None else None

    def capabilities(self) -> dict:
        return {path.rsplit("/", 1)[-1]: {"endpoint": k} for path, k in self.endpoints.items()}

    def read(self) -> list[Reading]:
        out = []
        for path, k in self.endpoints.items():
            r = Reading(self.id, path.rsplit("/", 1)[-1], "server", "generic", "BMC")
            resp = self.c.get(path + self.PATHS[k])
            if resp.status_code != 200:
                r.errors["power_w"] = f"HTTP {resp.status_code}"
            else:
                r.power_w = self.parse(k, resp.json())
                if r.power_w is None:
                    r.errors["power_w"] = "响应中没有功率读数"
            out.append(r)
        return out


# ======================================================================= 通用适配器


@register_sensor("prometheus")
class PrometheusSensor(Sensor):
    """从 Prometheus 查询已有导出器的功率（或能耗计数）。示例：

    - NVIDIA dcgm-exporter：query="DCGM_FI_DEV_POWER_USAGE{Hostname='node1'}", device_label="gpu", kind="gpu"
    - Kepler：query="kepler_node_cpu_watts{instance='node1:28282'}", kind="cpu"
    - node_exporter RAPL：query="rate(node_rapl_package_joules_total{instance='node1:9100'}[30s])", kind="cpu"
    - ipmi_exporter：query="ipmi_dcmi_power_consumption_watts{instance='bmc1'}", kind="server"
    - 昇腾 NPU-Exporter、智能 PDU 导出器等：按其指标名填写 query

    查询结果中的每条时间序列对应一个设备；device_label 给出时设备名为 <kind><标签值>，否则按结果顺序编号。
    """

    requires = "无（经网络或子进程访问）"

    def __init__(self, url: str, query: str, kind: str = "other", vendor: str = "generic", device_label: str | None = None,
                 scale: float = 1.0, **params):
        super().__init__(**params)
        import httpx

        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=5.0, trust_env=False)
        self.query, self.kind, self.vendor, self.label, self.scale = query, kind, vendor, device_label, scale

    def read(self) -> list[Reading]:
        resp = self.c.get("/api/v1/query", params={"query": self.query})
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise SensorError(f"Prometheus 查询失败：{body.get('error')}")
        out = []
        for i, s in enumerate(body["data"]["result"]):
            if self.label and self.label not in s["metric"]:
                raise SensorError(f"查询结果缺少标签 {self.label}：{s['metric']}")
            dev = f"{self.kind}{s['metric'][self.label]}" if self.label else f"{self.kind}{i}"
            out.append(Reading(self.id, dev, self.kind, self.vendor, power_w=float(s["value"][1]) * self.scale,
                               extra={"labels": s["metric"]}))
        return out


@register_sensor("command")
class CommandSensor(Sensor):
    """任意命令行工具 + 正则。正则的第 1 个捕获组为数值；多处匹配视为多个设备（device0、device1……）。

    示例（寒武纪 MLU，具体输出格式以所装版本的 cnmon 为准）：
      {type: command, cmd: ["cnmon", "info", "-p"], regex: "Usage\\s*:\\s*([0-9.]+)\\s*W", kind: npu, vendor: cambricon}
    """

    requires = "无（经网络或子进程访问）"

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


@register_sensor("http-json")
class HttpJsonSensor(Sensor):
    """HTTP JSON 接口（智能插座、PDU、自研采集板）。path 为点号分隔的字段路径，支持 [i] 下标，例如 "meters[0].power"。"""

    requires = "无（经网络或子进程访问）"

    def __init__(self, url: str, path: str, kind: str = "server", device: str = "meter", scale: float = 1.0,
                 headers: dict | None = None, vendor: str = "generic", **params):
        super().__init__(**params)
        import httpx

        self.c = httpx.Client(timeout=5.0, headers=headers or {}, trust_env=False)
        self.url, self.path, self.kind, self.device, self.scale, self.vendor = url, path, kind, device, scale, vendor

    @staticmethod
    def extract(d, path: str):
        for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
            d = d[int(part[1:-1])] if part.startswith("[") else d[part]
        return d

    def read(self) -> list[Reading]:
        resp = self.c.get(self.url)
        resp.raise_for_status()
        try:
            v = float(self.extract(resp.json(), self.path))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise SensorError(f"响应中没有可用的字段 {self.path}：{e!r}") from e
        return [Reading(self.id, self.device, self.kind, self.vendor, power_w=v * self.scale)]
