"""BurstGPT（v2.0，BurstGPT_1.csv）→ workload 数据集。

原始数据：https://github.com/HPMLL/BurstGPT/releases/download/v2.0/BurstGPT_1.csv（前 2 个月的 ChatGPT 与 GPT-4 请求），
列 Timestamp（自首日 0:00 起的秒数）、Model、Request tokens、Response tokens、Total tokens、Log Type。许可 CC-BY-4.0。
引用：Wang Y, Chen Y, Li Z, et al. BurstGPT: A Real-World Workload Dataset to Optimize LLM Serving Systems. KDD 2025.

转换规则：
- 按 Timestamp 排序，arrival_s = Timestamp − 最早的 Timestamp；数据集未给出绝对日期，描述文件不含 cpc.origin；
- 扩展字段：model（约定扩展）、log_type（任意扩展：Conversation log / API log）；
- 响应 token 数为 0 的记录（数据集说明中的失败请求）按 --zero-output 处理。

用法：python examples/datasets/burstgpt.py --zero-output drop [--raw 文件] [--out 目录]
"""

from __future__ import annotations

import argparse

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from _util import WORKLOAD_CORE, out_dir, raw_file, zero_output

from cpc.datasets.build import write_dataset

URL = "https://github.com/HPMLL/BurstGPT/releases/download/v2.0/BurstGPT_1.csv"
CITE = ("Wang Y, Chen Y, Li Z, et al. BurstGPT: A Real-World Workload Dataset to Optimize LLM Serving Systems. KDD 2025. "
        "doi:10.1145/3711896.3737413")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--zero-output", choices=["fail", "drop"], required=True)
    args = ap.parse_args()
    t = pacsv.read_csv(raw_file(args.raw, "burstgpt", URL))
    t = t.take(pc.sort_indices(t, [("Timestamp", "ascending")]))
    out = t.column("Response tokens").to_numpy()
    zero = out < 1
    zero_output(int(zero.sum()), args.zero_output, URL)
    t = t.filter(pa.array(~zero))
    ts = t.column("Timestamp").to_numpy().astype(float)
    table = pa.table({
        "arrival_s": ts - ts[0],
        "input_tokens": t.column("Request tokens").cast(pa.int64()),
        "output_tokens": t.column("Response tokens").cast(pa.int64()),
        "model": t.column("Model").cast(pa.string()),
        "log_type": t.column("Log Type").cast(pa.string()),
    })
    fields = WORKLOAD_CORE + [{"name": "model", "type": "string", "description": "服务的模型（ChatGPT / GPT-4）"},
                              {"name": "log_type", "type": "string", "description": "日志类型：Conversation log / API log"}]
    name = "burstgpt-1"
    d = write_dataset(out_dir(args.out, name), name=name, version="v2.0", kind="workload", table=table, fields=fields,
                      title="BurstGPT：ChatGPT 与 GPT-4 服务的请求 trace（前 2 个月）",
                      licenses=[{"name": "CC-BY-4.0"}], sources=[{"title": "BurstGPT", "path": "https://github.com/HPMLL/BurstGPT"},
                                                                {"title": "raw", "path": URL}],
                      cpc={"citation": CITE, "conversion": {"script": "examples/datasets/burstgpt.py", "zero_output": args.zero_output,
                                                            "dropped_zero_output": int(zero.sum())}})
    print(f"{d.ref}：{table.num_rows} 个请求 → {d.root}")


if __name__ == "__main__":
    main()
