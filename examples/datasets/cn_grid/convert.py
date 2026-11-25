"""Provincial time-of-use tariff rules and provincial emission factors of electricity in China -> grid_signal dataset.

Raw material (this directory):
    tou_tariffs.yaml           periods and multipliers of the commercial and industrial time-of-use tariffs of
                               Guangdong, Guizhou, western Inner Mongolia and Shanghai (documents of the provincial
                               development and reform commissions, see source of each entry); the flat price is
                               illustrative
    carbon_factors_2023.csv    table 3 of the announcement on the 2023 CO2 emission factors of electricity of the
                               Ministry of Ecology and Environment and the National Bureau of Statistics
                               (Announcement No. 47 of 2025): 2023 average factors of 30 provinces (kgCO2/kWh)
    source: https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202512/t20251231_1139517.html

Conversion:
- price: the tariff rules are expanded hour by hour over [--start, --end) in Beijing time; adjacent hours with the
  same price and period are merged; label is the period (sharp, peak, flat, valley, deep_valley);
- carbon_intensity: one period per province covering [--start, --end) with the 2023 annual factor. Using the
  2023 mean for other years is an explicit assumption, recorded in cpc.conversion of the descriptor.

Usage: python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01 [--provinces guangdong ...] [--out DIR]
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _util import SIGNAL_CORE, out_dir  # noqa: E402

from cpc.datasets.build import write_dataset  # noqa: E402

HERE = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
MEE = "https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202512/t20251231_1139517.html"


def _in(hour: float, ranges) -> bool:
    return any(a <= hour < b for a, b in (ranges or []))


def tariff_price(rule: dict, t: datetime) -> tuple[float, str]:
    """Returns (price CNY/kWh, period)."""
    h = t.hour + t.minute / 60.0
    season = next(s for s in rule["seasons"] if t.month in s["months"])
    periods = season.get("periods", {}) or {}
    mult = season.get("multipliers", {}) or {}
    if _in(h, periods.get("peak")):
        label, m = "peak", mult.get("peak", 1.0)
    elif _in(h, periods.get("valley")):
        label, m = "valley", mult.get("valley", 1.0)
    else:
        label, m = "flat", 1.0
    sharp = season.get("sharp")
    if sharp and t.month in sharp.get("months", season["months"]) and _in(h, sharp.get("hours")):
        label, m = "sharp", mult.get("peak", 1.0) * sharp["multiplier_over_peak"]
    deep = season.get("deep_valley")
    if deep and t.month in deep.get("months", season["months"]) and _in(h, deep.get("hours")):
        label, m = "deep_valley", mult.get("valley", 1.0) * deep["multiplier_over_valley"]
    return round(float(rule["flat_price"]) * m, 6), label


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="start date (Beijing time), e.g. 2026-01-01")
    ap.add_argument("--end", required=True, help="end date (exclusive)")
    ap.add_argument("--provinces", nargs="*", default=None, help="default: all provinces of the tariff rules")
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default=None, help="dataset name (default cn-grid-<start year>)")
    args = ap.parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=CST)
    end = datetime.fromisoformat(args.end).replace(tzinfo=CST)
    tariffs = yaml.safe_load((HERE / "tou_tariffs.yaml").read_text(encoding="utf-8"))
    for key, rule in tariffs.items():
        months = sorted(m for s in rule["seasons"] for m in s["months"])
        if months != list(range(1, 13)):
            sys.exit(f"the seasons of tariff {key} must cover months 1-12 exactly once, got {months}")
    with open(HERE / "carbon_factors_2023.csv", encoding="utf-8-sig") as f:
        factors = {r["province"]: float(r["factor_kg_per_kwh"]) for r in csv.DictReader(f)}
    provinces = args.provinces or sorted(tariffs)
    rows: list[tuple] = []
    for p in provinces:
        if p not in tariffs or p not in factors:
            sys.exit(f"province {p} lacks tariff rules or an emission factor")
        t, cur = start, None
        while t < end:
            price, label = tariff_price(tariffs[p], t)
            if cur is not None and cur[3] == price and cur[4] == label:
                cur[1] = t + timedelta(hours=1)
            else:
                if cur is not None:
                    rows.append((p, cur[0], cur[1], "price", cur[3], cur[4]))
                cur = [t, t + timedelta(hours=1), None, price, label]
            t += timedelta(hours=1)
        rows.append((p, cur[0], cur[1], "price", cur[3], cur[4]))
        rows.append((p, start, end, "carbon_intensity", factors[p], None))
    ts = pa.timestamp("us", tz="UTC")
    table = pa.table({
        "region": [r[0] for r in rows], "start": pa.array([r[1] for r in rows], ts), "end": pa.array([r[2] for r in rows], ts),
        "signal": [r[3] for r in rows], "value": pa.array([r[4] for r in rows], pa.float64()), "label": [r[5] for r in rows],
    })
    fields = SIGNAL_CORE + [{"name": "label", "type": "string", "description": "time-of-use period"}]
    name = args.name or f"cn-grid-{start.year}"
    d = write_dataset(
        out_dir(args.out, name), name=name, version=f"{args.start}_{args.end}", kind="grid_signal", table=table, fields=fields,
        title="Provincial time-of-use electricity prices in China (expanded rules) and 2023 provincial average CO2 emission factors of electricity",
        licenses=[{"name": "public government information"}],
        sources=[{"title": "time-of-use tariff documents of the provincial development and reform commissions (see source in tou_tariffs.yaml)"},
                 {"title": "Ministry of Ecology and Environment, National Bureau of Statistics: Announcement No. 47 of 2025", "path": MEE}],
        cpc={"signals": {"price": {"unit": "CNY/kWh", "note": "the flat price is illustrative; the multipliers apply to the whole flat price"},
                         "carbon_intensity": {"unit": "kgCO2/kWh", "basis": "average CO2 emission factor of electricity (2023 annual mean)"}},
             "citation": "Ministry of Ecology and Environment, National Bureau of Statistics. Announcement on the 2023 CO2 emission factors of electricity (Announcement No. 47 of 2025). 2025.",
             "conversion": {"script": "examples/datasets/cn_grid/convert.py", "start": start.isoformat(), "end": end.isoformat(),
                            "assumption": "the 2023 provincial annual factor is the constant carbon intensity over the converted period"}},
        fmt="csv")
    print(f"{d.ref}: {table.num_rows} rows, {len(provinces)} provinces -> {d.root}")


if __name__ == "__main__":
    main()
