"""场景配置（YAML）与服务地址配置。

场景文件描述"实验世界"：有哪些数据中心站点、用户来源区域、节点功率/性能参数、
电网信号数据、负载与 SLO、默认调度策略。它与部署方式无关。

地址文件（endpoints）描述"服务在哪里"：电网信号服务、网关、控制器、各站点代理的 URL。
同一场景可用于本机单进程、Docker Compose、Kubernetes 或云服务器部署，只需换地址文件。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

CST = timezone(timedelta(hours=8))


class NodeProfile(BaseModel):
    """一台推理服务器（节点）的功率与性能参数。

    默认值是示意参数（8×A100 级服务器），仅用于仿真；接入真实服务器后应以实测标定。
    """

    gpus: int = 8
    gpu_model: str = "A100-SXM4-80GB"
    gpu_idle_w: float = 60.0  # 单卡空载功率
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


class SourceRegion(BaseModel):
    """用户请求的来源区域。"""

    id: str
    name: str
    lat: float
    lon: float
    share: float = 1.0  # 请求量占比（会归一化）


class UpstreamConfig(BaseModel):
    """真实后端：一个 OpenAI 兼容推理实例（如 vLLM），视为一个"节点"。"""

    id: str
    url: str  # 例如 http://127.0.0.1:8000
    gpu_indices: list[int] = Field(default_factory=list)  # 该实例占用的本机 NVIDIA GPU 序号（等价于 devices: [gpu<i>]）
    devices: list[str] = Field(default_factory=list)  # 该实例占用的加速卡在传感器层中的设备名，如 [gpu0]、[npu0-0]、[amdgpu0]
    container: str | None = None  # 若填写，开关机通过 Docker 启停该容器
    max_inflight: int = 16  # 该实例的并发上限（相当于 slots）


class SiteConfig(BaseModel):
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
    carbon_amplitude: float = 0.1  # 合成碳强度日内波动幅度（仅 carbon_profile=synthetic 时使用）
    # 真实后端的功率传感器列表，例如 [{type: nvml}, {type: rapl}, {type: ipmi}]；不填则自动探测本机可用传感器
    sensors: list[dict[str, Any]] | None = None

    @property
    def node_count(self) -> int:
        return len(self.upstreams) if self.backend == "openai" else self.nodes


class ClockConfig(BaseModel):
    """信号时钟：信号时间 = start + (真实经过时间) × speedup。"""

    start: datetime = datetime(2026, 7, 15, 0, 0, tzinfo=CST)
    speedup: float = 1.0

    @model_validator(mode="after")
    def _tz(self) -> "ClockConfig":
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=CST)
        return self


class NetworkConfig(BaseModel):
    """广域网时延模型：RTT = base_ms + 2 × 距离 × route_factor / km_per_ms。"""

    base_ms: float = 4.0
    route_factor: float = 1.5  # 光纤路由相对大圆距离的绕行系数
    km_per_ms: float = 200.0  # 光在光纤中约 2×10^5 km/s
    emulate_wan: bool = True  # 网关是否按 RTT 注入时延（跨数据中心仿真）
    overrides: dict[str, dict[str, float]] = Field(default_factory=dict)  # 实测 RTT：{source: {site: ms}}


class DREvent(BaseModel):
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
        return self


class GridConfig(BaseModel):
    tariffs: str = "data/grid/tou_tariffs.yaml"
    carbon_factors: str = "data/grid/carbon_factors_2023.csv"
    carbon_profile: Literal["flat", "synthetic"] = "synthetic"
    traces: str | None = None  # 可选：逐时实测/外部信号 CSV（覆盖电价与碳强度）
    dr_events: list[DREvent] = Field(default_factory=list)


class TokenDist(BaseModel):
    """对数正态 token 长度分布（mean 为算术均值）。"""

    mean: float
    sigma: float = 0.8
    min: int = 1
    max: int = 8192


class WorkloadConfig(BaseModel):
    rps: float = 5.0  # 全局平均到达率（请求/秒）
    diurnal_amplitude: float = 0.5  # 日内波动幅度 A：λ(h) = rps × (1 + A·cos(2π(h−peak)/24))
    peak_hour: float = 20.0
    input_tokens: TokenDist = TokenDist(mean=800, sigma=0.8)
    output_tokens: TokenDist = TokenDist(mean=200, sigma=0.7, max=2048)
    trace: str | None = None  # 可选：按 Azure LLM 推理 trace 格式回放
    seed: int = 1


class SLOConfig(BaseModel):
    ttft_s: float = 2.0  # 首 token 时延
    tpot_s: float = 0.15  # 每输出 token 时延


class PolicyConfig(BaseModel):
    policy: str
    params: dict[str, Any] = Field(default_factory=dict)


class SchedulerConfig(BaseModel):
    routing: PolicyConfig = PolicyConfig(policy="nearest")
    capacity: PolicyConfig = PolicyConfig(policy="static")
    control_interval_s: float = 15.0  # 在线平台的容量控制周期（真实秒）
    sim_control_interval_s: float = 300.0  # 离散事件仿真中的容量控制周期（仿真秒）
    carbon_price_yuan_per_kg: float = 0.1  # 碳价（元/kgCO2），用于把碳排放折算为成本
    queue_limit_per_slot: float = 4.0  # 站点排队上限 = 在线并发槽数 × 该系数


class Scenario(BaseModel):
    name: str = "scenario"
    clock: ClockConfig = ClockConfig()
    sources: list[SourceRegion]
    sites: list[SiteConfig]
    node_profiles: dict[str, NodeProfile] = Field(default_factory=lambda: {"default": NodeProfile()})
    network: NetworkConfig = NetworkConfig()
    grid: GridConfig = GridConfig()
    workload: WorkloadConfig = WorkloadConfig()
    slo: SLOConfig = SLOConfig()
    scheduler: SchedulerConfig = SchedulerConfig()

    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self) -> "Scenario":
        ids = [s.id for s in self.sites]
        if len(set(ids)) != len(ids):
            raise ValueError("站点 id 重复")
        for s in self.sites:
            if s.profile not in self.node_profiles:
                raise ValueError(f"站点 {s.id} 引用了不存在的 node profile: {s.profile}")
            if s.backend == "openai" and not s.upstreams:
                raise ValueError(f"站点 {s.id} 使用 openai 后端但未配置 upstreams")
        for e in self.grid.dr_events:
            if e.site not in ids:
                raise ValueError(f"需求响应事件引用了不存在的站点: {e.site}")
        return self

    def site(self, site_id: str) -> SiteConfig:
        for s in self.sites:
            if s.id == site_id:
                return s
        raise KeyError(site_id)

    def profile_of(self, site_id: str) -> NodeProfile:
        return self.node_profiles[self.site(site_id).profile]

    def resolve(self, path: str | None) -> Path | None:
        """相对路径按场景文件所在目录的上一级（项目根）或场景目录解析。"""
        if path is None:
            return None
        p = Path(path)
        if p.is_absolute():
            return p
        for root in (self.base_dir, self.base_dir.parent, Path.cwd()):
            if (root / p).exists():
                return root / p
        return self.base_dir.parent / p


def load_scenario(path: str | os.PathLike) -> Scenario:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    scn = Scenario.model_validate(raw)
    scn.base_dir = path.parent
    return scn


class Endpoints(BaseModel):
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
    return Endpoints(
        grid=f"http://{host}:9000",
        gateway=f"http://{host}:8080",
        controller=f"http://{host}:9300",
        sites={s.id: f"http://{host}:{9101 + i}" for i, s in enumerate(scn.sites)},
    )


def load_endpoints(path: str | os.PathLike | None, scn: Scenario) -> Endpoints:
    """读取地址文件；未提供时使用本机默认端口。环境变量 CPC_ENDPOINTS 也可指定文件。"""
    path = path or os.environ.get("CPC_ENDPOINTS")
    if not path:
        return default_local_endpoints(scn)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    ep = Endpoints.model_validate(raw)
    missing = [s.id for s in scn.sites if s.id not in ep.sites]
    if missing:
        raise ValueError(f"地址文件缺少站点: {missing}")
    return ep
