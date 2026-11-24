"""内置传感器。

| 类型          | 硬件 / 平台                                   | 接口                                         |
|---------------|-----------------------------------------------|----------------------------------------------|
| nvml          | NVIDIA GPU（数据中心卡与消费卡）Linux/Windows | NVIDIA 官方 NVML（nvidia-ml-py）             |
| nvidia-smi    | NVIDIA GPU，无 Python 绑定时的后备            | nvidia-smi --query-gpu                       |
| amdsmi        | AMD Instinct / Radeon，Linux                  | AMD SMI Python 库（ROCm 自带）               |
| hwmon         | AMD GPU（amdgpu）、Intel 独显（i915/xe），Linux| 内核 hwmon sysfs（无需额外依赖）             |
| rapl          | Intel / AMD CPU 封装与内存，Linux             | 内核 powercap sysfs（RAPL）                  |
| ascend-dcmi   | 华为昇腾 NPU，Linux                           | 昇腾驱动 DCMI 库（libdcmi.so，ctypes 调用）  |
| ipmi          | 服务器整机功率（本机或远程 BMC）              | ipmitool dcmi power reading                  |
| redfish       | 服务器整机功率（BMC 的 DMTF Redfish 接口）    | HTTPS REST                                   |
| powermetrics  | Apple Silicon（CPU/GPU），macOS               | powermetrics（需 root）                      |
| lhm           | Windows 上的 CPU/GPU 功率                     | LibreHardwareMonitor 的 WMI 接口             |
| prometheus    | 任意已有导出器：dcgm-exporter、Kepler、        | Prometheus HTTP API（PromQL）               |
|               | node_exporter、ipmi_exporter、NPU-Exporter…   |                                              |
| command       | 任意厂商命令行工具（寒武纪 cnmon、海光 hy-smi、 | 子进程 + 正则                                |
|               | 摩尔线程 mthreads-gmi、昆仑芯 xpu-smi……）      |                                              |
| http-json     | 智能插座/PDU/自研采集板等 HTTP JSON 接口      | HTTP GET + 字段路径                          |
"""

from __future__ import annotations

import ctypes
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .base import Reading, Sensor, register_sensor

log = logging.getLogger("cpc.sensors")


def _run(cmd: list[str], timeout: float = 5.0, env: dict[str, str] | None = None) -> str:
    full_env = {**os.environ, **env} if env else None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=full_env).stdout


def _read(path: Path | str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class _CounterPower:
    """由累计能量计数器的差分得到功率；处理计数器回绕。"""

    def __init__(self) -> None:
        self.last: dict[str, tuple[float, float]] = {}  # key -> (t, raw)
        self.total: dict[str, float] = {}  # key -> 累计能量（与 raw 同单位）

    def update(self, key: str, raw: float, max_range: float | None, now: float) -> tuple[float | None, float]:
        prev = self.last.get(key)
        self.last[key] = (now, raw)
        if prev is None:
            self.total.setdefault(key, 0.0)
            return None, self.total[key]
        dt = now - prev[0]
        delta = raw - prev[1]
        if delta < 0:  # 回绕
            delta = (raw + max_range - prev[1]) if max_range else 0.0
        self.total[key] = self.total.get(key, 0.0) + delta
        return (delta / dt if dt > 0 else None), self.total[key]


# ======================================================================= NVIDIA


@register_sensor("nvml")
class NvmlSensor(Sensor):
    platforms = "Linux / Windows"
    verified = True

    def __init__(self, gpus: list[int] | None = None, **params):
        super().__init__(**params)
        import pynvml  # nvidia-ml-py

        self.nv = pynvml
        self.nv.nvmlInit()
        n = self.nv.nvmlDeviceGetCount()
        self.idx = gpus if gpus is not None else list(range(n))
        self.h = {i: self.nv.nvmlDeviceGetHandleByIndex(i) for i in self.idx}
        self.names = {}
        for i, h in self.h.items():
            nm = self.nv.nvmlDeviceGetName(h)
            self.names[i] = nm.decode() if isinstance(nm, bytes) else nm

    @classmethod
    def detect(cls) -> bool:
        try:
            import pynvml

            pynvml.nvmlInit()
            return pynvml.nvmlDeviceGetCount() > 0
        except Exception:  # noqa: BLE001
            return False

    def read(self) -> list[Reading]:
        out = []
        for i, h in self.h.items():
            r = Reading("nvml", f"gpu{i}", "gpu", "nvidia", self.names[i])
            try:
                r.power_w = self.nv.nvmlDeviceGetPowerUsage(h) / 1000.0
            except self.nv.NVMLError:
                pass
            try:
                r.energy_j = self.nv.nvmlDeviceGetTotalEnergyConsumption(h) / 1000.0  # mJ → J（Volta 及以后）
            except self.nv.NVMLError:
                pass
            try:
                r.util_pct = float(self.nv.nvmlDeviceGetUtilizationRates(h).gpu)
            except self.nv.NVMLError:
                pass
            out.append(r)
        return out

    def set_power_limit(self, device: str, ratio: float) -> None:
        h = self.h[int(device.removeprefix("gpu"))]
        lo, hi = self.nv.nvmlDeviceGetPowerManagementLimitConstraints(h)
        self.nv.nvmlDeviceSetPowerManagementLimit(h, int(max(lo, min(hi, ratio * hi))))  # 需要 root/管理员


@register_sensor("nvidia-smi")
class NvidiaSmiSensor(Sensor):
    platforms = "Linux / Windows"
    verified = True
    QUERY = "index,name,power.draw,utilization.gpu"

    @classmethod
    def detect(cls) -> bool:
        return shutil.which("nvidia-smi") is not None

    @staticmethod
    def parse(text: str) -> list[Reading]:
        out = []
        for line in text.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue

            def num(s: str) -> float | None:
                try:
                    return float(s)
                except ValueError:
                    return None  # "[N/A]" 等

            out.append(Reading("nvidia-smi", f"gpu{parts[0]}", "gpu", "nvidia", parts[1], power_w=num(parts[2]), util_pct=num(parts[3])))
        return out

    def read(self) -> list[Reading]:
        return self.parse(_run(["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"]))


# ======================================================================= AMD / Intel GPU


@register_sensor("amdsmi")
class AmdSmiSensor(Sensor):
    """AMD SMI Python 库（ROCm 6+ 自带）。不同 ROCm 版本返回字段名略有差异，这里做了兼容。"""

    platforms = "Linux（ROCm）"

    def __init__(self, **params):
        super().__init__(**params)
        import amdsmi  # type: ignore

        self.a = amdsmi
        self.a.amdsmi_init()
        self.h = self.a.amdsmi_get_processor_handles()
        self.cp = _CounterPower()

    @classmethod
    def detect(cls) -> bool:
        try:
            import amdsmi  # type: ignore

            amdsmi.amdsmi_init()
            return len(amdsmi.amdsmi_get_processor_handles()) > 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _num(v) -> float | None:
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def read(self) -> list[Reading]:
        out, now = [], time.monotonic()
        for i, h in enumerate(self.h):
            r = Reading("amdsmi", f"gpu{i}", "gpu", "amd")
            try:
                r.model = str(self.a.amdsmi_get_gpu_asic_info(h).get("market_name", ""))
            except Exception:  # noqa: BLE001
                pass
            try:
                info = self.a.amdsmi_get_power_info(h)
                for k in ("current_socket_power", "average_socket_power", "socket_power"):
                    p = self._num(info.get(k))
                    if p is not None:
                        r.power_w = p
                        break
            except Exception:  # noqa: BLE001
                pass
            try:
                e = self.a.amdsmi_get_energy_count(h)
                raw = self._num(e.get("energy_accumulator", e.get("power")))
                res = self._num(e.get("counter_resolution")) or 1.0  # µJ / 计数
                if raw is not None:
                    p2, tot = self.cp.update(r.device, raw * res, None, now)
                    r.energy_j = tot / 1e6
                    if r.power_w is None and p2 is not None:
                        r.power_w = p2 / 1e6
            except Exception:  # noqa: BLE001
                pass
            try:
                r.util_pct = self._num(self.a.amdsmi_get_gpu_activity(h).get("gfx_activity"))
            except Exception:  # noqa: BLE001
                pass
            out.append(r)
        return out


@register_sensor("hwmon")
class HwmonSensor(Sensor):
    """Linux 内核 hwmon：amdgpu（power1_average / power1_input，µW）与 Intel i915/xe（energy1_input，µJ）。"""

    platforms = "Linux"
    VENDOR = {"amdgpu": "amd", "i915": "intel", "xe": "intel"}

    def __init__(self, root: str = "/sys/class/hwmon", drivers: list[str] | None = None, **params):
        super().__init__(**params)
        self.root = Path(root)
        self.drivers = drivers or ["amdgpu", "i915", "xe"]
        self.cp = _CounterPower()
        self.devs: list[tuple[str, Path, str]] = []  # (device id, hwmon dir, driver)
        count: dict[str, int] = {}
        for d in sorted(self.root.glob("hwmon*")):
            try:
                drv = _read(d / "name")
            except OSError:
                continue
            if drv in self.drivers:
                k = count.get(drv, 0)
                count[drv] = k + 1
                self.devs.append((f"{drv}{k}", d, drv))

    @classmethod
    def detect(cls) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        return any(_safe_read(p) in cls.VENDOR for p in glob.glob("/sys/class/hwmon/hwmon*/name"))

    def read(self) -> list[Reading]:
        out, now = [], time.monotonic()
        for dev, d, drv in self.devs:
            r = Reading("hwmon", dev, "gpu", self.VENDOR.get(drv, "generic"), drv)
            for f in ("power1_input", "power1_average"):
                if (d / f).exists():
                    try:
                        r.power_w = int(_read(d / f)) / 1e6
                        break
                    except (OSError, ValueError):
                        pass
            if (d / "energy1_input").exists():
                try:
                    p, tot = self.cp.update(dev, int(_read(d / "energy1_input")), None, now)
                    r.energy_j = tot / 1e6
                    if r.power_w is None and p is not None:
                        r.power_w = p / 1e6
                except (OSError, ValueError):
                    pass
            busy = d / "device" / "gpu_busy_percent"
            if busy.exists():
                try:
                    r.util_pct = float(_read(busy))
                except (OSError, ValueError):
                    pass
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

    注意：出于安全原因，许多发行版的 energy_uj 仅 root 可读；容器内需挂载 /sys/class/powercap 并具备权限。
    core/uncore 是 package 的子集，不单独计入，避免重复。
    """

    platforms = "Linux（裸金属；多数云虚拟机不提供 RAPL）"
    KIND = {"package": "cpu", "dram": "dram", "psys": "platform"}

    def __init__(self, root: str = "/sys/class/powercap", sep: str = ":", **params):
        super().__init__(**params)
        self.root = Path(root)
        self.cp = _CounterPower()
        self.zones: list[tuple[str, str, Path, float | None]] = []
        for z in sorted(self.root.glob(f"intel-rapl{sep}*")):  # sep 仅为测试而设（Windows 目录名不能含冒号）
            try:
                name = _read(z / "name")
            except OSError:
                continue
            base = name.split("-")[0]
            if base not in self.KIND:
                continue
            parent = z.name.split(sep)[1]
            dev = name if base == "package" else f"{name}-{parent}"
            try:
                mx = float(_read(z / "max_energy_range_uj"))
            except (OSError, ValueError):
                mx = None
            self.zones.append((dev, self.KIND[base], z, mx))

    @classmethod
    def detect(cls) -> bool:
        if not sys.platform.startswith("linux"):
            return False
        files = glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")
        return bool(files) and os.access(files[0], os.R_OK)

    def read(self) -> list[Reading]:
        out, now = [], time.monotonic()
        for dev, kind, z, mx in self.zones:
            try:
                raw = float(_read(z / "energy_uj"))
            except (OSError, ValueError):
                continue
            p, tot = self.cp.update(dev, raw, mx, now)
            out.append(Reading("rapl", dev, kind, "intel/amd", "RAPL", power_w=None if p is None else p / 1e6, energy_j=tot / 1e6))
        return out


# ======================================================================= Huawei Ascend


@register_sensor("ascend-dcmi")
class AscendDcmiSensor(Sensor):
    """华为昇腾 NPU：通过昇腾驱动自带的 DCMI 库（华为《DCMI API 参考》）读取芯片功耗。

    调用顺序：dcmi_init → dcmi_get_card_list → dcmi_get_device_id_in_card →
    dcmi_get_device_power_info（单位 0.1 W）/ dcmi_get_device_utilization_rate（类型 2 = AI Core）。
    在 Kubernetes 昇腾集群中，更推荐部署华为官方的 NPU-Exporter，再用 prometheus 传感器读取。
    """

    platforms = "Linux（昇腾驱动）"
    LIB_PATHS = ["/usr/local/Ascend/driver/lib64/driver/libdcmi.so", "/usr/local/dcmi/libdcmi.so",
                 "/opt/Ascend/driver/lib64/driver/libdcmi.so"]

    def __init__(self, lib: str | None = None, **params):
        super().__init__(**params)
        path = lib or next((p for p in self.LIB_PATHS if os.path.exists(p)), None)
        if path is None:
            raise OSError("未找到 libdcmi.so")
        self.lib = ctypes.CDLL(path)
        if self.lib.dcmi_init() != 0:
            raise OSError("dcmi_init 失败")
        n = ctypes.c_int(0)
        cards = (ctypes.c_int * 64)()
        if self.lib.dcmi_get_card_list(ctypes.byref(n), cards, 64) != 0:
            raise OSError("dcmi_get_card_list 失败")
        self.devs: list[tuple[int, int]] = []
        for k in range(n.value):
            card = cards[k]
            dmax, mcu, cpu = ctypes.c_int(0), ctypes.c_int(0), ctypes.c_int(0)
            if self.lib.dcmi_get_device_id_in_card(card, ctypes.byref(dmax), ctypes.byref(mcu), ctypes.byref(cpu)) == 0:
                self.devs += [(card, d) for d in range(dmax.value)]

    @classmethod
    def detect(cls) -> bool:
        return sys.platform.startswith("linux") and any(os.path.exists(p) for p in cls.LIB_PATHS)

    def read(self) -> list[Reading]:
        out = []
        for card, dev in self.devs:
            r = Reading("ascend-dcmi", f"npu{card}-{dev}", "npu", "huawei", "Ascend")
            p = ctypes.c_int(0)
            if self.lib.dcmi_get_device_power_info(card, dev, ctypes.byref(p)) == 0:
                r.power_w = p.value / 10.0
            u = ctypes.c_uint(0)
            if self.lib.dcmi_get_device_utilization_rate(card, dev, 2, ctypes.byref(u)) == 0:
                r.util_pct = float(u.value)
            out.append(r)
        return out


# ======================================================================= 服务器整机


@register_sensor("ipmi")
class IpmiSensor(Sensor):
    """整机功率：ipmitool dcmi power reading（本机 /dev/ipmi0，或通过 host/user 访问远程 BMC）。

    远程 BMC 的密码通过环境变量 IPMI_PASSWORD 传给 ipmitool（-E 选项），不出现在进程参数中。
    """

    platforms = "Linux / Windows（需 ipmitool 与 BMC 支持 DCMI）"
    PAT = re.compile(r"Instantaneous power reading:\s*([0-9.]+)\s*Watts", re.I)

    def __init__(self, host: str | None = None, user: str | None = None, password: str | None = None,
                 interface: str = "lanplus", device: str = "server", **params):
        super().__init__(**params)
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
    def parse(cls, text: str) -> float | None:
        m = cls.PAT.search(text)
        return float(m.group(1)) if m else None

    def read(self) -> list[Reading]:
        return [Reading("ipmi", self.device, "server", "generic", "BMC", power_w=self.parse(_run(self.cmd, 10, self.env)))]


@register_sensor("redfish")
class RedfishSensor(Sensor):
    """整机功率：DMTF Redfish。优先读 Chassis/{id}/EnvironmentMetrics 的 PowerWatts，
    其次读旧版 Chassis/{id}/Power 的 PowerControl[0].PowerConsumedWatts。"""

    platforms = "任意（通过网络访问 BMC）"

    def __init__(self, url: str, username: str | None = None, password: str | None = None, verify: bool | str = True,
                 chassis: list[str] | None = None, **params):
        """verify：True（默认，校验证书）、CA 证书文件路径，或 False（BMC 自签名证书时显式关闭，会打印警告）。"""
        super().__init__(**params)
        import httpx

        if verify is False:
            log.warning("Redfish %s：已关闭 TLS 证书校验，仅应在可信内网中使用", url)
        auth = (username, password or os.environ.get("REDFISH_PASSWORD", "")) if username else None
        self.c = httpx.Client(base_url=url.rstrip("/"), auth=auth, verify=verify, timeout=10.0)
        self.chassis = chassis

    def _members(self) -> list[str]:
        if self.chassis:
            return [f"/redfish/v1/Chassis/{c}" for c in self.chassis]
        return [m["@odata.id"] for m in self.c.get("/redfish/v1/Chassis").json().get("Members", [])]

    @staticmethod
    def parse_env(d: dict) -> float | None:
        v = (d.get("PowerWatts") or {}).get("Reading")
        return float(v) if v is not None else None

    @staticmethod
    def parse_power(d: dict) -> float | None:
        pc = d.get("PowerControl") or []
        v = pc[0].get("PowerConsumedWatts") if pc else None
        return float(v) if v is not None else None

    def read(self) -> list[Reading]:
        out = []
        for path in self._members():
            p = None
            r = self.c.get(path + "/EnvironmentMetrics")
            if r.status_code == 200:
                p = self.parse_env(r.json())
            if p is None:
                r = self.c.get(path + "/Power")
                if r.status_code == 200:
                    p = self.parse_power(r.json())
            if p is not None:
                out.append(Reading("redfish", path.rsplit("/", 1)[-1], "server", "generic", "BMC", power_w=p))
        return out


# ======================================================================= Apple / Windows


@register_sensor("powermetrics")
class PowermetricsSensor(Sensor):
    """Apple Silicon：powermetrics 输出的 CPU/GPU/ANE 功率（mW）。需要 root。"""

    platforms = "macOS（Apple Silicon）"
    PAT = re.compile(r"^(CPU|GPU|ANE) Power:\s*([0-9.]+)\s*mW", re.M)

    def __init__(self, interval_ms: int = 200, **params):
        super().__init__(**params)
        self.cmd = ["powermetrics", "-n", "1", "-i", str(interval_ms), "--samplers", "cpu_power,gpu_power"]

    @classmethod
    def detect(cls) -> bool:
        return sys.platform == "darwin" and shutil.which("powermetrics") is not None and os.geteuid() == 0

    @classmethod
    def parse(cls, text: str) -> list[Reading]:
        kind = {"CPU": "cpu", "GPU": "gpu", "ANE": "npu"}
        return [Reading("powermetrics", k.lower(), kind[k], "apple", "Apple Silicon", power_w=float(v) / 1000.0)
                for k, v in cls.PAT.findall(text)]

    def read(self) -> list[Reading]:
        return self.parse(_run(self.cmd, 10))


@register_sensor("lhm")
class LibreHardwareMonitorSensor(Sensor):
    """Windows：LibreHardwareMonitor（需以管理员运行）通过 WMI 命名空间 root/LibreHardwareMonitor 提供的功率传感器。

    只把 "Package"（CPU 封装）与 GPU 总功率记为 cpu/gpu，其余子项记为 other，避免重复求和。
    """

    platforms = "Windows"
    PS = ("Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | "
          "Where-Object { $_.SensorType -eq 'Power' } | Select-Object Identifier,Name,Value | ConvertTo-Json -Compress")

    @classmethod
    def detect(cls) -> bool:
        if sys.platform != "win32":
            return False
        try:
            return bool(cls._query())
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def _query(cls) -> list[dict]:
        txt = _run(["powershell", "-NoProfile", "-Command", cls.PS], 10).strip()
        if not txt:
            return []
        d = json.loads(txt)
        return d if isinstance(d, list) else [d]

    @staticmethod
    def classify(identifier: str, name: str) -> str:
        ident = identifier.lower()
        if "cpu" in ident:
            return "cpu" if "package" in name.lower() else "other"
        if "gpu" in ident:
            return "gpu" if any(k in name.lower() for k in ("package", "gpu power", "total")) else "other"
        return "other"

    def read(self) -> list[Reading]:
        out = []
        for s in self._query():
            ident, name = s.get("Identifier", ""), s.get("Name", "")
            out.append(Reading("lhm", ident.strip("/").replace("/", "-"), self.classify(ident, name), "generic", name,
                               power_w=float(s["Value"]) if s.get("Value") is not None else None))
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
    """

    platforms = "任意"

    def __init__(self, url: str, query: str, kind: str = "other", vendor: str = "generic", device_label: str | None = None,
                 scale: float = 1.0, name: str = "prometheus", **params):
        super().__init__(**params)
        import httpx

        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=5.0)
        self.query, self.kind, self.vendor, self.label, self.scale, self.tag = query, kind, vendor, device_label, scale, name

    def read(self) -> list[Reading]:
        res = self.c.get("/api/v1/query", params={"query": self.query}).json()["data"]["result"]
        out = []
        for i, s in enumerate(res):
            dev = s["metric"].get(self.label, str(i)) if self.label else str(i)
            out.append(Reading(self.tag, f"{self.kind}{dev}" if self.label else f"{self.kind}{i}", self.kind, self.vendor,
                               power_w=float(s["value"][1]) * self.scale, extra={"labels": s["metric"]}))
        return out


@register_sensor("command")
class CommandSensor(Sensor):
    """任意命令行工具 + 正则。正则的第 1 个捕获组为数值；多处匹配视为多个设备（device0、device1……）。

    示例（寒武纪 MLU，具体输出格式请以所装版本的 cnmon 为准）：
      {type: command, cmd: ["cnmon", "info", "-p"], regex: "Usage\\s*:\\s*([0-9.]+)\\s*W", kind: npu, vendor: cambricon}
    """

    platforms = "任意"

    def __init__(self, cmd: list[str] | str, regex: str, kind: str = "other", vendor: str = "generic", device: str | None = None,
                 scale: float = 1.0, name: str = "command", timeout: float = 5.0, **params):
        super().__init__(**params)
        self.cmd = cmd if isinstance(cmd, list) else cmd.split()
        self.pat = re.compile(regex, re.M)
        self.kind, self.vendor, self.device, self.scale, self.tag, self.timeout = kind, vendor, device, scale, name, timeout

    def parse(self, text: str) -> list[Reading]:
        vals = [float(m.group(1)) * self.scale for m in self.pat.finditer(text)]
        base = self.device or self.kind
        return [Reading(self.tag, base if len(vals) == 1 and self.device else f"{base}{i}", self.kind, self.vendor, power_w=v)
                for i, v in enumerate(vals)]

    def read(self) -> list[Reading]:
        return self.parse(_run(self.cmd, self.timeout))


@register_sensor("http-json")
class HttpJsonSensor(Sensor):
    """HTTP JSON 接口（智能插座、PDU、自研采集板）。path 为点号分隔的字段路径，支持 [i] 下标，例如 "meters[0].power"。"""

    platforms = "任意"

    def __init__(self, url: str, path: str, kind: str = "server", device: str = "meter", scale: float = 1.0,
                 headers: dict | None = None, name: str = "http-json", **params):
        super().__init__(**params)
        import httpx

        self.c = httpx.Client(timeout=5.0, headers=headers or {})
        self.url, self.path, self.kind, self.device, self.scale, self.tag = url, path, kind, device, scale, name

    @staticmethod
    def extract(d, path: str):
        for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
            d = d[int(part[1:-1])] if part.startswith("[") else d[part]
        return d

    def read(self) -> list[Reading]:
        v = self.extract(self.c.get(self.url).json(), self.path)
        return [Reading(self.tag, self.device, self.kind, power_w=float(v) * self.scale)]
