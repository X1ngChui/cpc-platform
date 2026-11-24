"""电网侧信号：电价、碳强度（来自电网信号数据集）与需求响应（功率上限）事件。

电价与碳强度分别由场景 grid.price、grid.carbon 指定的数据集给出（cpc.datasets.grid），站点到数据集区域的对应关系
见 Scenario.region。所有信号按"信号时间"（带时区的 datetime）查询，与时钟实现无关，在线服务与离散事件仿真器共用。

启动检查：数据集文件存在且校验通过；全部站点的区域在数据集中收录；有时间覆盖范围的数据集须覆盖场景时钟起点。
运行期：某站点某时刻的信号不可用时抛出 SignalUnavailable；snapshot() 把该站点列入 missing 并给出原因。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel

from ..config import DREvent, Scenario
from ..datasets import DatasetError, make_dataset
from ..datasets.grid import GridSignalDataset, SignalUnavailable

__all__ = ["GridModel", "GridSignal", "SignalUnavailable"]


class GridSignal(BaseModel):
    site: str
    province: str
    time: datetime
    price_yuan_per_kwh: float
    period: str
    carbon_kg_per_kwh: float
    power_cap_kw: float | None = None  # 设施功率上限（并网容量或需求响应）
    dr_event: str | None = None
    pue: float = 1.0
    carbon_price_yuan_per_kg: float = 0.0

    @property
    def effective_price(self) -> float:
        """计入碳成本后的等效电价（元/kWh，按设施电量计）。"""
        return self.price_yuan_per_kwh + self.carbon_price_yuan_per_kg * self.carbon_kg_per_kwh


def _load(scn: Scenario, quantity: str) -> GridSignalDataset:
    src = scn.grid.price if quantity == "price" else scn.grid.carbon
    ds = make_dataset(src.dataset, **src.params)
    if not isinstance(ds, GridSignalDataset):
        raise DatasetError(f"数据集 {src.dataset} 不是电网信号数据集")
    ds.check(quantity)
    ds.verify()
    regions = set(ds.regions())
    lack = {s.id: scn.region(s.id, quantity) for s in scn.sites if scn.region(s.id, quantity) not in regions}
    if lack:
        raise DatasetError(f"{quantity} 数据集 {ds.ref} 没有收录站点的区域 {lack}")
    cov = ds.coverage()
    if cov is not None and not (cov[0] <= scn.clock.start < cov[1]):
        raise DatasetError(f"{quantity} 数据集 {ds.ref} 覆盖 {cov[0].isoformat()} – {cov[1].isoformat()}，不含场景时钟起点 {scn.clock.start.isoformat()}")
    return ds


class GridModel:
    def __init__(self, scn: Scenario):
        self.scn = scn
        self.price = _load(scn, "price")
        self.carbon = _load(scn, "carbon")
        self.events: list[DREvent] = list(scn.grid.dr_events)

    def sources(self) -> dict[str, str]:
        return {"price": self.price.ref, "carbon": self.carbon.ref}

    def add_event(self, ev: DREvent) -> None:
        if ev.site not in {s.id for s in self.scn.sites}:
            raise KeyError(ev.site)
        self.events.append(ev)

    def clear_events(self, site: str | None = None) -> None:
        self.events = [e for e in self.events if site is not None and e.site != site]

    def signal(self, site_id: str, t: datetime) -> GridSignal:
        """站点在信号时间 t 的电网信号；电价或碳强度不可用时抛出 SignalUnavailable。"""
        site = self.scn.site(site_id)
        p = self.price.value("price", self.scn.region(site_id, "price"), t)
        c = self.carbon.value("carbon", self.scn.region(site_id, "carbon"), t)
        cap = site.grid_cap_kw
        event = None
        for e in self.events:
            if e.site == site_id and e.start <= t < e.end:
                cap = e.cap_kw if cap is None else min(cap, e.cap_kw)
                event = e.kind
        return GridSignal(
            site=site_id, province=site.province, time=t, price_yuan_per_kwh=p.value, period=p.period or "-",
            carbon_kg_per_kwh=round(c.value, 6), power_cap_kw=cap, dr_event=event, pue=site.pue,
            carbon_price_yuan_per_kg=self.scn.scheduler.carbon_price_yuan_per_kg,
        )

    def snapshot(self, t: datetime) -> tuple[dict[str, GridSignal], dict[str, str]]:
        """全部站点的信号与不可用站点的原因。"""
        out, missing = {}, {}
        for s in self.scn.sites:
            try:
                out[s.id] = self.signal(s.id, t)
            except SignalUnavailable as e:
                missing[s.id] = str(e)
        return out, missing

    def forecast(self, site_id: str, t0: datetime, hours: float = 24, step_min: int = 60) -> list[GridSignal | None]:
        """按数据集逐点给出未来若干小时的信号（分时电价可预知；有时间序列的数据集给出其记录值）；不可用的点为 None。"""
        n = int(hours * 60 / step_min)
        out: list[GridSignal | None] = []
        for i in range(n):
            try:
                out.append(self.signal(site_id, t0 + timedelta(minutes=i * step_min)))
            except SignalUnavailable:
                out.append(None)
        return out
