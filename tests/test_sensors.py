"""传感器层：用伪造的 sysfs 目录与命令输出检验解析逻辑（不需要真实硬件）。"""

from pathlib import Path

import pytest

from cpc.config import NodeProfile, UpstreamConfig
from cpc.sensors import EnergyIntegrator, Reading, make_sensor
from cpc.sensors.builtin import (
    CommandSensor, HttpJsonSensor, HwmonSensor, IpmiSensor, NvidiaSmiSensor, RaplSensor, RedfishSensor,
)
from cpc.sensors.exporter import host_total
from cpc.site.core import Node
from cpc.site.power import SensorPower


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_rapl_counter_and_wraparound(tmp_path, monkeypatch):
    # 与内核 powercap 相同的目录布局：顶层域 intel-rapl:N，子域 intel-rapl:N:M
    z = tmp_path / "intel-rapl:0"
    write(z / "name", "package-0")
    write(z / "max_energy_range_uj", "1000000")
    write(z / "energy_uj", "900000")
    write(tmp_path / "intel-rapl:0:0" / "name", "core")  # 子域，不应重复计入
    write(tmp_path / "intel-rapl:0:0" / "energy_uj", "1")
    d = tmp_path / "intel-rapl:0:2"
    write(d / "name", "dram")
    write(d / "energy_uj", "0")
    s = RaplSensor(root=str(tmp_path))
    clock = iter([0.0, 1.0])
    monkeypatch.setattr("cpc.sensors.builtin.time.monotonic", lambda: next(clock))
    first = {r.device: r for r in s.read()}
    assert set(first) == {"package-0", "dram-0"} and first["package-0"].power_w is None
    write(z / "energy_uj", "100000")  # 回绕：900000 → 1000000 → 100000，增量 200000 µJ
    write(d / "energy_uj", "5000000")
    second = {r.device: r for r in s.read()}
    assert second["package-0"].power_w == pytest.approx(0.2)
    assert second["package-0"].energy_j == pytest.approx(0.2)
    assert second["dram-0"].kind == "dram" and second["dram-0"].power_w == pytest.approx(5.0)


def test_hwmon_amdgpu_and_intel(tmp_path):
    a = tmp_path / "hwmon3"
    write(a / "name", "amdgpu")
    write(a / "power1_average", "152000000")  # 152 W
    write(a / "device" / "gpu_busy_percent", "87")
    i = tmp_path / "hwmon5"
    write(i / "name", "i915")
    write(i / "energy1_input", "1000000")
    write(tmp_path / "hwmon0" / "name", "coretemp")  # 无关设备
    rs = {r.device: r for r in HwmonSensor(root=str(tmp_path)).read()}
    assert rs["amdgpu0"].power_w == pytest.approx(152.0) and rs["amdgpu0"].vendor == "amd" and rs["amdgpu0"].util_pct == 87
    assert rs["i9150"].vendor == "intel" and rs["i9150"].energy_j == 0.0
    assert "coretemp0" not in rs


def test_cli_parsers():
    rs = NvidiaSmiSensor.parse("0, NVIDIA A100-SXM4-80GB, 312.45, 97\n1, NVIDIA A100-SXM4-80GB, [N/A], 0\n")
    assert rs[0].power_w == pytest.approx(312.45) and rs[1].power_w is None and rs[1].device == "gpu1"
    ipmi = "    Instantaneous power reading:                   412 Watts\n    Minimum during sampling period:  300 Watts\n"
    assert IpmiSensor.parse(ipmi) == 412.0


def test_generic_adapters():
    c = CommandSensor(cmd=["true"], regex=r"Power\s*:\s*([0-9.]+)\s*W", kind="npu", vendor="x")
    rs = c.parse("Card 0 Power : 75.5 W\nCard 1 Power : 80 W\n")
    assert [r.power_w for r in rs] == [75.5, 80.0] and rs[1].device == "npu1"
    assert HttpJsonSensor.extract({"meters": [{"power": 12.5}]}, "meters[0].power") == 12.5
    assert RedfishSensor.parse_env({"PowerWatts": {"Reading": 640}}) == 640.0
    assert RedfishSensor.parse_power({"PowerControl": [{"PowerConsumedWatts": 512}]}) == 512.0
    with pytest.raises(KeyError):
        make_sensor({"type": "no-such-sensor"})


def test_integrator_and_host_total():
    integ = EnergyIntegrator()
    r0 = [Reading("x", "gpu0", "gpu", power_w=100.0)]
    integ.update(r0, now=0.0)
    r1 = [Reading("x", "gpu0", "gpu", power_w=100.0)]
    integ.update(r1, now=2.0)
    assert r1[0].energy_j == pytest.approx(200.0)
    rs = [Reading("a", "gpu0", "gpu", power_w=300.0), Reading("b", "package-0", "cpu", power_w=100.0)]
    assert host_total(rs) == 400.0
    assert host_total(rs + [Reading("c", "server", "server", power_w=650.0)]) == 650.0  # 整机读数优先


class FakeSensor:
    name = "fake"

    def __init__(self, readings):
        self.readings = readings

    def read(self):
        return [Reading(**r.__dict__) for r in self.readings]


def test_sensor_power_attribution():
    prof = NodeProfile(host_idle_w=100, host_max_w=200)
    nodes = [Node(id=f"n{i}", slots=8, gpus=1, upstream=UpstreamConfig(id=f"n{i}", url="http://x", gpu_indices=[i])) for i in range(2)]
    rs = [Reading("fake", "gpu0", "gpu", "nvidia", power_w=250.0), Reading("fake", "gpu1", "gpu", "nvidia", power_w=150.0),
          Reading("fake", "server", "server", power_w=700.0)]
    SensorPower(prof, [FakeSensor(rs)]).sample(nodes)
    # 主机部分 = 700 − 400 = 300 W，在两个在线节点间平分
    assert nodes[0].it_power_w == pytest.approx(400.0) and nodes[1].it_power_w == pytest.approx(300.0)
    nodes[1].state = "off"
    SensorPower(prof, [FakeSensor(rs[:2])]).sample(nodes)  # 无整机读数 → 主机部分用模型
    assert nodes[0].it_power_w == pytest.approx(250.0 + 100.0) and nodes[1].it_power_w == pytest.approx(150.0)
