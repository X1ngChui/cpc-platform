"""ML.ENERGY Benchmark v3 (LLM run summaries) -> power_profile dataset.

Raw data: https://huggingface.co/datasets/ml-energy/benchmark-v3 (commit f4db020, file runs/llm.parquet), Apache-2.0.
Access on Hugging Face is gated: request access on the dataset page and provide the token as HF_TOKEN.
Citation: Chung J-W, Ma J J, Wu R, et al. The ML.ENERGY Benchmark: Toward Automated Inference Energy Measurement and
Optimization. NeurIPS Datasets and Benchmarks, 2025.

Fields: gpu_model -> device, num_gpus -> device_count, model_id -> model, avg_batch_size -> concurrency,
avg_power_watts -> power_w, mean_itl_ms / 1000 -> itl_s; extensions max_concurrency (max_num_seqs),
throughput_tokens_per_s, energy_per_token_j, task. Only runs with is_stable are kept by default
(--include-unstable keeps all and adds the field is_stable).

Usage: python examples/datasets/mlenergy.py [--raw llm.parquet] [--out DIR]
"""

from __future__ import annotations

import argparse

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from _util import out_dir, raw_file

from cpc.datasets.build import write_dataset

URL = "https://huggingface.co/datasets/ml-energy/benchmark-v3/resolve/f4db020a045a92cc212199f889e57d55f9fa6391/runs/llm.parquet"
CITE = ("Chung J-W, Ma J J, Wu R, et al. The ML.ENERGY Benchmark: Toward Automated Inference Energy Measurement and "
        "Optimization. NeurIPS Datasets and Benchmarks, 2025.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--include-unstable", action="store_true")
    args = ap.parse_args()
    t = pq.read_table(raw_file(args.raw, "mlenergy-benchmark-v3", URL, token_env="HF_TOKEN"))
    if not args.include_unstable:
        t = t.filter(pc.equal(t.column("is_stable"), True))
    cols = {
        "device": t.column("gpu_model").cast(pa.string()),
        "device_count": t.column("num_gpus").cast(pa.int64()),
        "model": t.column("model_id").cast(pa.string()),
        "concurrency": t.column("avg_batch_size").cast(pa.float64()),
        "power_w": t.column("avg_power_watts").cast(pa.float64()),
        "itl_s": pc.divide(t.column("mean_itl_ms").cast(pa.float64()), 1000.0),
        "max_concurrency": t.column("max_num_seqs").cast(pa.int64()),
        "throughput_tokens_per_s": t.column("output_throughput_tokens_per_sec").cast(pa.float64()),
        "energy_per_token_j": t.column("energy_per_token_joules").cast(pa.float64()),
        "task": t.column("task").cast(pa.string()),
    }
    if args.include_unstable:
        cols["is_stable"] = t.column("is_stable").cast(pa.bool_())
    table = pa.table(cols)
    fields = [
        {"name": "device", "type": "string"}, {"name": "device_count", "type": "integer", "constraints": {"minimum": 1}},
        {"name": "model", "type": "string"}, {"name": "concurrency", "type": "number", "description": "mean batch size"},
        {"name": "power_w", "type": "number", "unit": "W", "description": "mean total power of all GPUs"},
        {"name": "itl_s", "type": "number", "unit": "s", "description": "mean inter-token latency"},
        {"name": "max_concurrency", "type": "integer", "description": "vLLM max_num_seqs"},
        {"name": "throughput_tokens_per_s", "type": "number"}, {"name": "energy_per_token_j", "type": "number"},
        {"name": "task", "type": "string"},
    ] + ([{"name": "is_stable", "type": "boolean"}] if args.include_unstable else [])
    name = "mlenergy-benchmark-v3-llm"
    d = write_dataset(out_dir(args.out, name), name=name, version="f4db020", kind="power_profile", table=table, fields=fields,
                      title="ML.ENERGY Benchmark v3: power, latency and throughput of LLM inference runs (NVIDIA H100, B200)",
                      licenses=[{"name": "Apache-2.0"}],
                      sources=[{"title": "ML.ENERGY Benchmark", "path": "https://huggingface.co/datasets/ml-energy/benchmark-v3"}],
                      cpc={"citation": CITE, "conversion": {"script": "examples/datasets/mlenergy.py",
                                                            "stable_only": not args.include_unstable}})
    print(f"{d.ref}: {table.num_rows} measurements -> {d.root}")


if __name__ == "__main__":
    main()
