"""服务之间交换的数据结构（JSON）。在线服务与仿真器共用，策略代码只依赖这些结构。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NodeSnapshot(BaseModel):
    id: str
    state: str  # off / booting / on / draining
    slots: int
    busy: int
    cap_ratio: float = 1.0
    it_power_w: float = 0.0
    gpu_power_w: float = 0.0
    power_source: str = "model"  # model / nvml / mixed


class SiteSnapshot(BaseModel):
    """站点代理 GET /state 的返回。"""

    site: str
    ts: float = 0.0  # 采样时刻（真实 Unix 时间；仿真中为仿真秒）
    backend: str = "emulated"
    healthy: bool = True
    nodes_total: int
    nodes_on: int
    nodes_booting: int = 0
    nodes_draining: int = 0
    slots_per_node: int
    slots_on: int  # 在线（可接新请求）的并发槽数
    busy: int  # 正在服务的请求数
    queue_len: int  # 排队等待的请求数
    queue_limit: int
    cap_ratio: float = 1.0
    it_power_w: float
    facility_power_w: float
    pue: float
    energy_it_j: float = 0.0  # 累计 IT 能耗
    est_service_s: float = 5.0  # 平均服务时间的滑动估计
    arrival_rate: float = 0.0  # 到达率（请求/秒，滑动估计）
    node_idle_w: float = 0.0  # 单节点空载功率（模型参数或实测估计）
    node_peak_w: float = 0.0  # 单节点满载功率
    nodes: list[NodeSnapshot] = Field(default_factory=list)

    @property
    def utilization(self) -> float:
        return (self.busy + self.queue_len) / max(self.slots_on, 1)

    @property
    def saturated(self) -> bool:
        return self.slots_on == 0 or self.queue_len >= self.queue_limit


class CapacityTarget(BaseModel):
    """容量控制指令：目标在线节点数与 GPU 功率封顶比例（None 表示不改变）。"""

    active_nodes: int | None = None
    cap_ratio: float | None = None


class DemandSnapshot(BaseModel):
    """网关 GET /state 中的需求统计。"""

    rates: dict[str, float] = Field(default_factory=dict)  # 各来源区域的到达率（请求/秒）
    site_rates: dict[str, float] = Field(default_factory=dict)  # 各站点被分配的请求率
    policy: str = ""
    weights: dict[str, dict[str, float]] = Field(default_factory=dict)
