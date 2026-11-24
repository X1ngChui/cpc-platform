"""EIA-930 逐时分燃料净发电量 × 分燃料排放因子 → grid_signal 数据集（carbon_intensity）。

原始数据：U.S. EIA Hourly Electric Grid Monitor（Form EIA-930）六个月文件，
    https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/EIA930_BALANCE_2024_Jan_Jun.csv（美国联邦政府公开数据）。
    2024 年下半年起的文件采用更细的燃料分类，列结构不同，本范例只处理 2024 年 1–6 月的列结构。

转换规则：
- 碳强度（kgCO2eq/kWh）= Σ(燃料 f 的净发电量 × 排放因子 f) / Σ 净发电量；排放因子由 --factors 显式给出（gCO2eq/kWh）；
- --series 选择 EIA 原始上报值（reported）或 EIA 调整值（adjusted）；
- 平衡区从未上报的燃料类别视为没有此类发电；某小时已上报的类别为空、有正发电量的燃料缺少排放因子、或总发电量不为正时，
  该小时不输出（数据集中为缺口，平台视为信号不可用）；负的净发电量（如抽水蓄能抽水）不计入；
- 时段：EIA-930 以 UTC 小时结束时刻标注，输出时段为 [结束时刻 − 1 h, 结束时刻)。

缺省排放因子：Electricity Maps 默认生命周期因子（config/defaults.yaml，提交 0b6b5c6）：煤 820、天然气 490、核电 12、
水电 24、光伏 45、风电 11（IPCC 2014），石油 650（UK POST 2014），未知燃料 700；"其他燃料"取生物质的 230（IPCC 2014），属于假设。

用法：python examples/datasets/eia930.py --series adjusted --regions CISO ERCO PJM [--raw 文件] [--out 目录]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pyarrow as pa
import pyarrow.csv as pacsv
from _util import SIGNAL_CORE, out_dir, raw_file

from cpc.datasets.build import write_dataset

URL = "https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/EIA930_BALANCE_2024_Jan_Jun.csv"
FUELS = {"coal": "Coal", "natural_gas": "Natural Gas", "nuclear": "Nuclear", "petroleum": "All Petroleum Products",
         "hydro": "Hydropower and Pumped Storage", "solar": "Solar", "wind": "Wind", "other": "Other Fuel Sources",
         "unknown": "Unknown Fuel Sources"}
DEFAULT_FACTORS = {"coal": 820, "natural_gas": 490, "nuclear": 12, "hydro": 24, "solar": 45, "wind": 11, "petroleum": 650,
                   "other": 230, "unknown": 700}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", required=True, choices=["reported", "adjusted"])
    ap.add_argument("--regions", nargs="*", default=None, help="平衡区代码；缺省为全部")
    ap.add_argument("--factors", default=None, help="排放因子 JSON（gCO2eq/kWh），缺省见说明")
    ap.add_argument("--raw", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    factors = json.loads(args.factors) if args.factors else DEFAULT_FACTORS
    unknown = set(factors) - set(FUELS)
    if unknown:
        raise SystemExit(f"未知燃料 {sorted(unknown)}，可选 {sorted(FUELS)}")
    suffix = " (Adjusted)" if args.series == "adjusted" else ""
    gen_cols = {k: f"Net Generation (MW) from {v}{suffix}" for k, v in FUELS.items()}
    path = raw_file(args.raw, "eia930", URL)
    t = pacsv.read_csv(path, convert_options=pacsv.ConvertOptions(
        include_columns=["Balancing Authority", "UTC Time at End of Hour", *gen_cols.values()],
        column_types={c: pa.string() for c in gen_cols.values()}))
    ba = np.array(t.column("Balancing Authority").to_pylist())
    ends = [datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=timezone.utc)
            for s in t.column("UTC Time at End of Hour").to_pylist()]
    gen = {}
    for k, c in gen_cols.items():
        vals = t.column(c).to_pylist()
        gen[k] = np.array([float(v.replace(",", "")) if v and v.strip() else np.nan for v in vals])
    regions = args.regions or sorted(set(ba.tolist()))
    rows = []
    for r in regions:
        m = np.nonzero(ba == r)[0]
        if not len(m):
            raise SystemExit(f"原始数据中没有平衡区 {r}")
        reported = [k for k in FUELS if np.isfinite(gen[k][m]).any()]
        for i in m:
            num = den = 0.0
            ok = True
            for k in reported:
                g = gen[k][i]
                if np.isnan(g) or (g > 0 and k not in factors):
                    ok = False
                    break
                if g > 0:
                    num += g * factors[k]
                    den += g
            if ok and den > 0:
                rows.append((r, ends[i] - timedelta(hours=1), ends[i], num / den / 1000.0))
    rows.sort(key=lambda x: (x[0], x[1]))
    ts = pa.timestamp("us", tz="UTC")
    table = pa.table({"region": [x[0] for x in rows], "start": pa.array([x[1] for x in rows], ts),
                      "end": pa.array([x[2] for x in rows], ts), "signal": ["carbon_intensity"] * len(rows),
                      "value": pa.array([x[3] for x in rows], pa.float64())})
    name = "eia930-carbon-2024h1"
    d = write_dataset(
        out_dir(args.out, name), name=name, version=f"2024h1-{args.series}", kind="grid_signal", table=table, fields=SIGNAL_CORE,
        title="EIA-930 各平衡区逐时发电碳强度（2024 年 1–6 月，按分燃料发电量与生命周期排放因子计算）",
        licenses=[{"name": "public domain（美国联邦政府公开数据）"}],
        sources=[{"title": "U.S. EIA Hourly Electric Grid Monitor", "path": "https://www.eia.gov/electricity/gridmonitor/"},
                 {"title": "raw", "path": URL}],
        cpc={"signals": {"carbon_intensity": {"unit": "kgCO2/kWh", "basis": "生命周期（CO2eq），发电侧，不含跨区交换"}},
             "citation": "U.S. Energy Information Administration. Hourly Electric Grid Monitor (Form EIA-930), Six-Month File 2024 Jan–Jun.",
             "conversion": {"script": "examples/datasets/eia930.py", "series": args.series, "factors_g_per_kwh": factors}})
    print(f"{d.ref}：{table.num_rows} 个时段，{len(regions)} 个平衡区 → {d.root}")


if __name__ == "__main__":
    main()
