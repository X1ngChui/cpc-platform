"""电网侧信号：电价、碳强度与扩展信号（来自 grid_signal 数据集），以及需求响应（功率上限）事件。

场景 grid.price、grid.carbon、grid.extras 各自指定数据集、信号名与站点到区域的对应（缺省为站点的 region）。
所有信号按"信号时间"（带时区的 datetime）查询，与时钟实现无关，在线服务与离散事件仿真器共用。

启动检查：数据集符合规范；信号在数据集中声明且单位符合要求（电价 <货币>/kWh，碳强度 kgCO2/kWh）；
全部站点的区域在数据集中收录；数据覆盖场景时钟起点。
运行期：某站点某时刻的电价或碳强度不可用时 signal() 抛出 SignalUnavailable，snapshot() 把该站点列入 missing；
扩展信号不可用时其取值为 None。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..config import DREvent, Scenario, SignalSource
from ..datasets.descriptor import DatasetError, load_descriptor
from ..datasets.grid import SignalSet, SignalUnavailable

__all__ = ["GridModel", "GridSignal", "SignalUnavailable"]


class GridSignal(BaseModel):
    site: str
    time: datetime
    price: float  # 电价（currency/kWh）
    price_label: str | None = None  # 时段标签（如峰、平、谷），数据集给出时
    carbon_intensity: float  # kgCO2/kWh
    currency: str
    power_cap_kw: float | None = None  # 设施功率上限（并网容量或需求响应）
    dr_event: str | None = None
    pue: float = 1.0
    carbon_price: float = 0.0  # currency/kgCO2
    extra: dict[str, float | None] = Field(default_factory=dict)  # 扩展信号（场景 grid.extras），不可用时为 None

    @property
    def effective_price(self) -> float:
        """计入碳成本后的等效电价（currency/kWh，按设施电量计）。"""
        return self.price + self.carbon_price * self.carbon_intensity


class _Bound:
    """场景中的一个信号来源：数据集 + 信号名 + 站点 → 区域。"""

    def __init__(self, scn: Scenario, what: str, src: SignalSource, sets: dict, unit_rule):
        d = load_descriptor(src.dataset, scn.base_dir)
        key = str(d.root)
        if key not in sets:
            sets[key] = SignalSet(d)
        self.set: SignalSet = sets[key]
        self.signal = src.signal
        self.ref = d.ref
        if src.signal not in self.set.meta:
            raise DatasetError(f"{what}：数据集 {d.ref} 没有声明信号 {src.signal}（有 {self.set.signals()}）")
        self.unit = self.set.unit(src.signal)
        if unit_rule is not None and not unit_rule(self.unit):
            raise DatasetError(f"{what}：数据集 {d.ref} 的信号 {src.signal} 单位为 {self.unit}，不符合要求")
        self.regions = {s.id: scn.region(s.id, src) for s in scn.sites}
        have = set(self.set.regions(src.signal))
        lack = {k: v for k, v in self.regions.items() if v not in have}
        if lack:
            raise DatasetError(f"{what}：数据集 {d.ref} 的信号 {src.signal} 没有收录站点的区域 {lack}")
        for sid, reg in self.regions.items():
            lo, hi = self.set.coverage(src.signal, reg)
            if not (lo <= scn.clock.start < hi):
                raise DatasetError(f"{what}：数据集 {d.ref} 中区域 {reg} 的覆盖范围 {lo.isoformat()} – {hi.isoformat()} "
                                   f"不含场景时钟起点 {scn.clock.start.isoformat()}")

    def value(self, site: str, t: datetime):
        return self.set.value(self.signal, self.regions[site], t)


class GridModel:
    def __init__(self, scn: Scenario):
        self.scn = scn
        sets: dict[str, SignalSet] = {}
        self.price = _Bound(scn, "grid.price", scn.grid.price, sets, lambda u: u.endswith("/kWh") and len(u) > 4)
        self.carbon = _Bound(scn, "grid.carbon", scn.grid.carbon, sets, lambda u: u == "kgCO2/kWh")
        self.extras = {x.name: _Bound(scn, f"grid.extras[{x.name}]", x, sets, None) for x in scn.grid.extras}
        self.currency = self.price.unit.removesuffix("/kWh")
        self.events: list[DREvent] = list(scn.grid.dr_events)

    def sources(self) -> dict[str, str]:
        out = {"price": f"{self.price.ref}:{self.price.signal}", "carbon": f"{self.carbon.ref}:{self.carbon.signal}"}
        out.update({k: f"{b.ref}:{b.signal}" for k, b in self.extras.items()})
        return out

    def add_event(self, ev: DREvent) -> None:
        if ev.site not in {s.id for s in self.scn.sites}:
            raise KeyError(ev.site)
        self.events.append(ev)

    def clear_events(self, site: str | None = None) -> None:
        self.events = [e for e in self.events if site is not None and e.site != site]

    def signal(self, site_id: str, t: datetime) -> GridSignal:
        """站点在信号时间 t 的电网信号；电价或碳强度不可用时抛出 SignalUnavailable。"""
        site = self.scn.site(site_id)
        p = self.price.value(site_id, t)
        c = self.carbon.value(site_id, t)
        extra: dict[str, float | None] = {}
        for k, b in self.extras.items():
            try:
                extra[k] = b.value(site_id, t).value
            except SignalUnavailable:
                extra[k] = None
        cap = site.grid_cap_kw
        event = None
        for e in self.events:
            if e.site == site_id and e.start <= t < e.end:
                cap = e.cap_kw if cap is None else min(cap, e.cap_kw)
                event = e.kind
        return GridSignal(site=site_id, time=t, price=p.value, price_label=p.label, carbon_intensity=c.value,
                          currency=self.currency, power_cap_kw=cap, dr_event=event, pue=site.pue,
                          carbon_price=self.scn.scheduler.carbon_price_per_kg, extra=extra)

    def snapshot(self, t: datetime) -> tuple[dict[str, GridSignal], dict[str, str]]:
        """全部站点的信号与不可用站点的原因。"""
        out, missing = {}, {}
        for s in self.scn.sites:
            try:
                out[s.id] = self.signal(s.id, t)
            except SignalUnavailable as e:
                missing[s.id] = str(e)
        return out, missing
