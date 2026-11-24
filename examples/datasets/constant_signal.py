"""由显式给定的数值生成恒定信号的 grid_signal 数据集。

用于场景中某种信号没有实测数据、需要取约定值的情形（例如只研究碳强度时的统一电价）。数值为参数，不是实测数据，
描述文件的 cpc.conversion 中记录全部参数。

用法：
    python examples/datasets/constant_signal.py --name us-flat-price --signal price --unit USD/kWh \\
        --values CISO=0.06 ERCO=0.06 PJM=0.06 --start 2024-01-01T00:00:00Z --end 2025-01-01T00:00:00Z [--out 目录]
"""

from __future__ import annotations

import argparse
from datetime import datetime

import pyarrow as pa
from _util import SIGNAL_CORE, out_dir

from cpc.datasets.build import write_dataset


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--signal", required=True)
    ap.add_argument("--unit", required=True)
    ap.add_argument("--values", nargs="+", required=True, metavar="区域=值")
    ap.add_argument("--start", required=True, help="RFC 3339，须带时区")
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise SystemExit("--start、--end 须带时区，且 end 晚于 start")
    values = {k: float(v) for k, v in (x.split("=", 1) for x in args.values)}
    ts = pa.timestamp("us", tz="UTC")
    table = pa.table({"region": list(values), "start": pa.array([start] * len(values), ts), "end": pa.array([end] * len(values), ts),
                      "signal": [args.signal] * len(values), "value": pa.array(list(values.values()), pa.float64())})
    d = write_dataset(out_dir(args.out, args.name), name=args.name, version="1", kind="grid_signal", table=table, fields=SIGNAL_CORE,
                      title=f"恒定信号 {args.signal}（参数给定，非实测）", licenses=[{"name": "CC0-1.0"}],
                      cpc={"signals": {args.signal: {"unit": args.unit, "note": "参数给定的恒定值，非实测"}},
                           "conversion": {"script": "examples/datasets/constant_signal.py", "values": values,
                                          "start": start.isoformat(), "end": end.isoformat()}},
                      fmt="csv")
    print(f"{d.ref} → {d.root}")


if __name__ == "__main__":
    main()
