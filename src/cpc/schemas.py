"""服务之间交换的数据结构（JSON）。在线服务与仿真器共用，策略代码只依赖这些结构。

功率与能耗字段可能为 None：表示对应读数缺失（传感器故障、设备无读数等），缺失原因见 power_missing 与 components。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NodeSnapshot(BaseModel):
    """服务器状态。"""

    id: str
    pool: str = ""
    state: str  # off / booting / on / draining
    slots: int
    busy: int
    cap_ratio: float = 1.0
    it_power_w: float | None = None  # 缺失时为 None
    gpu_power_w: float | None = None
    power_source: str = "model"  # model（功率模型）/ sensors（传感器实测）
    power_missing: list[str] = Field(default_factory=list)  # 缺失的功率组成部分，如 device:gpu0、host
    last_error: str | None = None  # 最近一次开关机或功率封顶操作的错误


class PoolSnapshot(BaseModel):
    """服务器池摘要。"""

    id: str
    profile: str
    backend: str
    total: int
    on: int
    booting: int = 0
    draining: int = 0
    slots_per_node: float
    node_idle_w: float
    node_peak_w: float


class SiteSnapshot(BaseModel):
    """数据中心状态摘要（站点代理 GET /state 的返回；仿真器中由同一运行时给出）。

    服务器池可以异构：slots_per_node、node_idle_w、node_peak_w 为全部服务器的平均值，各池的取值见 pools。
    """

    site: str
    ts: float = 0.0  # 采样时刻（真实 Unix 时间；仿真中为仿真秒）
    backend: str = "emulated"  # emulated / openai / mixed
    nodes_total: int
    nodes_on: int
    nodes_booting: int = 0
    nodes_draining: int = 0
    slots_per_node: float
    slots_on: int  # 在线（可接新请求）的并发槽数
    busy: int  # 正在服务的请求数
    queue_len: int  # 排队等待的请求数
    queue_limit: int
    cap_ratio: float = 1.0
    cap_min_ratio: float = 0.5  # 节点允许的最低功率封顶比例（节点配置参数）
    it_power_w: float | None  # 任一节点功率缺失时为 None
    facility_power_w: float | None
    pue: float
    power_scope: str = "node"  # node：加速卡 + 主机部分；accelerators：只含加速卡（主机部分不计入）
    energy_it_j: float = 0.0  # 功率有效区间内的累计 IT 能耗
    energy_coverage: float | None = None  # 功率有效时长占比（节点·秒加权）；尚无数据时为 None
    est_service_s: float = 5.0  # 平均服务时间的滑动估计
    arrival_rate: float = 0.0  # 到达率（请求/秒，滑动估计）
    node_idle_w: float = 0.0  # 服务器在线空载功率（模型参数，平均值）
    node_peak_w: float = 0.0  # 服务器满载功率（模型参数，平均值）
    pools: list[PoolSnapshot] = Field(default_factory=list)
    nodes: list[NodeSnapshot] = Field(default_factory=list)
    components: dict[str, dict] = Field(default_factory=dict)  # 子模块（功率传感器等）的健康状态

    @property
    def utilization(self) -> float:
        return (self.busy + self.queue_len) / max(self.slots_on, 1)

    @property
    def saturated(self) -> bool:
        return self.slots_on == 0 or self.queue_len >= self.queue_limit


class CapacityTarget(BaseModel):
    """数据中心指令（全局 → 本地）：目标在线服务器数与功率封顶比例（None 表示不改变）。"""

    active_nodes: int | None = None
    cap_ratio: float | None = None


class DemandSnapshot(BaseModel):
    """网关 GET /state 中的需求统计。"""

    rates: dict[str, float] = Field(default_factory=dict)  # 各来源区域的到达率（请求/秒）
    site_rates: dict[str, float] = Field(default_factory=dict)  # 各站点被分配的请求率
    policy: str = ""
    weights: dict[str, dict[str, float]] = Field(default_factory=dict)
