"""Sensor layer: parsing and fault handling against fake sysfs trees and command output (no real hardware)."""

from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry

from cpc.config import ConfigError, MeasurementConfig, NodeProfile, UpstreamConfig
from cpc.datacenter import Server
from cpc.health import HealthBoard
from cpc.sensors import EnergyIntegrator, Reading, Sensor, SensorError, make_sensor, read_all
from cpc.sensors.builtin import CommandSensor, IpmiSensor, RaplSensor
from cpc.site.power import SensorPower


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def sample(reg: CollectorRegistry, name: str, **labels) -> float | None:
    return reg.get_sample_value(name, labels)


# ------------------------------------------------------------------ parsing and capability probing


def test_rapl_counter_wraparound_and_reset(tmp_path, monkeypatch):
    # directory layout of the kernel powercap: domains intel-rapl:N, subdomains intel-rapl:N:M
    z = tmp_path / "intel-rapl:0"
    write(z / "name", "package-0")
    write(z / "max_energy_range_uj", "1000000")
    write(z / "energy_uj", "900000")
    write(tmp_path / "intel-rapl:0:0" / "name", "core")  # subdomain, not counted twice
    write(tmp_path / "intel-rapl:0:0" / "energy_uj", "1")
    d = tmp_path / "intel-rapl:0:2"
    write(d / "name", "dram")
    write(d / "energy_uj", "0")
    s = RaplSensor(root=str(tmp_path))
    clock = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr("cpc.sensors.builtin.time.monotonic", lambda: next(clock))
    first = {r.device: r for r in s.read()}
    assert set(first) == {"package-0", "dram-0"}
    assert first["package-0"].power_w is None and not first["package-0"].errors  # no predecessor yet: not an error
    write(z / "energy_uj", "100000")  # wrap-around: 900000 -> 1000000 -> 100000, increment 200000 uJ
    write(d / "energy_uj", "5000000")
    second = {r.device: r for r in s.read()}
    assert second["package-0"].power_w == pytest.approx(0.2)
    assert second["package-0"].energy_j == pytest.approx(0.2)
    assert second["dram-0"].kind == "dram" and second["dram-0"].power_w == pytest.approx(5.0)
    write(d / "energy_uj", "10")  # dram has no range: a drop is a reset, power is missing rather than 0
    third = {r.device: r for r in s.read()}
    assert third["dram-0"].power_w is None and "energy_j" in third["dram-0"].errors
    assert third["dram-0"].energy_j == pytest.approx(5.0)  # the total does not grow


def test_rapl_unreadable_fails_at_construction(tmp_path):
    with pytest.raises(SensorError):
        RaplSensor(root=str(tmp_path))  # no domain at all


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
    with pytest.raises(ConfigError, match="unknown sensor type"):
        make_sensor({"type": "no-such-sensor"})
    with pytest.raises(SensorError, match="failed to initialise"):
        make_sensor({"type": "rapl", "root": "/nonexistent"})


# ------------------------------------------------------------------ energy integration and completeness


def test_integrator_skips_missing_intervals():
    integ = EnergyIntegrator()
    key = ("x", "gpu0")
    integ.update([Reading("x", "gpu0", "gpu", power_w=100.0)], now=0.0, expected={key})
    r1 = [Reading("x", "gpu0", "gpu", power_w=None)]  # this sample has no power
    integ.update(r1, now=2.0, expected={key})
    assert r1[0].energy_j == pytest.approx(200.0)  # [0, 2) is covered by the sample at t=0
    integ.update([], now=5.0, expected={key})  # reading absent: [2, 5) is not integrated
    r3 = [Reading("x", "gpu0", "gpu", power_w=50.0)]
    integ.update(r3, now=6.0, expected={key})
    assert r3[0].energy_j == pytest.approx(200.0)  # the left end of [5, 6) is still missing
    assert integ.measured_s[key] == pytest.approx(2.0) and integ.missing_s[key] == pytest.approx(4.0)
    assert integ.coverage(key) == pytest.approx(1 / 3)


def test_integrator_counter_reset():
    integ = EnergyIntegrator()
    integ.update([Reading("x", "gpu0", "gpu", power_w=1.0, energy_j=1000.0)], now=0.0)
    r = [Reading("x", "gpu0", "gpu", power_w=1.0, energy_j=10.0)]  # driver reloaded, counter restarts
    integ.update(r, now=1.0)
    assert r[0].energy_j == pytest.approx(1010.0) and integ.resets[("x", "gpu0")] == 1


# ------------------------------------------------------------------ attribution to servers and fault isolation


class FakeSensor(Sensor):
    """Sensor with injectable faults: readings are the current readings; read() raises fail when set."""

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
    # host part = 700 - 400 = 300 W, shared by the two servers that are not off
    assert nodes[0].it_power_w == pytest.approx(400.0) and nodes[1].it_power_w == pytest.approx(300.0)
    assert p.scope == "node" and not nodes[0].power_missing


def test_sensor_power_missing_device_and_host():
    nodes = two_nodes()
    s = FakeSensor(readings())
    p = make_power("server", [s])
    p.start(nodes)
    s.readings = readings(gpu1=None)
    p.sample(nodes)
    # gpu1 missing: n1 has no power; whole server minus accelerators is unknown, so the host part is missing
    # too; the GPU reading of n0 stays
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
    # whole-server meter failed: only the host part is affected, GPU readings continue; the fault is explicit
    # in state and metrics
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
    assert nodes[0].it_power_w == pytest.approx(250.0) and nodes[1].it_power_w == pytest.approx(150.0)  # GPUs draw power when off


def test_sensor_power_start_checks():
    nodes = two_nodes()
    s = FakeSensor(readings())
    s.fail = OSError("no device")
    with pytest.raises(SensorError, match="failed the startup check"):
        make_power("server", [s]).start(nodes)
    with pytest.raises(SensorError, match="have no reading"):
        make_power("server", [FakeSensor(readings()[:1] + readings()[2:])]).start(nodes)  # gpu1 missing
    with pytest.raises(SensorError, match="cpu"):
        make_power("cpu_dram", [FakeSensor(readings())]).start(nodes)  # no CPU or memory readings
    dup = [FakeSensor(readings()[:2], "a"), FakeSensor(readings()[:1], "b")]
    with pytest.raises(SensorError, match="reported by both"):
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
    """A real server without accelerators (CPU-only inference): server power = host part; missing when RAPL is missing."""
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
    """Nested layout of /sys/devices/virtual/powercap (used when mounted elsewhere inside a container)."""
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
    """Readings above max_w (e.g. a sporadic 16462 W from a BMC) count as missing: not integrated, with the
    reason in the reading errors and the sensor health."""
    class Bmc(FakeSensor):
        def __init__(self, readings):
            super().__init__(readings, sid="bmc")
            self.max_w = 2000.0

    s = Bmc([Reading("bmc", "server", "server", power_w=16462.0)])
    board = HealthBoard("sensor", None)
    board.register(s.id)
    r = read_all([s], board)[0]
    assert r.power_w is None and "exceeds max_w" in r.errors["power_w"] and not board.items["bmc"].up
    s.readings = [Reading("bmc", "server", "server", power_w=98.0)]
    assert read_all([s], board)[0].power_w == 98.0 and board.items["bmc"].up
    with pytest.raises(SensorError, match="max_w"):
        make_sensor({"type": "rapl", "max_w": 0})
    with pytest.raises(SensorError, match="did you mean 'max_w'"):
        make_sensor({"type": "ipmi", "max_W": 2000})  # misspelt parameters fail at construction instead of being ignored


def test_power_cap_checked_after_sensor_start():
    """A site with power capping starts (the device map is queried after the sensors started), and cap ratios
    reach the sensor that manages the accelerator."""
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
    hooks = RealNodeHooks([up], power=p, enable_power_cap=True)  # the constructor does not query the device map
    p.start([node])
    hooks.check_power_cap([up])
    asyncio.run(hooks.set_cap(node, 0.7))
    assert sensor.limits == [("gpu0", 0.7)]
