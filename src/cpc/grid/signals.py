"""Grid-side signals: price, carbon intensity and extra signals (from grid_signal datasets), and
demand-response (power-limit) events.

grid.price, grid.carbon and grid.extras of the scenario each name a dataset, a signal and the
site -> region map (default: the region of the site). Signals are queried by signal time (an aware
datetime), independently of any clock, so the live services and the simulator share this module.

Startup checks: the datasets follow the specification; the signals are declared with the required
units (price <currency>/kWh, carbon intensity kgCO2/kWh); every site region is covered; the data
covers the start of the scenario clock.
At run time signal() raises SignalUnavailable when the price or carbon intensity of a site is
unavailable at a time, and snapshot() lists the site under missing; an unavailable extra signal is None.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field

from ..config import DREvent, Scenario, SignalSource
from ..datasets.descriptor import DatasetError, load_descriptor
from ..datasets.grid import SignalSet, SignalUnavailable

__all__ = ["GridModel", "GridSignal", "SignalUnavailable"]


class GridSignal(BaseModel):
    site: str
    time: datetime
    price: float  # currency/kWh
    price_label: str | None = None  # tariff period (e.g. peak, flat, valley) when the dataset gives one
    carbon_intensity: float  # kgCO2/kWh
    currency: str
    power_cap_kw: float | None = None  # facility power limit (grid connection or demand response)
    dr_event: str | None = None
    pue: float = 1.0
    carbon_price: float = 0.0  # currency/kgCO2
    extra: dict[str, float | None] = Field(default_factory=dict)  # grid.extras; None when unavailable

    @property
    def effective_price(self) -> float:
        """Price including the carbon cost (currency/kWh of facility energy)."""
        return self.price + self.carbon_price * self.carbon_intensity


class _Bound:
    """One signal source of the scenario: dataset + signal + site -> region."""

    def __init__(self, scn: Scenario, what: str, src: SignalSource, sets: dict[str, SignalSet],
                 unit_ok: Callable[[str], bool] | None = None):
        d = load_descriptor(src.dataset, scn.base_dir)
        if (key := str(d.root)) not in sets:  # datasets shared by several signals are loaded once
            sets[key] = SignalSet(d)
        self.set = sets[key]
        self.signal, self.ref = src.signal, d.ref
        where = f"{what}: dataset {d.ref}"
        if src.signal not in self.set.meta:
            raise DatasetError(f"{where} does not declare signal {src.signal} (it has {self.set.signals()})")
        self.unit = self.set.unit(src.signal)
        if unit_ok is not None and not unit_ok(self.unit):
            raise DatasetError(f"{where}: signal {src.signal} has unit {self.unit}, which is not allowed here")
        self.regions = {s.id: scn.region(s.id, src) for s in scn.sites}
        have = set(self.set.regions(src.signal))
        if lack := {k: v for k, v in self.regions.items() if v not in have}:
            raise DatasetError(f"{where}: signal {src.signal} does not cover the site regions {lack}")
        for reg in set(self.regions.values()):
            lo, hi = self.set.coverage(src.signal, reg)
            if not lo <= scn.clock.start < hi:
                raise DatasetError(f"{where}: region {reg} covers {lo.isoformat()} - {hi.isoformat()}, "
                                   f"which does not include the clock start {scn.clock.start.isoformat()}")

    def value(self, site: str, t: datetime):
        return self.set.value(self.signal, self.regions[site], t)

    @property
    def source(self) -> str:
        return f"{self.ref}:{self.signal}"


class GridModel:
    def __init__(self, scn: Scenario):
        self.scn = scn
        sets: dict[str, SignalSet] = {}
        self.price = _Bound(scn, "grid.price", scn.grid.price, sets, lambda u: u.endswith("/kWh") and len(u) > 4)
        self.carbon = _Bound(scn, "grid.carbon", scn.grid.carbon, sets, lambda u: u == "kgCO2/kWh")
        self.extras = {x.name: _Bound(scn, f"grid.extras[{x.name}]", x, sets) for x in scn.grid.extras}
        self.currency = self.price.unit.removesuffix("/kWh")
        self.events: list[DREvent] = list(scn.grid.dr_events)

    def sources(self) -> dict[str, str]:
        return {"price": self.price.source, "carbon": self.carbon.source} | {k: b.source for k, b in self.extras.items()}

    def add_event(self, ev: DREvent) -> None:
        if ev.site not in {s.id for s in self.scn.sites}:
            raise KeyError(f"unknown site {ev.site}")
        self.events.append(ev)

    def clear_events(self, site: str | None = None) -> None:
        self.events = [e for e in self.events if site is not None and e.site != site]

    def _extra(self, b: _Bound, site_id: str, t: datetime) -> float | None:
        try:
            return b.value(site_id, t).value
        except SignalUnavailable:
            return None

    def signal(self, site_id: str, t: datetime) -> GridSignal:
        """Grid signal of a site at signal time t; SignalUnavailable when price or carbon intensity is unavailable."""
        site = self.scn.site(site_id)
        p, c = self.price.value(site_id, t), self.carbon.value(site_id, t)
        active = [e for e in self.events if e.site == site_id and e.start <= t < e.end]
        caps = [x for x in (site.grid_cap_kw, *(e.cap_kw for e in active)) if x is not None]
        return GridSignal(site=site_id, time=t, price=p.value, price_label=p.label, carbon_intensity=c.value,
                          currency=self.currency, power_cap_kw=min(caps, default=None),
                          dr_event=active[-1].kind if active else None, pue=site.pue,
                          carbon_price=self.scn.scheduler.carbon_price_per_kg,
                          extra={k: self._extra(b, site_id, t) for k, b in self.extras.items()})

    def snapshot(self, t: datetime) -> tuple[dict[str, GridSignal], dict[str, str]]:
        """Signals of all sites and the reasons for the unavailable ones."""
        out, missing = {}, {}
        for s in self.scn.sites:
            try:
                out[s.id] = self.signal(s.id, t)
            except SignalUnavailable as e:
                missing[s.id] = str(e)
        return out, missing
