"""用公开数据集检验平台：对每个已下载的公开负载数据集（以及美国 EIA 逐时碳强度场景）运行离散事件仿真。

用法：
    cpc data fetch azure-llm-2024-conv ...           # 先下载所需数据集（cpc data list 查看状态）
    python scripts/dataset_check.py [--out results/dataset-check] [--jobs 4]

以 configs/scenario.yaml 为基础，逐一替换负载数据集（时间窗取数据集起点起至多 24 小时），运行 nearest:static 与
cost_aware:reactive 两组策略；另以 configs/scenario.us-eia.yaml 运行同样两组。未下载的数据集跳过并在结果中注明。
输出 summary.json 与 summary.md（每组的请求数、SLO 达成率、TTFT P99、设施用电、电费、碳排放、信号覆盖率、路由原因）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cpc.config import load_scenario  # noqa: E402
from cpc.datasets import DatasetError, make_dataset  # noqa: E402
from cpc.datasets.base import file_view  # noqa: E402
from cpc.sched import make_capacity, make_routing  # noqa: E402
from cpc.sim.des import Simulator  # noqa: E402

COMBOS = [("nearest", "static"), ("cost_aware", "reactive")]
# (标签, 场景文件, 负载数据集, 数据集参数, 时间窗起点 s, 时间窗长度 s, 到达率缩放)
CASES = [
    ("azure-2024-conv（1 天）", "configs/scenario.yaml", None, {}, None, None, None),
    ("azure-2024-code（1 天）", "configs/scenario.yaml", "azure-llm-2024-code", {}, 230400, 86400, 1.0),
    ("azure-2023-conv（1 小时）", "configs/scenario.yaml", "azure-llm-2023-conv", {}, 0, 3400, 1.0),
    ("azure-2023-code（1 小时）", "configs/scenario.yaml", "azure-llm-2023-code", {}, 0, 3400, 1.0),
    ("burstgpt-1（第 10 天）", "configs/scenario.yaml", "burstgpt-1", {"include_failed": False}, 9 * 86400, 86400, 1.0),
    ("mooncake-conversation（1 小时）", "configs/scenario.yaml", "mooncake-fast25-conversation", {}, 0, 3500, 1.0),
    ("mooncake-toolagent（1 小时）", "configs/scenario.yaml", "mooncake-fast25-toolagent", {}, 0, 3500, 1.0),
    ("美国 EIA 逐时碳强度 + azure-2024-conv（1 天）", "configs/scenario.us-eia.yaml", None, {}, None, None, None),
]
KEYS = ["arrived", "served", "rejected", "unrouted", "slo_attainment", "ttft_p99_s", "facility_kwh", "cost_yuan", "carbon_kg",
        "wh_per_request", "signal_coverage"]


def run_case(case, routing: str, capacity: str) -> dict:
    label, scenario, dataset, params, start, dur, rate = case
    scn = load_scenario(ROOT / scenario)
    if dataset is not None:
        scn.workload.dataset, scn.workload.params = dataset, params
        scn.workload.start_s, scn.workload.duration_s, scn.workload.rate_scale = start, dur, rate
    hours = (scn.workload.duration_s or 86400) / scn.workload.time_scale / 3600
    t0 = time.time()
    try:
        sim = Simulator(scn, make_routing(routing, scn.scheduler.routing.params if routing == scn.scheduler.routing.policy else {}),
                        make_capacity(capacity, scn.scheduler.capacity.params if capacity == scn.scheduler.capacity.policy else {}),
                        hours=hours)
        s = sim.run().summary
    except DatasetError as e:
        return {"case": label, "routing": routing, "capacity": capacity, "error": str(e)}
    out = {"case": label, "routing": routing, "capacity": capacity, "hours": round(hours, 3), "wall_s": round(time.time() - t0, 1),
           "workload": s["workload"]["ref"], "grid": s["grid"], "route_reasons": s["route_reasons"],
           "skipped_cycles": s["skipped_cycles"], **{k: s[k] for k in KEYS}}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "results/dataset-check"))
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs, skipped = [], []
    for case in CASES:
        scn = load_scenario(ROOT / case[1])
        names = [case[2] or scn.workload.dataset, scn.grid.price.dataset, scn.grid.carbon.dataset]
        missing = [n for n in names if any(v != "ok" for v in file_view(n).status().values())]
        if missing:
            skipped.append({"case": case[0], "missing": missing})
            continue
        jobs += [(case, r, c) for r, c in COMBOS]
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        rows = list(ex.map(run_case, *zip(*jobs))) if jobs else []
    (out / "summary.json").write_text(json.dumps({"rows": rows, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["| 负载 | 策略 | 请求 | 拒绝 | SLO | TTFT P99 (s) | 设施用电 (kWh) | 电费 (元) | 碳排放 (kg) | Wh/请求 | 信号覆盖 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['case']} | {r['routing']}+{r['capacity']} | 错误：{r['error']} |||||||||")
            continue
        lines.append(f"| {r['case']} | {r['routing']}+{r['capacity']} | {r['served']} | {r['rejected'] + r['unrouted']} | "
                     f"{r['slo_attainment']:.4f} | {r['ttft_p99_s']:.3f} | {r['facility_kwh']:.1f} | {r['cost_yuan']:.1f} | "
                     f"{r['carbon_kg']:.1f} | {r['wh_per_request']:.4f} | {r['signal_coverage']} |")
    for s in skipped:
        lines.append(f"| {s['case']} | 跳过：未下载 {s['missing']} |||||||||")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
