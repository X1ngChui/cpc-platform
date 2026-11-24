"""场景配置（YAML）与服务地址配置。

场景文件描述"实验世界"：有哪些数据中心站点、用户来源区域、节点功率/性能参数、
电网信号数据集、负载数据集与 SLO、默认调度策略。它与部署方式无关。

地址文件（endpoints）描述"服务在哪里"：电网信号服务、网关、控制器、各站点代理的 URL。
同一场景可用于本机单进程、Docker Compose、Kubernetes 或云服务器部署，只需换地址文件。

配置错误在加载阶段报错（ConfigError）：未知字段、引用不存在的站点/节点配置或数据集、
真实后端站点未声明功率来源等。相对路径一律相对于场景文件所在目录解析。
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


class NodeProfile(_Strict):
    """一台推理服务器（节点）的功率与性能参数。

    默认值是示意参数（8×A100 级服务器），仅用于仿真；接入真实服务器后应以实测标定。
    """

    gpus: int = 8
    gpu_model: str = "A100-SXM4-80GB"
    gpu_idle_w: float = 60.0  # 单卡空载功率（没有在服务的请求）
    gpu_active_min_w: float | None = None  # 单卡在服务时的功率下限（占用率趋于 0 的外推值）；None 表示取 gpu_idle_w
    gpu_max_w: float = 400.0  # 单卡满载功率（TDP）
    host_idle_w: float = 800.0  # 主机（CPU、内存、风扇等）空载功率
    host_max_w: float = 1500.0  # 主机满载功率
    standby_w: float = 0.0  # 关机（或深度休眠）状态功率，例如 BMC
    power_exponent: float = 0.7  # GPU 动态功率 ∝ 占用率^α（α<1 表示随批大小凹增长）
    slots: int = 16  # 并发序列数上限（连续批处理的批大小上限）
    prefill_tokens_per_s: float = 20000.0  # 预填充吞吐
    decode_s_per_token: float = 0.025  # 空载时单序列每 token 解码时间（TPOT）
    decode_slowdown: float = 0.6  # 满批时 TPOT 相对空载的增加比例
    cap_min_ratio: float = 0.5  # 功率封顶比例下限
    cap_perf_exponent: float = 0.4  # 速度 ∝ 封顶比例^β
    boot_s: float = 60.0  # 开机到可服务的时间（秒，真实时间）


class SourceRegion(_Strict):
    """用户请求的来源区域。"""

    id: str
    name: str
    lat: float
    lon: float
    share: float = 1.0  # 请求量占比（会归一化）


class UpstreamConfig(_Strict):
    """真实后端：一个 OpenAI 兼容推理实例（如 vLLM），视为一个"节点"。"""

    id: str
    url: str  # 例如 http://127.0.0.1:8000
    gpu_indices: list[int] = Field(default_factory=list)  # 该实例占用的本机 NVIDIA GPU 序号（等价于 devices: [gpu<i>]）
    devices: list[str] = Field(default_factory=list)  # 该实例占用的加速卡在传感器层中的设备名，如 [gpu0]、[npu0-0]、[amdgpu0]
    container: str | None = None  # 若填写，开关机通过 Docker 启停该容器；不填写则为逻辑开关（只停止派发）
    max_inflight: int = 16  # 该实例的并发上限（相当于 slots）
    ready_path: str = "/health"  # 就绪检查路径（vLLM 提供 /health）

    @model_validator(mode="after")
    def _devices(self) -> "UpstreamConfig":
        if self.gpu_indices and self.devices:
            raise ValueError(f"upstream {self.id}：gpu_indices 与 devices 只能填写其一")
        return self

    @property
    def device_names(self) -> list[str]:
        return list(self.devices) if self.devices else [f"gpu{i}" for i in self.gpu_indices]


class SitePowerConfig(_Strict):
    """真实后端站点的功率来源。

    sensors：传感器列表，例如 [{type: nvml}, {type: ipmi, host: 10.0.0.5, user: admin}]；
             同类型的多个实例需用 name 区分。每个传感器在启动时必须可用，否则站点代理启动失败。
    host：   主机部分（CPU、内存、主板、风扇等）功率的确定方式：
             server   = 整机读数（kind 为 server / platform）− 全部加速卡读数；
             cpu_dram = CPU 与内存读数之和（RAPL 等；不含主板、风扇、电源损耗）；
             excluded = 不计主机部分，节点 IT 功率只含加速卡（快照中 power_scope 标为 accelerators）。
    """

    sensors: list[dict[str, Any]]
    host: Literal["server", "cpu_dram", "excluded"]

    @model_validator(mode="after")
    def _names(self) -> "SitePowerConfig":
        names = []
        for s in self.sensors:
            if "type" not in s:
                raise ValueError(f"传感器配置缺少 type：{s}")
            names.append(s.get("name") or s["type"])
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"传感器名称重复：{dup}（同类型的多个传感器请用 name 区分）")
        return self


class SiteConfig(_Strict):
    """一个（虚拟）数据中心站点。"""

    id: str
    name: str
    province: str  # 对应电价/排放因子数据中的省份键
    lat: float
    lon: float
    pue: float = 1.3
    nodes: int = 4  # 节点数（仿真后端）；真实后端以 upstreams 数量为准
    profile: str = "default"
    min_active: int = 1
    grid_cap_kw: float | None = None  # 并网（设施）功率上限
    backend: Literal["emulated", "openai"] = "emulated"
    upstreams: list[UpstreamConfig] = Field(default_factory=list)
    power: SitePowerConfig | None = None  # 真实后端必填；仿真后端的功率由功率模型给出，不可填写

    @property
    def node_count(self) -> int:
        return len(self.upstreams) if self.backend == "openai" else self.nodes


class ClockConfig(_Strict):
    """信号时钟：信号时间 = start + (真实经过时间) × speedup。"""

    start: datetime = datetime(2026, 7, 15, 0, 0, tzinfo=CST)
    speedup: float = 1.0

    @model_validator(mode="after")
    def _tz(self) -> "ClockConfig":
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=CST)
        return self


class NetworkConfig(_Strict):
    """广域网时延模型：RTT = base_ms + 2 × 距离 × route_factor / km_per_ms。"""

    base_ms: float = 4.0
    route_factor: float = 1.5  # 光纤路由相对大圆距离的绕行系数
    km_per_ms: float = 200.0  # 光在光纤中约 2×10^5 km/s
    emulate_wan: bool = True  # 网关是否按 RTT 注入时延（跨数据中心仿真）
    overrides: dict[str, dict[str, float]] = Field(default_factory=dict)  # 实测 RTT：{source: {site: ms}}
    default_source: str | None = None  # 未携带 x-cpc-source 请求头的请求所归属的来源区域；未配置时此类请求返回 400


class DREvent(_Strict):
    """需求响应（或电网限电）事件：时段内站点设施功率不得超过 cap_kw。"""

    site: str
    start: datetime
    end: datetime
    cap_kw: float
    kind: str = "peak_shaving"

    @model_validator(mode="after")
    def _tz(self) -> "DREvent":
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=CST)
        if self.end.tzinfo is None:
            self.end = self.end.replace(tzinfo=CST)
        if self.end <= self.start:
            raise ValueError(f"需求响应事件的结束时间须晚于开始时间：{self.start} – {self.end}")
        return self


class SignalSource(_Strict):
    """电网信号的数据来源：一个电网信号数据集（见 cpc data list）及其参数。

    regions：站点 id → 数据集中的区域键；未列出的站点使用其 province。
    """

    dataset: str
    params: dict[str, Any] = Field(default_factory=dict)
    regions: dict[str, str] = Field(default_factory=dict)


class GridConfig(_Strict):
    price: SignalSource  # 电价（元/kWh）
    carbon: SignalSource  # 碳强度（kgCO2/kWh）
    dr_events: list[DREvent] = Field(default_factory=list)


class WorkloadConfig(_Strict):
    """负载：一个负载数据集（见 cpc data list）及显式变换。

    start_s、duration_s 为原始 trace 时间（秒）上的时间窗；time_scale > 1 表示时间压缩；
    rate_scale 为到达率缩放（≤ 1 为稀疏化，> 1 为多个稀疏化副本叠加）；seed 用于缩放与来源区域分配。
    变换后的 t = 0 对应场景时钟起点 clock.start。
    """

    dataset: str
    params: dict[str, Any] = Field(default_factory=dict)
    start_s: float = Field(0.0, ge=0)
    duration_s: float | None = Field(None, gt=0)
    time_scale: float = Field(1.0, gt=0)
    rate_scale: float = Field(1.0, gt=0)
    seed: int = 1


class SLOConfig(_Strict):
    ttft_s: float = 2.0  # 首 token 时延
    tpot_s: float = 0.15  # 每输出 token 时延


class PolicyConfig(_Strict):
    policy: str
    params: dict[str, Any] = Field(default_factory=dict)


RetryCause = Literal["overloaded", "connect_error"]


class RetryConfig(_Strict):
    """网关转发失败时的重试规则。

    max_attempts：每个请求最多尝试的站点数（含首次）；
    retry_on：可重试的失败类型。overloaded = 站点返回 503（排队已满）；connect_error = 连接建立失败
              （请求未送达站点）。请求已送达后的读取失败、超时与其他错误不重试，直接返回 502。
    """

    max_attempts: int = Field(3, ge=1)
    retry_on: list[RetryCause] = Field(default_factory=lambda: ["overloaded", "connect_error"])


class SchedulerConfig(_Strict):
    routing: PolicyConfig = PolicyConfig(policy="nearest")
    capacity: PolicyConfig = PolicyConfig(policy="static")
    control_interval_s: float = 15.0  # 在线平台的容量控制周期（真实秒）
    sim_control_interval_s: float = 300.0  # 离散事件仿真中的容量控制周期（仿真秒）
    carbon_price_yuan_per_kg: float = 0.1  # 碳价（元/kgCO2），用于把碳排放折算为成本
    queue_limit_per_slot: float = 4.0  # 站点排队上限 = 在线并发槽数 × 该系数
    retry: RetryConfig = RetryConfig()
    state_refresh_s: float = 1.0  # 网关拉取站点状态的周期
    state_stale_s: float = 5.0  # 站点状态或电网信号超过该时长未成功刷新即视为不可用


class Scenario(_Strict):
    name: str = "scenario"
    clock: ClockConfig = ClockConfig()
    sources: list[SourceRegion]
    sites: list[SiteConfig]
    node_profiles: dict[str, NodeProfile] = Field(default_factory=lambda: {"default": NodeProfile()})
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
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("来源区域 id 重复")
        for s in self.sites:
            if s.profile not in self.node_profiles:
                raise ValueError(f"站点 {s.id} 引用了不存在的 node profile: {s.profile}")
            if s.backend == "openai":
                if not s.upstreams:
                    raise ValueError(f"站点 {s.id} 使用 openai 后端但未配置 upstreams")
                if s.power is None:
                    raise ValueError(f"站点 {s.id} 使用 openai 后端，须在 power 中声明功率传感器与主机功率的确定方式")
                for u in s.upstreams:
                    if not u.device_names:
                        raise ValueError(f"站点 {s.id} 的 upstream {u.id} 未声明占用的加速卡（gpu_indices 或 devices）")
                devs = [d for u in s.upstreams for d in u.device_names]
                if len(set(devs)) != len(devs):
                    raise ValueError(f"站点 {s.id} 的多个 upstream 声明了同一加速卡：{devs}")
            elif s.power is not None or s.upstreams:
                raise ValueError(f"站点 {s.id} 为仿真后端，功率由功率模型给出，不应配置 power 或 upstreams")
        src_ids = {s.id for s in self.sources}
        if self.network.default_source is not None and self.network.default_source not in src_ids:
            raise ValueError(f"network.default_source 引用了不存在的来源区域: {self.network.default_source}")
        for src, row in self.network.overrides.items():
            if src not in src_ids or not set(row) <= set(ids):
                raise ValueError(f"network.overrides 引用了不存在的来源区域或站点: {src} {sorted(row)}")
        for e in self.grid.dr_events:
            if e.site not in ids:
                raise ValueError(f"需求响应事件引用了不存在的站点: {e.site}")
        for name, src in (("grid.price", self.grid.price), ("grid.carbon", self.grid.carbon)):
            extra = sorted(set(src.regions) - set(ids))
            if extra:
                raise ValueError(f"{name}.regions 引用了不存在的站点: {extra}")
        return self

    def site(self, site_id: str) -> SiteConfig:
        for s in self.sites:
            if s.id == site_id:
                return s
        raise KeyError(site_id)

    def profile_of(self, site_id: str) -> NodeProfile:
        return self.node_profiles[self.site(site_id).profile]

    def resolve(self, path: str | None) -> Path | None:
        """相对路径相对于场景文件所在目录解析。"""
        if path is None:
            return None
        p = Path(path)
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    def region(self, site_id: str, quantity: str) -> str:
        """站点在电价（price）或碳强度（carbon）数据集中的区域键。"""
        src = self.grid.price if quantity == "price" else self.grid.carbon
        return src.regions.get(site_id) or self.site(site_id).province


def load_scenario(path: str | os.PathLike) -> Scenario:
    path = Path(path).resolve()
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"无法读取场景文件 {path}：{e}") from e
    try:
        scn = Scenario.model_validate({**(raw or {}), "base_dir": path.parent})
    except ValidationError as e:
        raise ConfigError(f"场景文件 {path} 无效：\n{e}") from e
    from .datasets import all_datasets

    known = all_datasets()
    for field_name, name, kind in (("workload.dataset", scn.workload.dataset, "workload"),
                                   ("grid.price.dataset", scn.grid.price.dataset, "grid_signal"),
                                   ("grid.carbon.dataset", scn.grid.carbon.dataset, "grid_signal")):
        if name not in known:
            raise ConfigError(f"场景文件 {path} 的 {field_name} 引用了未知数据集 {name}，可选：{sorted(known)}")
        if known[name].manifest.kind != kind:
            raise ConfigError(f"场景文件 {path} 的 {field_name} 须为 {kind} 类数据集，{name} 为 {known[name].manifest.kind}")
    return scn


class Endpoints(_Strict):
    """各服务的访问地址。"""

    grid: str
    gateway: str
    controller: str
    sites: dict[str, str]

    def site_bind(self, site_id: str) -> tuple[str, int]:
        return _host_port(self.sites[site_id])


def _host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse

    u = urlparse(url)
    return (u.hostname or "127.0.0.1", u.port or 80)


def default_local_endpoints(scn: Scenario, host: str = "127.0.0.1") -> Endpoints:
    """本机模式的地址：信号 9000、网关 8080、控制器 9300、站点按场景顺序从 9101 起。"""
    return Endpoints(
        grid=f"http://{host}:9000",
        gateway=f"http://{host}:8080",
        controller=f"http://{host}:9300",
        sites={s.id: f"http://{host}:{9101 + i}" for i, s in enumerate(scn.sites)},
    )


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
    missing = [s.id for s in scn.sites if s.id not in ep.sites]
    if missing:
        raise ConfigError(f"地址文件 {path} 缺少站点: {missing}")
    extra = sorted(set(ep.sites) - {s.id for s in scn.sites})
    if extra:
        raise ConfigError(f"地址文件 {path} 含场景中不存在的站点: {extra}")
    return ep
