"""传感器层：用伪造的 sysfs 目录与命令输出检验解析逻辑与故障处理（不需要真实硬件）。"""

from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from cpc.config import ConfigError, MeasurementConfig, NodeProfile, UpstreamConfig
from cpc.datacenter import Server
from cpc.health import HealthBoard
from cpc.sensors import EnergyIntegrator, Reading, Sensor, SensorError, make_sensor
from cpc.sensors.builtin import CommandSensor, IpmiSensor, RaplSensor
from cpc.sensors.exporter import read_all
from cpc.site.power import SensorPower


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def sample(reg: CollectorRegistry, name: str, **labels) -> float | None:
    return reg.get_sample_value(name, labels)


# ------------------------------------------------------------------ 硬件传感器的解析与能力探测


def test_rapl_counter_wraparound_and_reset(tmp_path, monkeypatch):
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
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr("cpc.sensors.builtin.time.monotonic", lambda: next(clock))
    first = {r.device: r for r in s.read()}
    assert set(first) == {"package-0", "dram-0"}
    assert first["package-0"].power_w is None and not first["package-0"].errors  # 首次采样没有前一时刻，不算错误
    write(z / "energy_uj", "100000")  # 回绕：900000 → 1000000 → 100000，增量 200000 µJ
    write(d / "energy_uj", "5000000")
    second = {r.device: r for r in s.read()}
    assert second["package-0"].power_w == pytest.approx(0.2)
    assert second["package-0"].energy_j == pytest.approx(0.2)
    assert second["dram-0"].kind == "dram" and second["dram-0"].power_w == pytest.approx(5.0)
    write(d / "energy_uj", "10")  # dram 未给出回绕范围：计数器下降视为复位，功率缺失而不是 0
    third = {r.device: r for r in s.read()}
    assert third["dram-0"].power_w is None and "energy_j" in third["dram-0"].errors
    assert third["dram-0"].energy_j == pytest.approx(5.0)  # 累计量不增加


def test_rapl_unreadable_fails_at_construction(tmp_path):
    with pytest.raises(SensorError):
        RaplSensor(root=str(tmp_path))  # 没有任何域


def test_ipmi_parser():
    ipmi = "    Instantaneous power reading:                   412 Watts\n    Minimum during sampling period:  300 Watts\n"
    assert IpmiSensor.parse(ipmi) == 412.0
    with pytest.raises(SensorError):
        IpmiSensor.parse("Error: unable to get power reading")


def test_command_sensor():
    c = CommandSensor(cmd=["true"], regex=r"Power\s*:\s*([0-9.]+)\s*W", kind="npu", vendor="x")
    rs = c.parse("Card 0 Power : 75.5 W\nCard 1 Power : 80 W\n")
    assert [r.power_w for r in rs] == [75.5, 80.0] and rs[1].device == "npu1"
    with pytest.raises(SensorError):
        c.parse("no reading here")
    one = CommandSensor(cmd=["true"], regex=r"([0-9.]+)", kind="server", device="pdu")
    assert [(r.device, r.power_w) for r in one.parse("431.5\n")] == [("pdu", 431.5)]
    with pytest.raises(ConfigError, match="未知的功率传感器类型"):
        make_sensor({"type": "no-such-sensor"})
    with pytest.raises(SensorError, match="初始化失败"):
        make_sensor({"type": "rapl", "root": "/nonexistent"})


# ------------------------------------------------------------------ 能耗积分与完整率


def test_integrator_skips_missing_intervals():
    integ = EnergyIntegrator()
    key = ("x", "gpu0")
    integ.update([Reading("x", "gpu0", "gpu", power_w=100.0)], now=0.0, expected={key})
    r1 = [Reading("x", "gpu0", "gpu", power_w=None)]  # 本次采样功率缺失
    integ.update(r1, now=2.0, expected={key})
    assert r1[0].energy_j == pytest.approx(200.0)  # [0, 2) 由 t=0 的采样覆盖
    integ.update([], now=5.0, expected={key})  # 读数缺席：[2, 5) 不积分
    r3 = [Reading("x", "gpu0", "gpu", power_w=50.0)]
    integ.update(r3, now=6.0, expected={key})
    assert r3[0].energy_j == pytest.approx(200.0)  # [5, 6) 的左端点仍缺失
    assert integ.measured_s[key] == pytest.approx(2.0) and integ.missing_s[key] == pytest.approx(4.0)
    assert integ.coverage(key) == pytest.approx(1 / 3)


def test_integrator_counter_reset():
    integ = EnergyIntegrator()
    integ.update([Reading("x", "gpu0", "gpu", power_w=1.0, energy_j=1000.0)], now=0.0)
    r = [Reading("x", "gpu0", "gpu", power_w=1.0, energy_j=10.0)]  # 驱动重载，计数器从 0 重新开始
    integ.update(r, now=1.0)
    assert r[0].energy_j == pytest.approx(1010.0) and integ.resets[("x", "gpu0")] == 1


# ------------------------------------------------------------------ 站点的功率归属与故障隔离


class FakeSensor(Sensor):
    """可注入故障的传感器：readings 为当前读数；fail 为异常时 read() 抛出。"""

    name = "fake"

    def __init__(self, readings, sid="fake"):
        super().__init__()
        self.id = sid
        self.readings = readings
        self.fail: Exception | None = None

    def read(self):
        if self.fail:
            raise self.fail
        return [Reading(**{**r.__dict__, "sensor": self.id, "errors": dict(r.errors)}) for r in self.readings]


def two_nodes():
    prof = NodeProfile(slots=8, power_w=[(0, 100), (1, 400)], tpot_s=[(1, 0.02)], prefill_tokens_per_s=1e4, boot_s=0)
    return [Server(id=f"n{i}", pool="p", profile=prof, slots=8, model_power=False, state="on",
                   upstream=UpstreamConfig(id=f"n{i}", url="http://x", gpu_indices=[i])) for i in range(2)]


def readings(gpu0=250.0, gpu1=150.0, server=700.0):
    return [Reading("fake", "gpu0", "gpu", "nvidia", power_w=gpu0), Reading("fake", "gpu1", "gpu", "nvidia", power_w=gpu1),
            Reading("fake", "server", "server", power_w=server)]


def make_power(host, sensors, reg=None):
    return SensorPower("lab", MeasurementConfig(sensors=[], host=host), reg, sensors=sensors)


def test_sensor_power_attribution_server_host():
    nodes = two_nodes()
    p = make_power("server", [FakeSensor(readings())])
    p.start(nodes)
    p.sample(nodes)
    # 主机部分 = 700 − 400 = 300 W，在两个未关机节点间平分
    assert nodes[0].it_power_w == pytest.approx(400.0) and nodes[1].it_power_w == pytest.approx(300.0)
    assert p.scope == "node" and not nodes[0].power_missing


def test_sensor_power_missing_device_and_host():
    nodes = two_nodes()
    s = FakeSensor(readings())
    p = make_power("server", [s])
    p.start(nodes)
    s.readings = readings(gpu1=None)
    p.sample(nodes)
    # gpu1 缺失：节点 n1 功率缺失；整机减加速卡无法计算，主机部分也缺失；n0 的 GPU 读数仍保留
    assert nodes[0].gpu_power_w == pytest.approx(250.0) and nodes[0].it_power_w is None
    assert nodes[1].detail["power_w"] == [None] and nodes[1].power_missing == ["device:gpu1", "host"]


def test_sensor_power_isolates_failed_sensor():
    nodes = two_nodes()
    reg = CollectorRegistry()
    gpus = FakeSensor(readings()[:2], "gpus")
    meter = FakeSensor(readings()[2:], "meter")
    p = make_power("server", [gpus, meter], reg)
    p.start(nodes)
    meter.fail = TimeoutError("meter unreachable")
    p.sample(nodes)
    # 整机电表故障：只影响主机部分，GPU 读数照常；故障以状态与指标显式报告
    assert nodes[0].gpu_power_w == pytest.approx(250.0) and nodes[0].it_power_w is None
    assert nodes[0].power_missing == ["host"]
    assert sample(reg, "cpc_sensor_up", site="lab", sensor="meter") == 0
    assert sample(reg, "cpc_sensor_up", site="lab", sensor="gpus") == 1
    assert sample(reg, "cpc_sensor_errors_total", site="lab", sensor="meter") == 1
    st = p.status()["sensors"]["meter"]
    assert st["up"] is False and "meter unreachable" in st["last_error"]
    meter.fail = None
    p.sample(nodes)
    assert nodes[0].it_power_w == pytest.approx(400.0) and sample(reg, "cpc_sensor_up", site="lab", sensor="meter") == 1


def test_sensor_power_excluded_scope_and_off_nodes():
    nodes = two_nodes()
    p = make_power("excluded", [FakeSensor(readings()[:2])])
    p.start(nodes)
    nodes[1].state = "off"
    p.sample(nodes)
    assert p.scope == "accelerators"
    assert nodes[0].it_power_w == pytest.approx(250.0) and nodes[1].it_power_w == pytest.approx(150.0)  # 关机后 GPU 仍有实测功率


def test_sensor_power_start_checks():
    nodes = two_nodes()
    s = FakeSensor(readings())
    s.fail = OSError("no device")
    with pytest.raises(SensorError, match="启动检查失败"):
        make_power("server", [s]).start(nodes)
    with pytest.raises(SensorError, match="没有读数"):
        make_power("server", [FakeSensor(readings()[:1] + readings()[2:])]).start(nodes)  # 缺 gpu1
    with pytest.raises(SensorError, match="cpu"):
        make_power("cpu_dram", [FakeSensor(readings())]).start(nodes)  # 没有 CPU / 内存读数
    dup = [FakeSensor(readings()[:2], "a"), FakeSensor(readings()[:1], "b")]
    with pytest.raises(SensorError, match="同时出现"):
        make_power("excluded", dup).start(nodes)


def test_read_all_marks_field_errors():
    board = HealthBoard("sensor", None)
    s = FakeSensor([Reading("fake", "gpu0", "gpu", power_w=None, errors={"power_w": "GPU is lost"})])
    rs = read_all([s], board)
    assert len(rs) == 1 and board.items["fake"].up is False and "GPU is lost" in board.items["fake"].last_error


def test_cli_lists_sensor_types(capsys):
    from cpc.cli import main as cli_main
    from cpc.sched import ROUTING
    from cpc.sensors import SENSORS

    cli_main(["sensors", "--list"])
    out = capsys.readouterr().out
    assert "nvml" in out and "rapl" in out and "ipmi" in out and "command" in out
    names = set(SENSORS.all())
    assert {"nvml", "rapl", "ipmi", "command"} <= names and not {"nvidia-smi", "redfish", "http-json", "hwmon"} & names
    assert "cost_aware" in ROUTING.names()


def test_cpu_only_server_power_is_host_part():
    """未声明加速卡的真实服务器（只用 CPU 的推理实例）：服务器功率 = 主机部分；RAPL 读数缺失时功率缺失。"""
    prof = NodeProfile(slots=8, power_w=[(0, 100), (1, 400)], tpot_s=[(1, 0.02)], prefill_tokens_per_s=1e4, boot_s=0)
    node = Server(id="c0", pool="p", profile=prof, slots=8, model_power=False, state="on",
                  upstream=UpstreamConfig(id="c0", url="http://x"))
    rd = [Reading("fake", "package-0", "cpu", power_w=90.0), Reading("fake", "dram-0", "dram", power_w=10.0)]
    s = FakeSensor(rd)
    p = make_power("cpu_dram", [s])
    p.start([node])
    p.sample([node])
    assert node.it_power_w == pytest.approx(100.0) and node.gpu_power_w is None and not node.power_missing
    s.readings = [rd[0], Reading("fake", "dram-0", "dram", power_w=None)]
    p.sample([node])
    assert node.it_power_w is None and node.power_missing == ["host"]


def test_rapl_reads_nested_powercap_layout(tmp_path):
    """/sys/devices/virtual/powercap 的层级布局（容器内挂载到其他路径时使用）。"""
    top = tmp_path / "intel-rapl" / "intel-rapl:0"
    write(top / "name", "package-0")
    write(top / "energy_uj", "1000")
    write(top / "intel-rapl:0:1" / "name", "dram")
    write(top / "intel-rapl:0:1" / "energy_uj", "10")
    write(top / "intel-rapl:0:0" / "name", "core")
    write(top / "intel-rapl:0:0" / "energy_uj", "1")
    s = RaplSensor(root=str(tmp_path))
    assert sorted(s.capabilities()) == ["dram-0", "package-0"]


def test_max_w_marks_implausible_reading_missing():
    """超过 max_w 的读数（如 BMC 偶发的 16462 W）记为缺失：不积分，原因记入读数错误与健康状态。"""
    from cpc.health import HealthBoard
    from cpc.sensors.exporter import read_all

    class Bmc(FakeSensor):
        def __init__(self, readings):
            super().__init__(readings, sid="bmc")
            self.max_w = 2000.0

    s = Bmc([Reading("bmc", "server", "server", power_w=16462.0)])
    board = HealthBoard("sensor", None)
    board.register(s.id)
    r = read_all([s], board)[0]
    assert r.power_w is None and "超出上限" in r.errors["power_w"] and not board.items["bmc"].up
    s.readings = [Reading("bmc", "server", "server", power_w=98.0)]
    assert read_all([s], board)[0].power_w == 98.0 and board.items["bmc"].up
    with pytest.raises(SensorError, match="max_w"):
        make_sensor({"type": "rapl", "max_w": 0})
    with pytest.raises(SensorError, match="是否要写 max_w"):
        make_sensor({"type": "ipmi", "max_W": 2000})  # 拼写错误的参数在构造时报错，而不是被忽略


def test_power_cap_checked_after_sensor_start():
    """开启功率封顶的站点能够启动（此前在传感器启动前查询设备映射而失败），封顶比例下发到管理该加速卡的传感器。"""
    import asyncio

    from cpc.site.executors import RealNodeHooks

    class CapSensor(FakeSensor):
        supports_power_limit = True

        def __init__(self, readings):
            super().__init__(readings)
            self.limits = []

        def set_power_limit(self, device, ratio):
            self.limits.append((device, ratio))

    up = UpstreamConfig(id="n0", url="http://127.0.0.1:9", gpu_indices=[0])
    prof = NodeProfile(slots=8, power_w=[(0, 100), (1, 400)], tpot_s=[(1, 0.02)], prefill_tokens_per_s=1e4, boot_s=0)
    node = Server(id="n0", pool="p", profile=prof, slots=8, model_power=False, state="on", upstream=up)
    sensor = CapSensor([Reading("fake", "gpu0", "gpu", "nvidia", power_w=150.0)])
    p = make_power("excluded", [sensor])
    hooks = RealNodeHooks([up], power=p, enable_power_cap=True)   # 构造时不查询设备映射
    p.start([node])
    hooks.check_power_cap([up])
    asyncio.run(hooks.set_cap(node, 0.7))
    assert sensor.limits == [("gpu0", 0.7)]
