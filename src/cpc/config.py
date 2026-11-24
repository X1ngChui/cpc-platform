"""场景配置（YAML）与服务地址配置。

场景描述实验对象，与运行方式（仿真或实验）和部署方式无关：
    sources          用户请求的来源区域
    node_profiles    服务器类型：功率与性能模型参数
    sites            站点（数据中心）：位置、PUE、电网区域、服务器池（仿真服务器或真实推理实例）与站内调度策略
    network          来源区域到站点的往返时延模型
    grid             电网信号：电价、碳强度与扩展信号的数据集，需求响应事件
    workload         负载数据集与显式变换
    slo, scheduler   服务目标；跨站点调度策略与运行参数

地址文件（endpoints）描述实验运行时各服务的地址。配置错误在加载阶段报错（ConfigError）；
相对路径相对于场景文件所在目录解析，路径中可使用 ${CPC_DATA_DIR}（未设置时为 ~/.cache/cpc/datasets）与其他环境变量。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CST = timezone(timedelta(hours=8))


class ConfigError(Exception):
    """配置或运行环境错误：在启动阶段报告并退出。"""


class _Strict(BaseModel):
    """拒绝未知字段：拼写错误或已废弃的字段在加载时报错，而不是被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class PolicyConfig(_Strict):
    policy: str
    params: dict[str, Any] = Field(default_factory=dict)


Curve = list[tuple[float, float]]


class NodeProfile(_Strict):
    """服务器类型：功率与性能模型参数（参数含义见《平台设计说明书》"服务器模型"一节）。

    在服务时的单卡功率与 token 间隔有两种给法：参数式（gpu_active_min_w、gpu_max_w、power_exponent；
    decode_s_per_token、decode_slowdown）或查表式（gpu_power_curve、tpot_curve：占用率 → 值的分段线性曲线，
    通常由 power_profile 数据集的测量直接得到，见 cpc data calibrate --curves）。给出曲线时以曲线为准。
    """

    gpus: int = 8
    gpu_model: str = "A100-SXM4-80GB"
    gpu_idle_w: float = 60.0  # 单卡空载功率（没有在服务的请求）
    gpu_active_min_w: float | None = None  # 单卡在服务时的功率下限；None 表示取 gpu_idle_w
    gpu_max_w: float = 400.0  # 单卡满载功率
    host_idle_w: float = 800.0  # 主机部分（CPU、内存、风扇等）空载功率
    host_max_w: float = 1500.0  # 主机部分满载功率
    standby_w: float = 0.0  # 关机状态功率
    power_exponent: float = 0.7  # GPU 动态功率 ∝ 占用率^α
    slots: int = 16  # 并发序列数上限（连续批处理的批大小上限）
    prefill_tokens_per_s: float = 20000.0  # 预填充吞吐
    decode_s_per_token: float = 0.025  # 低负载时的 token 间隔
    decode_slowdown: float = 0.6  # 满批时 token 间隔相对低负载的增加比例
    cap_min_ratio: float = 0.5  # 功率封顶比例下限
    cap_perf_exponent: float = 0.4  # 速度 ∝ 封顶比例^β
    boot_s: float = 60.0  # 开机到可服务的时间（秒）
    gpu_power_curve: Curve | None = None  # [(占用率, 单卡功率 W)]，占用率在 (0, 1] 内递增
    tpot_curve: Curve | None = None  # [(占用率, token 间隔 s)]

    @model_validator(mode="after")
    def _curves(self) -> "NodeProfile":
        for name in ("gpu_power_curve", "tpot_curve"):
            c = getattr(self, name)
            if c is None:
                continue
            xs = [x for x, _ in c]
            if len(c) < 2 or xs != sorted(set(xs)) or xs[0] <= 0 or xs[-1] > 1 or any(y <= 0 for _, y in c):
                raise ValueError(f"{name} 须至少 2 个点，占用率在 (0, 1] 内严格递增，取值为正")
        return self


class SourceRegion(_Strict):
    """用户请求的来源区域。"""

    id: str
    name: str
    lat: float
    lon: float
    share: float = 1.0  # 请求量占比（归一化）


class UpstreamConfig(_Strict):
    """真实推理实例（如一个 vLLM 进程），对应站点中的一台服务器。"""

    id: str
    url: str
    gpu_indices: list[int] = Field(default_factory=list)  # 占用的本机 NVIDIA GPU 序号（等价于 devices: [gpu<i>]）
    devices: list[str] = Field(default_factory=list)  # 占用的加速卡在传感器层中的设备名
    container: str | None = None  # 填写时通过 Docker 启停该容器；不填写为逻辑开关（只停止派发）
    max_inflight: int = 16  # 并发上限（相当于 slots）
    ready_path: str = "/health"

    @model_validator(mode="after")
    def _devices(self) -> "UpstreamConfig":
        if self.gpu_indices and self.devices:
            raise ValueError(f"upstream {self.id}：gpu_indices 与 devices 只能填写其一")
        return self

    @property
    def device_names(self) -> list[str]:
        return list(self.devices) if self.devices else [f"gpu{i}" for i in self.gpu_indices]


class MeasurementConfig(_Strict):
    """站点中真实服务器的功率测量（站点代理读取的传感器）。

    sensors：传感器列表，例如 [{type: nvml}, {type: ipmi, name: bmc, host: 10.0.0.5}]；同类型多个实例用 name 区分。
    host：   主机部分功率的确定方式：server = 整机读数 − 加速卡读数；cpu_dram = CPU 与内存读数之和；
             excluded = 不计主机部分（服务器 IT 功率只含加速卡）。
    """

    sensors: list[dict[str, Any]]
    host: Literal["server", "cpu_dram", "excluded"]

    @model_validator(mode="after")
    def _names(self) -> "MeasurementConfig":
        names = []
        for s in self.sensors:
            if "type" not in s:
                raise ValueError(f"传感器配置缺少 type：{s}")
            names.append(s.get("name") or s["type"])
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"传感器名称重复：{dup}（同类型的多个传感器请用 name 区分）")
        return self


class PoolConfig(_Strict):
    """服务器池：同一类型的一组服务器。

    backend: emulated  由服务器模型仿真，数量为 count；
    backend: openai    每个 upstream 为一台真实服务器（OpenAI 兼容推理实例），功率由站点 measurement 中的传感器测量。
    在仿真中，真实服务器池按 profile 的模型仿真，服务器数为 upstreams 的数目。
    """

    id: str
    profile: str
    backend: Literal["emulated", "openai"] = "emulated"
    count: int = 0
    upstreams: list[UpstreamConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _backend(self) -> "PoolConfig":
        if self.backend == "emulated":
            if self.count < 1 or self.upstreams:
                raise ValueError(f"服务器池 {self.id}：仿真服务器池须给出 count ≥ 1，且不配置 upstreams")
        else:
            if not self.upstreams or self.count:
                raise ValueError(f"服务器池 {self.id}：真实服务器池须给出 upstreams，且不配置 count")
            for u in self.upstreams:
                if not u.device_names:
                    raise ValueError(f"服务器池 {self.id} 的 upstream {u.id} 未声明占用的加速卡（gpu_indices 或 devices）")
        return self

    @property
    def size(self) -> int:
        return self.count if self.backend == "emulated" else len(self.upstreams)


class SiteScheduler(_Strict):
    """站内调度：派发（请求 → 服务器）与电源管理（服务器开关）。"""

    dispatch: PolicyConfig = PolicyConfig(policy="least_loaded")
    power: PolicyConfig = PolicyConfig(policy="ordered")


class SiteConfig(_Strict):
    """站点（数据中心）。"""

    id: str
    name: str
    region: str  # 电网区域：电网信号数据集中的缺省区域键
    lat: float
    lon: float
    pue: float = 1.3
    pools: list[PoolConfig]
    measurement: MeasurementConfig | None = None  # 有真实服务器池时必填
    min_active: int = 1
    grid_cap_kw: float | None = None  # 并网（设施）功率上限
    scheduler: SiteScheduler = SiteScheduler()

    @property
    def size(self) -> int:
        return sum(p.size for p in self.pools)

    @property
    def real(self) -> bool:
        return any(p.backend == "openai" for p in self.pools)


class ClockConfig(_Strict):
    """信号时钟：信号时间 = start + 经过时间 × speedup（仿真中 speedup 不起作用）。"""

    start: datetime = datetime(2026, 7, 15, 0, 0, tzinfo=CST)
    speedup: float = 1.0

    @model_validator(mode="after")
    def _tz(self) -> "ClockConfig":
        if self.start.tzinfo is None:
            raise ValueError("clock.start 须带时区")
        return self


class NetworkConfig(_Strict):
    """广域网时延模型：RTT = base_ms + 2 × 距离 × route_factor / km_per_ms；overrides 给出实测值。"""

    base_ms: float = 4.0
    route_factor: float = 1.5
    km_per_ms: float = 200.0
    emulate_wan: bool = True  # 实验中网关是否按 RTT 注入时延
    overrides: dict[str, dict[str, float]] = Field(default_factory=dict)  # {source: {site: ms}}
    default_source: str | None = None  # 未携带 x-cpc-source 请求头的请求所归属的来源区域


class DREvent(_Strict):
    """需求响应事件：时段内站点设施功率不得超过 cap_kw。"""

    site: str
    start: datetime
    end: datetime
    cap_kw: float
    kind: str = "peak_shaving"

    @model_validator(mode="after")
    def _tz(self) -> "DREvent":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("需求响应事件的 start、end 须带时区")
        if self.end <= self.start:
            raise ValueError(f"需求响应事件的结束时间须晚于开始时间：{self.start} – {self.end}")
        return self


class SignalSource(_Strict):
    """一种电网信号的来源：grid_signal 数据集、信号名与站点到区域的对应（缺省为站点的 region）。"""

    dataset: str
    signal: str
    regions: dict[str, str] = Field(default_factory=dict)


class ExtraSignal(SignalSource):
    name: str  # 在 GridSignal.extra 中的键


class GridConfig(_Strict):
    price: SignalSource
    carbon: SignalSource
    extras: list[ExtraSignal] = Field(default_factory=list)  # 扩展信号，原样传给策略
    dr_events: list[DREvent] = Field(default_factory=list)


class WorkloadConfig(_Strict):
    """负载：workload 数据集、要传给策略的扩展字段与显式变换（时间窗为原始 trace 秒）。"""

    dataset: str
    attributes: list[str] = Field(default_factory=list)
    start_s: float = Field(0.0, ge=0)
    duration_s: float | None = Field(None, gt=0)
    time_scale: float = Field(1.0, gt=0)
    rate_scale: float = Field(1.0, gt=0)
    seed: int = 1


class SLOConfig(_Strict):
    ttft_s: float = 2.0
    tpot_s: float = 0.15


RetryCause = Literal["overloaded", "connect_error"]


class RetryConfig(_Strict):
    """网关转发失败时的重试：可重试类型 overloaded（站点 503）、connect_error（连接建立失败）；至多 max_attempts 个站点。"""

    max_attempts: int = Field(3, ge=1)
    retry_on: list[RetryCause] = Field(default_factory=lambda: ["overloaded", "connect_error"])


class SchedulerConfig(_Strict):
    """跨站点调度与运行参数。"""

    routing: PolicyConfig = PolicyConfig(policy="nearest")
    capacity: PolicyConfig = PolicyConfig(policy="static")
    control_interval_s: float = 15.0  # 实验中的容量控制周期
    sim_control_interval_s: float = 300.0  # 仿真中的容量控制周期
    carbon_price_per_kg: float = 0.1  # 碳价（与电价同一货币单位 / kgCO2）
    queue_limit_per_slot: float = 4.0  # 站点排队上限 = max(在线并发槽数, 最大服务器并发槽数) × 该系数
    rate_window_s: float = Field(60.0, gt=0)  # 站点到达率估计（指数加权）的时间常数
    retry: RetryConfig = RetryConfig()
    state_refresh_s: float = 1.0
    state_stale_s: float = 5.0


class Scenario(_Strict):
    name: str = "scenario"
    clock: ClockConfig = ClockConfig()
    sources: list[SourceRegion]
    node_profiles: dict[str, NodeProfile]
    sites: list[SiteConfig]
    network: NetworkConfig = NetworkConfig()
    grid: GridConfig
    workload: WorkloadConfig
    slo: SLOConfig = SLOConfig()
    scheduler: SchedulerConfig = SchedulerConfig()

    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "Scenario":
        ids = [s.id for s in self.sites]
        if len(set(ids)) != len(ids):
            raise ValueError("站点 id 重复")
        src_ids = {s.id for s in self.sources}
        if len(src_ids) != len(self.sources):
            raise ValueError("来源区域 id 重复")
        for s in self.sites:
            pids = [p.id for p in s.pools]
            if not pids or len(set(pids)) != len(pids):
                raise ValueError(f"站点 {s.id} 的服务器池须非空且 id 不重复")
            for p in s.pools:
                if p.profile not in self.node_profiles:
                    raise ValueError(f"站点 {s.id} 的服务器池 {p.id} 引用了不存在的 node profile: {p.profile}")
            if s.real != (s.measurement is not None):
                raise ValueError(f"站点 {s.id}：有真实服务器池时须配置 measurement，否则不应配置")
            ups = [u.id for p in s.pools for u in p.upstreams]
            devs = [d for p in s.pools for u in p.upstreams for d in u.device_names]
            if len(set(ups)) != len(ups) or len(set(devs)) != len(devs):
                raise ValueError(f"站点 {s.id} 的 upstream id 或其声明的加速卡重复")
        if self.network.default_source is not None and self.network.default_source not in src_ids:
            raise ValueError(f"network.default_source 引用了不存在的来源区域: {self.network.default_source}")
        for src, row in self.network.overrides.items():
            if src not in src_ids or not set(row) <= set(ids):
                raise ValueError(f"network.overrides 引用了不存在的来源区域或站点: {src} {sorted(row)}")
        for e in self.grid.dr_events:
            if e.site not in ids:
                raise ValueError(f"需求响应事件引用了不存在的站点: {e.site}")
        for name, src in [("grid.price", self.grid.price), ("grid.carbon", self.grid.carbon)] + [
                (f"grid.extras[{x.name}]", x) for x in self.grid.extras]:
            extra = sorted(set(src.regions) - set(ids))
            if extra:
                raise ValueError(f"{name}.regions 引用了不存在的站点: {extra}")
        names = [x.name for x in self.grid.extras]
        if len(set(names)) != len(names):
            raise ValueError("grid.extras 的 name 重复")
        return self

    def site(self, site_id: str) -> SiteConfig:
        for s in self.sites:
            if s.id == site_id:
                return s
        raise KeyError(site_id)

    def region(self, site_id: str, src: SignalSource) -> str:
        return src.regions.get(site_id) or self.site(site_id).region


def load_scenario(path: str | os.PathLike) -> Scenario:
    path = Path(path).resolve()
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"无法读取场景文件 {path}：{e}") from e
    try:
        return Scenario.model_validate({**(raw or {}), "base_dir": path.parent})
    except ValidationError as e:
        raise ConfigError(f"场景文件 {path} 无效：\n{e}") from e


class Endpoints(_Strict):
    """各服务的访问地址。"""

    grid: str
    gateway: str
    controller: str
    sites: dict[str, str]


def _host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    u = urlparse(url)
    return (u.hostname or "127.0.0.1", u.port or 80)


def default_local_endpoints(scn: Scenario, host: str = "127.0.0.1") -> Endpoints:
    """本机模式的地址：信号 9000、网关 8080、控制器 9300、站点按场景顺序从 9101 起。"""
    return Endpoints(grid=f"http://{host}:9000", gateway=f"http://{host}:8080", controller=f"http://{host}:9300",
                     sites={s.id: f"http://{host}:{9101 + i}" for i, s in enumerate(scn.sites)})


def load_endpoints(path: str | os.PathLike | None, scn: Scenario, local: bool = False) -> Endpoints:
    """读取地址文件（参数或环境变量 CPC_ENDPOINTS）。local=True 时使用本机模式地址，且不得同时给出地址文件。"""
    path = path or os.environ.get("CPC_ENDPOINTS")
    if local:
        if path:
            raise ConfigError(f"已指定本机模式（--local），不应再给出地址文件 {path}")
        return default_local_endpoints(scn)
    if not path:
        raise ConfigError("未指定服务地址：请用 -e / CPC_ENDPOINTS 给出地址文件，或用 --local 使用本机模式地址")
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        ep = Endpoints.model_validate(raw)
    except (OSError, ValidationError) as e:
        raise ConfigError(f"地址文件 {path} 无效：{e}") from e
    ids = {s.id for s in scn.sites}
    if set(ep.sites) != ids:
        raise ConfigError(f"地址文件 {path} 的站点 {sorted(ep.sites)} 与场景 {sorted(ids)} 不一致")
    return ep
