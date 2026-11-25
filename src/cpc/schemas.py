"""Data exchanged between services (JSON). Shared by the live services and the simulator; policies
depend only on these structures.

Power and energy fields may be None: the reading is missing (sensor failure, device without a
reading, ...); power_missing and components say why.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NodeSnapshot(BaseModel):
    """State of one server."""

    id: str
    pool: str = ""
    state: str  # off / booting / on / draining
    slots: int
    busy: int
    cap_ratio: float = 1.0
    it_power_w: float | None = None  # None when missing
    gpu_power_w: float | None = None
    power_source: str = "model"  # model (power model) / sensors (measured)
    power_missing: list[str] = Field(default_factory=list)  # missing power components, e.g. device:gpu0, host
    last_error: str | None = None  # last error of a power or power-cap action


class PoolSnapshot(BaseModel):
    """Summary of a server pool."""

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
    """Summary of a data centre (GET /state of a site agent; produced by the same runtime in simulation).

    Pools may be heterogeneous: slots_per_node, node_idle_w and node_peak_w average over all servers;
    pools gives the per-pool values.
    """

    site: str
    ts: float = 0.0  # sampling time (Unix time; simulated seconds in simulation)
    backend: str = "emulated"  # emulated / openai / mixed
    nodes_total: int
    nodes_on: int
    nodes_booting: int = 0
    nodes_draining: int = 0
    slots_per_node: float
    slots_on: int  # slots of online servers (accepting new requests)
    busy: int  # requests in service
    queue_len: int  # requests waiting
    queue_limit: int
    cap_ratio: float = 1.0
    cap_min_ratio: float = 0.5  # lowest power-cap ratio of the servers (profile parameter)
    it_power_w: float | None  # None when the power of any server is missing
    facility_power_w: float | None
    pue: float
    power_scope: str = "node"  # node: accelerators + host; accelerators: host part not counted
    energy_it_j: float = 0.0  # IT energy accumulated over intervals with valid power
    energy_coverage: float | None = None  # share of server-seconds with valid power; None before any data
    est_service_s: float = 5.0  # moving estimate of the mean service time
    arrival_rate: float = 0.0  # requests per second (moving estimate)
    node_idle_w: float = 0.0  # online idle power of a server (profile parameter, mean)
    node_peak_w: float = 0.0  # full-load power of a server (profile parameter, mean)
    pools: list[PoolSnapshot] = Field(default_factory=list)
    nodes: list[NodeSnapshot] = Field(default_factory=list)
    components: dict[str, dict] = Field(default_factory=dict)  # health of sub-components (power sensors, ...)

    @property
    def utilization(self) -> float:
        return (self.busy + self.queue_len) / max(self.slots_on, 1)

    @property
    def saturated(self) -> bool:
        return self.slots_on == 0 or self.queue_len >= self.queue_limit


class CapacityTarget(BaseModel):
    """Instruction to a data centre (global -> local): servers to keep online and power-cap ratio (None: unchanged)."""

    active_nodes: int | None = None
    cap_ratio: float | None = None


class DemandSnapshot(BaseModel):
    """Demand statistics of the gateway (GET /state)."""

    rates: dict[str, float] = Field(default_factory=dict)  # arrival rate per source region (requests/s)
    site_rates: dict[str, float] = Field(default_factory=dict)  # rate routed to each site
    policy: str = ""
    weights: dict[str, dict[str, float]] = Field(default_factory=dict)
