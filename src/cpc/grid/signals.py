"""电网侧信号模型：分时电价、碳排放因子、需求响应（功率上限）事件。

所有信号按"信号时间"（带时区的 datetime）查询，与时钟实现无关，
因此在线服务与离散事件仿真器共用同一份代码。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel

from ..config import CST, DREvent, Scenario

Range = tuple[float, float]


def _in(hour: float, ranges: list[list[float]] | None) -> bool:
    return any(a <= hour < b for a, b in (ranges or []))


@dataclass
class Tariff:
    key: str
    name: str
    source: str
    flat_price: float
    seasons: list[dict]

    def season(self, month: int) -> dict:
        for s in self.seasons:
            if month in s.get("months", []):
                return s
        return self.seasons[0]

    def price(self, t: datetime) -> tuple[float, str]:
        """返回 (电价 元/kWh, 时段名 sharp/peak/flat/valley/deep_valley)。"""
        t = t.astimezone(CST)
        h = t.hour + t.minute / 60.0
        s = self.season(t.month)
        periods = s.get("periods", {}) or {}
        mult = s.get("multipliers", {}) or {}
        if _in(h, periods.get("peak")):
            period, m = "peak", mult.get("peak", 1.0)
        elif _in(h, periods.get("valley")):
            period, m = "valley", mult.get("valley", 1.0)
        else:
            period, m = "flat", 1.0
        sharp = s.get("sharp")
        if sharp and t.month in sharp.get("months", s.get("months", [])) and _in(h, sharp.get("hours")):
            period, m = "sharp", mult.get("peak", 1.0) * sharp["multiplier_over_peak"]
        deep = s.get("deep_valley")
        if deep and t.month in deep.get("months", s.get("months", [])) and _in(h, deep.get("hours")):
            period, m = "deep_valley", mult.get("valley", 1.0) * deep["multiplier_over_valley"]
        return round(self.flat_price * m, 6), period


class TariffBook:
    def __init__(self, path: Path):
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.tariffs = {
            k: Tariff(key=k, name=v.get("name", k), source=v.get("source", ""), flat_price=float(v["flat_price"]), seasons=v["seasons"])
            for k, v in raw.items()
        }

    def get(self, province: str) -> Tariff:
        return self.tariffs.get(province) or self.tariffs["default"]


class CarbonBook:
    """省级电力平均二氧化碳排放因子（年均值），可叠加合成的日内形状。"""

    def __init__(self, path: Path):
        self.factors: dict[str, float] = {}
        self.names: dict[str, str] = {}
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                self.factors[row["province"]] = float(row["factor_kg_per_kwh"])
                self.names[row["province"]] = row["name"]

    def annual(self, province: str) -> float:
        if province not in self.factors:
            raise KeyError(f"排放因子表中没有省份 {province}")
        return self.factors[province]

    @staticmethod
    def synthetic_shape(hour: float) -> float:
        """均值为 0 的示意日内形状：午间光伏出力高 → 碳强度低（−1 附近），夜间略高。

        这是示意形状，不是实测；有逐时数据时请用 grid.traces 覆盖。
        """
        solar = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0)) if 6.0 <= hour <= 18.0 else 0.0
        return -(solar - 1.0 / math.pi)  # solar 的日均值为 1/π，减去后日均为 0


class TraceBook:
    """外部逐时信号（例如现货价格、实测碳强度）。CSV 列：time,key,price_yuan_per_kwh,carbon_kg_per_kwh

    key 可以是站点 id 或省份键；缺失列表示不覆盖该项。
    """

    def __init__(self, path: Path):
        self.data: dict[tuple[str, datetime], dict[str, float]] = {}
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                t = datetime.fromisoformat(row["time"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=CST)
                t = t.astimezone(CST).replace(minute=0, second=0, microsecond=0)
                vals = {}
                for col in ("price_yuan_per_kwh", "carbon_kg_per_kwh"):
                    if row.get(col) not in (None, ""):
                        vals[col] = float(row[col])
                self.data[(row["key"], t)] = vals

    def lookup(self, keys: list[str], t: datetime) -> dict[str, float]:
        t = t.astimezone(CST).replace(minute=0, second=0, microsecond=0)
        for k in keys:
            if (k, t) in self.data:
                return self.data[(k, t)]
        return {}


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


class GridModel:
    def __init__(self, scn: Scenario):
        self.scn = scn
        self.tariffs = TariffBook(scn.resolve(scn.grid.tariffs))
        self.carbon = CarbonBook(scn.resolve(scn.grid.carbon_factors))
        self.traces = TraceBook(scn.resolve(scn.grid.traces)) if scn.grid.traces else None
        self.events: list[DREvent] = list(scn.grid.dr_events)

    def add_event(self, ev: DREvent) -> None:
        if ev.site not in {s.id for s in self.scn.sites}:
            raise KeyError(ev.site)
        self.events.append(ev)

    def clear_events(self, site: str | None = None) -> None:
        self.events = [e for e in self.events if site is not None and e.site != site]

    def signal(self, site_id: str, t: datetime) -> GridSignal:
        site = self.scn.site(site_id)
        price, period = self.tariffs.get(site.province).price(t)
        carbon = self.carbon.annual(site.province)
        if self.scn.grid.carbon_profile == "synthetic":
            lt = t.astimezone(CST)
            carbon *= 1.0 + site.carbon_amplitude * CarbonBook.synthetic_shape(lt.hour + lt.minute / 60.0)
        if self.traces:
            ov = self.traces.lookup([site.id, site.province], t)
            price = ov.get("price_yuan_per_kwh", price)
            carbon = ov.get("carbon_kg_per_kwh", carbon)
            if "price_yuan_per_kwh" in ov:
                period = "trace"
        cap = site.grid_cap_kw
        event = None
        for e in self.events:
            if e.site == site_id and e.start <= t < e.end:
                cap = e.cap_kw if cap is None else min(cap, e.cap_kw)
                event = e.kind
        return GridSignal(
            site=site_id,
            province=site.province,
            time=t,
            price_yuan_per_kwh=price,
            period=period,
            carbon_kg_per_kwh=round(carbon, 6),
            power_cap_kw=cap,
            dr_event=event,
            pue=site.pue,
            carbon_price_yuan_per_kg=self.scn.scheduler.carbon_price_yuan_per_kg,
        )

    def snapshot(self, t: datetime) -> dict[str, GridSignal]:
        return {s.id: self.signal(s.id, t) for s in self.scn.sites}

    def forecast(self, site_id: str, t0: datetime, hours: float = 24, step_min: int = 60) -> list[GridSignal]:
        """确定性预测（分时电价与合成碳强度本身可预知；DR 事件按已登记的计算）。"""
        n = int(hours * 60 / step_min)
        return [self.signal(site_id, t0 + timedelta(minutes=i * step_min)) for i in range(n)]
