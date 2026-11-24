"""电网信号数据集：按区域与时间给出电价或碳强度。

接口：GridSignalDataset.value(quantity, region, t) → SignalValue（数值与电价时段）；
数据在该区域、该时刻不可用时抛出 SignalUnavailable（不以其他值替代）。quantity ∈ {"price", "carbon"}。

接入的数据集：
    cn-tou-tariffs             各省工商业峰谷分时电价的时段结构与浮动比例（政策文件整理，随包提供）
    cn-provincial-carbon-2023  生态环境部、国家统计局 2023 年省级电力二氧化碳排放因子（年均值，随包提供）
    eia930-carbon-2024h1       美国 EIA-930 逐时分燃料净发电量（2024 年 1–6 月）× 显式给出的分燃料排放因子 → 逐时碳强度
测试与示例用（synthetic）：
    synthetic-diurnal-carbon   年均排放因子叠加日内正弦形状
    fixed-price                各区域固定电价（参数给定，非实测）

需求响应事件不来自数据集：由场景 grid.dr_events 登记或运行时注入。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import numpy as np
import yaml

from .base import BundledFile, Dataset, DatasetError, Manifest, RemoteFile, register_dataset

CST = timezone(timedelta(hours=8))
QUANTITIES = ("price", "carbon")
UNITS = {"price": "元/kWh", "carbon": "kgCO2/kWh"}


class SignalUnavailable(DatasetError):
    """某区域某时刻的信号不可用（超出覆盖范围或原始数据缺失）。"""


@dataclass(frozen=True)
class SignalValue:
    value: float
    period: str | None = None  # 电价时段（sharp / peak / flat / valley / deep_valley），分时电价数据集给出


class GridSignalDataset(Dataset):
    quantities: ClassVar[frozenset[str]]

    def regions(self) -> list[str]:
        raise NotImplementedError

    def value(self, quantity: str, region: str, t: datetime) -> SignalValue:
        raise NotImplementedError

    def coverage(self) -> tuple[datetime, datetime] | None:
        """数据覆盖的时间范围 [起, 止)；规则型数据（如分时电价）为 None，表示任意时刻可用。"""
        return None

    def check(self, quantity: str) -> None:
        if quantity not in self.quantities:
            raise DatasetError(f"数据集 {self.name} 不提供 {quantity}（提供 {sorted(self.quantities)}）")


# ======================================================================= 中国：分时电价与省级排放因子


def _in(hour: float, ranges: list[list[float]] | None) -> bool:
    return any(a <= hour < b for a, b in (ranges or []))


@dataclass
class Tariff:
    key: str
    name: str
    source: str
    flat_price: float
    seasons: list[dict]

    def __post_init__(self) -> None:
        months = sorted(m for s in self.seasons for m in s.get("months", []))
        if months != list(range(1, 13)):
            raise ValueError(f"分时电价 {self.key} 的季节月份须恰好覆盖 1–12 月各一次，实际为 {months}")

    def season(self, month: int) -> dict:
        return next(s for s in self.seasons if month in s["months"])

    def price(self, t: datetime) -> tuple[float, str]:
        """返回 (电价 元/kWh, 时段名 sharp/peak/flat/valley/deep_valley)。时段按北京时间判定。"""
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


@register_dataset
class CnTouTariffs(GridSignalDataset):
    FILE = "grid/cn_tou_tariffs.yaml"
    quantities = frozenset({"price"})
    manifest = Manifest(
        name="cn-tou-tariffs", version="2026-11", kind="grid_signal",
        title="广东、贵州、内蒙古（蒙西）、上海工商业峰谷分时电价的时段结构与浮动比例",
        source="各省发展改革委文件（粤发改价格〔2021〕331号、黔发改价格〔2023〕481号、蒙西电网 2023-12-28 通知、沪发改价管〔2022〕50号）",
        license="政府公开信息", citation="见各省条目的 source 字段",
        bundled=(BundledFile(FILE, "4c1c08ca48c0321e16fee8c1009b863cd27d9f061eb60333adabbfbcecbf6f9c", "各省发展改革委分时电价文件"),),
        notes="平段电价 flat_price 为示意值，实际值随月度代理购电价格变化；浮动比例作用于整个平段电价（简化）。")

    def __init__(self, root=None, **params):
        super().__init__(root, **params)
        self.verify()
        with open(self.path(self.FILE), encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        self.tariffs = {k: Tariff(key=k, name=v.get("name", k), source=v.get("source", ""), flat_price=float(v["flat_price"]),
                                  seasons=v["seasons"]) for k, v in raw.items()}

    def regions(self) -> list[str]:
        return sorted(self.tariffs)

    def value(self, quantity, region, t):
        self.check(quantity)
        if region not in self.tariffs:
            raise SignalUnavailable(f"分时电价数据中没有省份 {region}，已收录：{self.regions()}")
        price, period = self.tariffs[region].price(t)
        return SignalValue(price, period)


@register_dataset
class CnProvincialCarbon2023(GridSignalDataset):
    FILE = "grid/cn_carbon_factors_2023.csv"
    quantities = frozenset({"carbon"})
    manifest = Manifest(
        name="cn-provincial-carbon-2023", version="mee-2025-47", kind="grid_signal",
        title="2023 年省级电力平均二氧化碳排放因子（30 个省份，年均值）",
        source="https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202512/t20251231_1139517.html",
        license="政府公开信息", citation="生态环境部, 国家统计局. 关于发布2023年电力二氧化碳排放因子的公告（公告2025年第47号）. 2025.",
        bundled=(BundledFile(FILE, "63133c2fa669c75c0214d1c2272d03db924eded079e96126411b0ec299741580", "公告2025年第47号 表3"),),
        notes="年均值，不随时间变化。")

    def __init__(self, root=None, **params):
        super().__init__(root, **params)
        self.verify()
        self.factors: dict[str, float] = {}
        with open(self.path(self.FILE), encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                self.factors[row["province"]] = float(row["factor_kg_per_kwh"])

    def regions(self) -> list[str]:
        return sorted(self.factors)

    def value(self, quantity, region, t):
        self.check(quantity)
        if region not in self.factors:
            raise SignalUnavailable(f"排放因子表中没有省份 {region}")
        return SignalValue(self.factors[region])


# ======================================================================= 美国：EIA-930 逐时发电结构


EIA_FUELS = {  # 参数键 → EIA-930 列名中的燃料名称
    "coal": "Coal", "natural_gas": "Natural Gas", "nuclear": "Nuclear", "petroleum": "All Petroleum Products",
    "hydro": "Hydropower and Pumped Storage", "solar": "Solar", "wind": "Wind", "other": "Other Fuel Sources",
    "unknown": "Unknown Fuel Sources",
}
EIA = "https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/"
EIA_SUMS = {
    "EIA930_BALANCE_2024_Jan_Jun.csv": ("26768c495c3ba987e29d79df2e40d4e741f9dbcff086f53c12ff774465756a10", 41742812),
}


@register_dataset
class Eia930Carbon2024(GridSignalDataset):
    """EIA-930 平衡区（balancing authority）逐时分燃料净发电量 × 分燃料排放因子 → 逐时发电碳强度（kgCO2/kWh）。

    参数：
      factors_g_per_kwh：各燃料的排放因子（gCO2eq/kWh），键见 EIA_FUELS，须显式给出（数据集本身不含排放数据）；
      series：reported（原始上报值）或 adjusted（EIA 清洗后的值），须显式给出。
    平衡区从未上报的燃料类别视为没有此类发电；某小时某平衡区：已上报的燃料类别发电量为空、有正发电量的燃料缺少排放因子、
    或总发电量不为正时，该小时的碳强度不可用。负的净发电量（如抽水蓄能抽水）不计入。
    时间对应：EIA-930 的小时以 UTC 结束时刻标注，区间 [结束时刻 − 1 h, 结束时刻) 的值取该小时。
    """

    FILES = ("EIA930_BALANCE_2024_Jan_Jun.csv",)  # 2024 年下半年的文件改用了更细的燃料分类，列结构不同，未纳入
    quantities = frozenset({"carbon"})
    manifest = Manifest(
        name="eia930-carbon-2024h1", version="2026-11-23", kind="grid_signal",
        title="EIA-930 Hourly Electric Grid Monitor：2024 年 1–6 月各平衡区逐时分燃料净发电量",
        source="https://www.eia.gov/electricity/gridmonitor/", license="美国联邦政府公开数据（public domain）",
        citation="U.S. Energy Information Administration. Hourly Electric Grid Monitor (Form EIA-930), Six-Month File 2024 Jan–Jun.",
        files=tuple(RemoteFile(f, EIA + f, *EIA_SUMS[f]) for f in FILES),
        notes="碳强度由分燃料发电量与参数给出的排放因子计算；EIA 会修订历史文件，版本号为下载时文件的 Last-Modified 日期。")

    def __init__(self, root=None, *, factors_g_per_kwh: dict[str, float], series: str, **params):
        super().__init__(root, **params)
        unknown = set(factors_g_per_kwh) - set(EIA_FUELS)
        if unknown:
            raise TypeError(f"未知燃料 {sorted(unknown)}，可选 {sorted(EIA_FUELS)}")
        if series not in ("reported", "adjusted"):
            raise TypeError("series 只能为 reported 或 adjusted")
        self.factors = dict(factors_g_per_kwh)
        self.series = series
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data

        def build():
            suffix = " (Adjusted)" if self.series == "adjusted" else ""
            ba, hours, gen = [], [], {k: [] for k in EIA_FUELS}
            for fn in self.FILES:
                with open(self.path(fn), encoding="utf-8-sig", newline="") as f:
                    r = csv.reader(f)
                    head = next(r)
                    col = {h: i for i, h in enumerate(head)}
                    i_ba, i_utc = col["Balancing Authority"], col["UTC Time at End of Hour"]
                    i_gen = {k: col[f"Net Generation (MW) from {v}{suffix}"] for k, v in EIA_FUELS.items()}
                    for row in r:
                        ba.append(row[i_ba])
                        hours.append(datetime.strptime(row[i_utc], "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=timezone.utc).timestamp())
                        for k, i in i_gen.items():
                            v = row[i].replace(",", "").strip()
                            gen[k].append(float(v) if v else np.nan)
            return {"ba": np.array(ba), "end": np.array(hours, dtype=np.int64), **{f"gen_{k}": np.array(v) for k, v in gen.items()}}

        a = self.derived(f"balance_{self.series}", build, list(self.FILES))
        gen = {k: a[f"gen_{k}"] for k in EIA_FUELS}
        # 平衡区从未上报的燃料类别视为该区没有此类发电（取 0）；曾上报过的类别在某小时为空，则该小时数据缺失
        reported: dict[str, set[str]] = {}
        for b in np.unique(a["ba"]):
            m = a["ba"] == b
            reported[str(b)] = {k for k, v in gen.items() if np.isfinite(v[m]).any()}
        self._data = {"ba": a["ba"], "end": a["end"], "gen": gen, "reported": reported,
                      "index": {(b, int(e)): i for i, (b, e) in enumerate(zip(a["ba"].tolist(), a["end"].tolist()))}}
        return self._data

    def regions(self) -> list[str]:
        return sorted(set(self._load()["ba"].tolist()))

    def coverage(self):
        d = self._load()
        lo, hi = int(d["end"].min()), int(d["end"].max())
        return (datetime.fromtimestamp(lo - 3600, timezone.utc), datetime.fromtimestamp(hi, timezone.utc))

    def value(self, quantity, region, t):
        self.check(quantity)
        d = self._load()
        ts = t.astimezone(timezone.utc).timestamp()
        end = int(math.floor(ts / 3600.0) * 3600 + 3600)
        i = d["index"].get((region, end))
        if i is None:
            raise SignalUnavailable(f"EIA-930 没有平衡区 {region} 在 {datetime.fromtimestamp(end, timezone.utc).isoformat()} 结束的小时")
        num = den = 0.0
        for k in d["reported"][region]:
            g = float(d["gen"][k][i])
            if math.isnan(g):
                raise SignalUnavailable(f"{region} 该小时 {k} 发电量缺失")
            if g > 0:
                if k not in self.factors:
                    raise SignalUnavailable(f"{region} 该小时有 {k} 发电（{g:.0f} MW），但未给出其排放因子")
                num += g * self.factors[k]
                den += g
        if den <= 0:
            raise SignalUnavailable(f"{region} 该小时总发电量不为正")
        return SignalValue(num / den / 1000.0)  # g/kWh → kg/kWh


# ======================================================================= 测试与示例用


@register_dataset
class SyntheticDiurnalCarbon(GridSignalDataset):
    """年均排放因子叠加日内形状（午间光伏出力高 → 偏低）：c(h) = c̄ × (1 + amplitude × s(h))，s 的日均值为 0。只用于测试与示例。"""

    quantities = frozenset({"carbon"})
    manifest = Manifest(name="synthetic-diurnal-carbon", version="1", kind="grid_signal",
                        title="合成碳强度：年均排放因子叠加日内正弦形状", source="cpc.datasets.grid.SyntheticDiurnalCarbon",
                        license="MIT", citation="", synthetic=True)

    def __init__(self, root=None, *, amplitude: float, **params):
        super().__init__(root, **params)
        self.base = CnProvincialCarbon2023(root)
        self.amplitude = amplitude

    @staticmethod
    def shape(hour: float) -> float:
        solar = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0)) if 6.0 <= hour <= 18.0 else 0.0
        return -(solar - 1.0 / math.pi)

    def regions(self):
        return self.base.regions()

    def value(self, quantity, region, t):
        c = self.base.value(quantity, region, t).value
        lt = t.astimezone(CST)
        return SignalValue(c * (1.0 + self.amplitude * self.shape(lt.hour + lt.minute / 60.0)))


@register_dataset
class FixedPrice(GridSignalDataset):
    """各区域固定电价（元/kWh，参数给定，非实测）。用于只研究碳强度、或电价取外部约定值的场景。"""

    quantities = frozenset({"price"})
    manifest = Manifest(name="fixed-price", version="1", kind="grid_signal", title="固定电价（参数给定）",
                        source="cpc.datasets.grid.FixedPrice", license="MIT", citation="", synthetic=True)

    def __init__(self, root=None, *, prices: dict[str, float], **params):
        super().__init__(root, **params)
        self.prices = {k: float(v) for k, v in prices.items()}

    def regions(self):
        return sorted(self.prices)

    def value(self, quantity, region, t):
        self.check(quantity)
        if region not in self.prices:
            raise SignalUnavailable(f"fixed-price 未给出区域 {region} 的电价")
        return SignalValue(self.prices[region], "fixed")
