"""中国省级分时电价规则与省级电力排放因子 → grid_signal 数据集。

原始资料（本目录）：
    tou_tariffs.yaml           广东、贵州、内蒙古（蒙西）、上海工商业峰谷分时电价的时段结构与浮动比例（各省发展改革委文件，
                               出处见各条目的 source）；平段电价为示意值
    carbon_factors_2023.csv    生态环境部、国家统计局《关于发布2023年电力二氧化碳排放因子的公告》（公告2025年第47号）表 3，
                               30 个省份的 2023 年电力平均二氧化碳排放因子（kgCO2/kWh）
    来源：https://www.mee.gov.cn/xxgk2018/xxgk/xxgk01/202512/t20251231_1139517.html

转换规则：
- price：在 [--start, --end) 内按北京时间逐小时展开分时电价规则，相邻且电价与时段相同的小时合并为一个时段；
  label 为时段名（sharp、peak、flat、valley、deep_valley）；
- carbon_intensity：每个省份一个覆盖 [--start, --end) 的时段，取 2023 年年均排放因子。
  把 2023 年的年均值用于其他年份是一项显式假设，记录在描述文件的 cpc.conversion 中。

用法：python examples/datasets/cn_grid/convert.py --start 2026-01-01 --end 2027-01-01 [--provinces guangdong ...] [--out 目录]
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
    """返回 (电价 CNY/kWh, 时段名)。"""
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
    ap.add_argument("--start", required=True, help="起始日期（北京时间），如 2026-01-01")
    ap.add_argument("--end", required=True, help="结束日期（不含）")
    ap.add_argument("--provinces", nargs="*", default=None, help="缺省为分时电价规则中的全部省份")
    ap.add_argument("--out", default=None)
    ap.add_argument("--name", default=None, help="数据集名（缺省 cn-grid-<起始年>）")
    args = ap.parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=CST)
    end = datetime.fromisoformat(args.end).replace(tzinfo=CST)
    tariffs = yaml.safe_load((HERE / "tou_tariffs.yaml").read_text(encoding="utf-8"))
    for key, rule in tariffs.items():
        months = sorted(m for s in rule["seasons"] for m in s["months"])
        if months != list(range(1, 13)):
            sys.exit(f"分时电价 {key} 的季节月份须恰好覆盖 1–12 月各一次，实际为 {months}")
    with open(HERE / "carbon_factors_2023.csv", encoding="utf-8-sig") as f:
        factors = {r["province"]: float(r["factor_kg_per_kwh"]) for r in csv.DictReader(f)}
    provinces = args.provinces or sorted(tariffs)
    rows: list[tuple] = []
    for p in provinces:
        if p not in tariffs or p not in factors:
            sys.exit(f"省份 {p} 缺少分时电价规则或排放因子")
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
    fields = SIGNAL_CORE + [{"name": "label", "type": "string", "description": "分时电价时段名"}]
    name = args.name or f"cn-grid-{start.year}"
    d = write_dataset(
        out_dir(args.out, name), name=name, version=f"{args.start}_{args.end}", kind="grid_signal", table=table, fields=fields,
        title="中国省级分时电价（规则展开）与 2023 年省级电力平均二氧化碳排放因子",
        licenses=[{"name": "政府公开信息"}],
        sources=[{"title": "各省发展改革委分时电价文件（见 tou_tariffs.yaml 的 source）"},
                 {"title": "生态环境部 国家统计局 公告2025年第47号", "path": MEE}],
        cpc={"signals": {"price": {"unit": "CNY/kWh", "note": "平段电价为示意值，浮动比例作用于整个平段电价"},
                         "carbon_intensity": {"unit": "kgCO2/kWh", "basis": "电力平均二氧化碳排放因子（2023 年年均）"}},
             "citation": "生态环境部, 国家统计局. 关于发布2023年电力二氧化碳排放因子的公告（公告2025年第47号）. 2025.",
             "conversion": {"script": "examples/datasets/cn_grid/convert.py", "start": start.isoformat(), "end": end.isoformat(),
                            "assumption": "以 2023 年省级年均排放因子作为转换时间范围内的恒定碳强度"}},
        fmt="csv")
    print(f"{d.ref}：{table.num_rows} 行，{len(provinces)} 个省份 → {d.root}")


if __name__ == "__main__":
    main()
