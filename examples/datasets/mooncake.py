"""Mooncake FAST'25 trace → workload 数据集。

原始数据（JSONL，字段 timestamp（毫秒）、input_length、output_length、hash_ids）：
    https://github.com/kvcache-ai/Mooncake/tree/main/FAST25-release/traces（提交 3cca71d）
    conversation_trace.jsonl / toolagent_trace.jsonl / synthetic_trace.jsonl。许可 Apache-2.0。
引用：Qin R, Li Z, He W, et al. Mooncake: Trading More Storage for Less Computation — A KVCache-centric Architecture
for Serving LLM Chatbot. FAST 2025.

转换规则：按 timestamp 排序，arrival_s = (timestamp − 最早的 timestamp) / 1000；hash_ids 作为约定扩展字段 prefix_hashes
（整数列表，每个元素对应一个 512 token 的前缀块）；数据集未给出绝对时间，描述文件不含 cpc.origin。

用法：python examples/datasets/mooncake.py --trace conversation [--raw 文件] [--out 目录] --zero-output fail
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pyarrow as pa
from _util import WORKLOAD_CORE, out_dir, raw_file, zero_output

from cpc.datasets.build import write_dataset

BASE = "https://raw.githubusercontent.com/kvcache-ai/Mooncake/3cca71daccf2a7afb8fe3f0295358f70e3a69fdb/FAST25-release/traces/"
CITE = ("Qin R, Li Z, He W, et al. Mooncake: Trading More Storage for Less Computation — A KVCache-centric Architecture for "
        "Serving LLM Chatbot. FAST 2025.")
TITLES = {"conversation": "对话服务 1 小时采样", "toolagent": "工具与智能体服务 1 小时采样", "synthetic": "由公开数据集构造、泊松到达的合成负载"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trace", required=True, choices=sorted(TITLES))
    ap.add_argument("--raw", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--zero-output", choices=["fail", "drop"], required=True)
    args = ap.parse_args()
    url = BASE + f"{args.trace}_trace.jsonl"
    rows = []
    with open(raw_file(args.raw, "mooncake", url), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                rows.append((d["timestamp"], d["input_length"], d["output_length"], d["hash_ids"]))
    rows.sort(key=lambda r: r[0])
    n_zero = sum(1 for r in rows if r[2] < 1)
    zero_output(n_zero, args.zero_output, url)
    rows = [r for r in rows if r[2] >= 1]
    ts = np.array([r[0] for r in rows], dtype=float)
    table = pa.table({
        "arrival_s": (ts - ts[0]) / 1000.0,
        "input_tokens": pa.array([r[1] for r in rows], pa.int64()),
        "output_tokens": pa.array([r[2] for r in rows], pa.int64()),
        "prefix_hashes": pa.array([r[3] for r in rows], pa.list_(pa.int64())),
    })
    fields = WORKLOAD_CORE + [{"name": "prefix_hashes", "type": "list", "itemType": "integer",
                               "description": "前缀块哈希序列（每块 512 token）"}]
    name = f"mooncake-fast25-{args.trace}"
    d = write_dataset(out_dir(args.out, name), name=name, version="3cca71d", kind="workload", table=table, fields=fields,
                      title=f"Mooncake FAST'25：{TITLES[args.trace]}", licenses=[{"name": "Apache-2.0"}],
                      sources=[{"title": "Mooncake", "path": "https://github.com/kvcache-ai/Mooncake"}, {"title": "raw", "path": url}],
                      cpc={"citation": CITE, "conversion": {"script": "examples/datasets/mooncake.py", "zero_output": args.zero_output,
                                                            "dropped_zero_output": n_zero}})
    print(f"{d.ref}：{table.num_rows} 个请求 → {d.root}")


if __name__ == "__main__":
    main()
