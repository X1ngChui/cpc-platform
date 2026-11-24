"""Azure LLM Inference Trace（2023、2024）→ workload 数据集。

原始数据（CSV，列 TIMESTAMP, ContextTokens, GeneratedTokens）：
    2023-conv  AzureLLMInferenceTrace_conv.csv        2023-11-16 采样 1 小时，对话服务
    2023-code  AzureLLMInferenceTrace_code.csv        同上，代码补全服务
    2024-conv  AzureLLMInferenceTrace_conv_1week.csv  2024-05-12 起 1 周（UTC），对话服务
    2024-code  AzureLLMInferenceTrace_code_1week.csv  同上，代码补全服务
许可 CC-BY-4.0。引用：2023 版 Splitwise（ISCA 2024）；2024 版 DynamoLLM（HPCA 2025）。

转换规则：
- 按时间戳排序；cpc.origin 取最早的时间戳，arrival_s 为相对秒数；
- 2023 版的时间戳未注明时区，按 UTC 解释（转换参数 --naive-tz 显式给出）；2024 版带 +00:00；
- 输出 token 数为 0 的请求按 --zero-output 处理（fail：报错；drop：剔除并在描述文件中记录数量）。

用法：
    python examples/datasets/azure_llm.py --variant 2024-conv [--raw 文件] [--out 目录] --zero-output fail
输出缺省写到 ${CPC_DATA_DIR}/azure-llm-<variant>。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
from _util import WORKLOAD_CORE, out_dir, raw_file, zero_output

from cpc.datasets.build import write_dataset

A23 = "https://raw.githubusercontent.com/Azure/AzurePublicDataset/790921015d50dd6aae7f7e47f39ba0e235ad6b08/data/"
A24 = "https://github.com/Azure/AzurePublicDataset/releases/download/dataset-llm-2024/"
DOC = "https://github.com/Azure/AzurePublicDataset/blob/master/"
CITE23 = "Patel P, Choukse E, Zhang C, et al. Splitwise: Efficient generative LLM inference using phase splitting. ISCA 2024."
CITE24 = ("Stojkovic J, Zhang C, Goiri Í, et al. DynamoLLM: Designing LLM Inference Clusters for Performance and Energy "
          "Efficiency. HPCA 2025.")
VARIANTS = {
    "2023-conv": (A23 + "AzureLLMInferenceTrace_conv.csv", "Azure LLM Inference Trace 2023（对话服务，1 小时）", "2023", CITE23),
    "2023-code": (A23 + "AzureLLMInferenceTrace_code.csv", "Azure LLM Inference Trace 2023（代码补全服务，1 小时）", "2023", CITE23),
    "2024-conv": (A24 + "AzureLLMInferenceTrace_conv_1week.csv", "Azure LLM Inference Trace 2024（对话服务，1 周）", "2024", CITE24),
    "2024-code": (A24 + "AzureLLMInferenceTrace_code_1week.csv", "Azure LLM Inference Trace 2024（代码补全服务，1 周）", "2024", CITE24),
}


def parse(path, naive_tz: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t = pacsv.read_csv(path, convert_options=pacsv.ConvertOptions(
        column_types={"TIMESTAMP": pa.string(), "ContextTokens": pa.int64(), "GeneratedTokens": pa.int64()}))
    ts = t.column("TIMESTAMP")
    with_tz = pc.ends_with(ts, "+00:00")
    n_tz = pc.sum(with_tz).as_py() or 0
    if 0 < n_tz < len(ts):
        raise SystemExit(f"{path}：部分时间戳带时区、部分不带，无法统一解释")
    if n_tz == 0 and naive_tz != "UTC":
        raise SystemExit("当前只支持把未注明时区的时间戳按 UTC 解释（--naive-tz UTC）")
    ts = pc.replace_substring(ts, "+00:00", "")
    ts = pc.utf8_slice_codeunits(ts, 0, 26)  # 2023 版小数部分为 7 位，截断到微秒
    us = ts.cast(pa.timestamp("us")).cast(pa.int64()).to_numpy()
    return us, t.column("ContextTokens").to_numpy(), t.column("GeneratedTokens").to_numpy()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", required=True, choices=sorted(VARIANTS))
    ap.add_argument("--raw", default=None, help="原始 CSV；缺省时下载")
    ap.add_argument("--out", default=None)
    ap.add_argument("--naive-tz", default="UTC", help="未注明时区的时间戳的解释（只支持 UTC）")
    ap.add_argument("--zero-output", choices=["fail", "drop"], required=True, help="输出 token 数为 0 的请求的处理方式")
    args = ap.parse_args()
    url, title, year, cite = VARIANTS[args.variant]
    version = "7909210" if year == "2023" else "dataset-llm-2024"  # 原始数据的提交或发布标签
    us, n_in, n_out = parse(raw_file(args.raw, f"azure-llm-{year}", url), args.naive_tz)
    order = np.argsort(us, kind="stable")
    us, n_in, n_out = us[order], n_in[order], n_out[order]
    zero = n_out < 1
    zero_output(int(zero.sum()), args.zero_output, url)
    us, n_in, n_out = us[~zero], n_in[~zero], n_out[~zero]
    origin = datetime.fromtimestamp(int(us[0]) / 1e6, timezone.utc)
    table = pa.table({"arrival_s": (us - us[0]) / 1e6, "input_tokens": n_in, "output_tokens": n_out})
    name = f"azure-llm-{args.variant}"
    d = write_dataset(
        out_dir(args.out, name), name=name, version=version, kind="workload", table=table, fields=WORKLOAD_CORE,
        title=title, licenses=[{"name": "CC-BY-4.0", "path": "https://creativecommons.org/licenses/by/4.0/"}],
        sources=[{"title": "Azure Public Dataset", "path": DOC + f"AzureLLMInferenceDataset{year}.md"}, {"title": "raw", "path": url}],
        cpc={"origin": origin.isoformat(), "citation": cite,
             "conversion": {"script": "examples/datasets/azure_llm.py", "naive_tz": args.naive_tz,
                            "zero_output": args.zero_output, "dropped_zero_output": int(zero.sum())}})
    print(f"{d.ref}：{table.num_rows} 个请求 → {d.root}")


if __name__ == "__main__":
    main()
