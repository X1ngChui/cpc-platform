"""运行结果数据包与实验描述。

一次运行（仿真 cpc sim / cpc run，或实验 cpc live）的结果写入一个目录，构成一个 Data Package：
    datapackage.yaml   描述文件：资源、字段与出处（cpc.kind = run_result）
    run.yaml           运行描述：运行方式（simulation / live）、场景（展开后的完整内容）、策略与参数、时长、平台版本、创建时间
    summary.json       汇总指标
    timeseries.csv     按时间片的各站点 IT 功率、在线服务器数、到达率与电网信号
    requests.parquet   逐请求记录（到达时间、来源、站点、状态、路由原因、时延、是否满足 SLO）
    decisions.jsonl    容量策略的逐周期决策

实验描述（cpc run）：一个场景 + 若干变体，每个变体覆盖策略或负载参数；各变体独立运行，结果写入 <out>/<变体名>/，
对比表写入 <out>/comparison.csv。
"""

from __future__ import annotations

import csv
import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import __version__
from .config import ConfigError, PolicyConfig, Scenario, describe_validation, read_yaml

@dataclass
class RunResult:
    """一次运行的结果：汇总、时间序列（每个时间片一行）、逐请求记录（列名 → 数组）与容量决策。"""

    summary: dict
    timeseries: list[dict]
    requests: dict[str, np.ndarray]
    decisions: list[dict]


def _cell(v: str) -> float | str | None:
    """时间序列 CSV 的单元格：空为 None，数值为 float，其余（如 time 列）保持字符串。"""
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return v


def read_run(path: str | Path) -> dict:
    """读取结果数据包：返回 summary（dict）、run（dict）、requests（pyarrow.Table）、timeseries（行列表）、decisions（列表）。

    timeseries 的每行是 dict：数值列为 float，空值（如没有功率上限、读数缺失）为 None，time 列为字符串。
    """
    d = Path(path)
    if not (d / "summary.json").is_file():
        raise FileNotFoundError(f"{d} 不是结果目录（缺少 summary.json）")
    with open(d / "timeseries.csv", encoding="utf-8") as f:
        ts = [{k: _cell(v) for k, v in row.items()} for row in csv.DictReader(f)]
    decisions = [json.loads(x) for x in (d / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return {"summary": json.loads((d / "summary.json").read_text(encoding="utf-8")),
            "run": yaml.safe_load((d / "run.yaml").read_text(encoding="utf-8")),
            "requests": pq.read_table(d / "requests.parquet"), "timeseries": ts, "decisions": decisions}


COMPARE_KEYS = ["variant", "routing", "capacity", "arrived", "served", "rejected", "unrouted", "slo_attainment", "ttft_p99_s",
                "facility_kwh", "cost", "currency", "carbon_kg", "wh_per_request", "signal_coverage"]


class Variant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    routing: PolicyConfig | None = None  # 缺省为场景 scheduler.routing
    capacity: PolicyConfig | None = None  # 缺省为场景 scheduler.capacity
    workload: dict[str, Any] = Field(default_factory=dict)  # 覆盖场景 workload 的字段（如 rate_scale、seed）


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    scenario: str  # 相对于实验描述文件
    hours: float = Field(24.0, gt=0)
    bin_s: float = Field(300.0, gt=0)
    variants: list[Variant]
    base_dir: Path = Field(default=Path("."), exclude=True)

    def scenario_path(self) -> Path:
        p = Path(self.scenario)
        return p if p.is_absolute() else (self.base_dir / p).resolve()


def load_experiment(path: str | Path) -> Experiment:
    path = Path(path).resolve()
    raw = read_yaml(path, "实验描述")
    try:
        exp = Experiment.model_validate({**raw, "base_dir": path.parent})
    except ValidationError as e:
        raise ConfigError(describe_validation(e, Experiment, f"实验描述 {path}")) from e
    names = [v.name for v in exp.variants]
    if not names or len(set(names)) != len(names):
        raise ConfigError(f"实验描述 {path}：variants 须非空且名称不重复")
    return exp


def _fields(table: pa.Table) -> list[dict]:
    out = []
    for f in table.schema:
        t = f.type
        typ = ("integer" if pa.types.is_integer(t) else "number" if pa.types.is_floating(t) else
               "boolean" if pa.types.is_boolean(t) else "string")
        out.append({"name": f.name, "type": typ})
    return out


def write_run(out: str | Path, res, scn: Scenario, run: dict) -> Path:
    """写出一次运行的结果数据包。res 为 RunResult；run 为运行描述（运行方式、策略、时长等）。"""
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(res.summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(d / "timeseries.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(res.timeseries[0]))
        w.writeheader()
        w.writerows(res.timeseries)
    with open(d / "decisions.jsonl", "w", encoding="utf-8") as f:
        for r in res.decisions:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cols = {k: (pa.array(v.tolist(), type=pa.string()) if isinstance(v, np.ndarray) and v.dtype == object else pa.array(v))
            for k, v in res.requests.items()}
    req = pa.table(cols)
    pq.write_table(req, d / "requests.parquet")
    meta = {"platform": f"cpc-platform {__version__}", "python": platform.python_version(),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), **run,
            "scenario": scn.model_dump(mode="json")}
    (d / "run.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ts_fields = [{"name": k, "type": "string" if k == "time" else "number"} for k in res.timeseries[0]]
    desc = {
        "name": str(run.get("name", d.name)).lower().replace(" ", "-"),
        "created": meta["created"],
        "cpc": {"kind": "run_result", "profile": 1, "run": "run.yaml", "workload": res.summary.get("workload", {}).get("ref"),
                "grid": res.summary.get("grid")},
        "resources": [
            {"name": "requests", "type": "table", "path": "requests.parquet", "format": "parquet", "schema": {"fields": _fields(req)}},
            {"name": "timeseries", "type": "table", "path": "timeseries.csv", "format": "csv", "schema": {"fields": ts_fields}},
            {"name": "summary", "path": "summary.json", "format": "json"},
            {"name": "decisions", "path": "decisions.jsonl", "format": "jsonl"},
            {"name": "run", "path": "run.yaml", "format": "yaml"},
        ],
    }
    (d / "datapackage.yaml").write_text(yaml.safe_dump(desc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return d


def write_comparison(out: str | Path, rows: list[dict]) -> Path:
    p = Path(out) / "comparison.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMPARE_KEYS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return p


def compare_runs(paths: list[str | Path]) -> tuple[list[str], list[tuple[str, str, list]]]:
    """并列比较结果目录：返回（各目录的名称，[(范围, 指标, 各目录的取值)]）。范围为"总计"或站点 id。

    指标：完成请求、失败请求（拒绝、未路由、失败之和）、SLO 达成率、首 token 时延 P50 / P90、IT 电量、设施电量、电费、碳排放。
    """
    runs = [read_run(p) for p in paths]
    names = [Path(p).name for p in paths]
    rows: list[tuple[str, str, list]] = []

    def total(key):
        return [r["summary"].get(key) for r in runs]

    failed = [sum(r["summary"].get(k) or 0 for k in ("rejected", "unrouted", "failed")) for r in runs]
    cur = next((r["summary"].get("currency") for r in runs if r["summary"].get("currency")), "")
    rows += [("总计", "完成请求", total("served")), ("总计", "失败请求", failed),
             ("总计", "SLO 达成率", total("slo_attainment")), ("总计", "TTFT P50 (s)", total("ttft_p50_s")),
             ("总计", "TTFT P99 (s)", total("ttft_p99_s")), ("总计", "IT 电量 (kWh)", total("it_kwh")),
             ("总计", "设施电量 (kWh)", total("facility_kwh")), ("总计", f"电费 ({cur})", total("cost")),
             ("总计", "碳排放 (kg)", total("carbon_kg"))]
    sites = sorted({sid for r in runs for sid in (r["summary"].get("sites") or {})})
    for sid in sites:
        served, p50, p90, it = [], [], [], []
        for r in runs:
            req = r["requests"]
            site = np.array(req.column("site").to_pylist(), dtype=object)
            ok = (site == sid) & (np.array(req.column("status").to_pylist(), dtype=object) == "served")
            tt = np.array([np.nan if v is None else v for v in req.column("ttft_s").to_pylist()], dtype=float)[ok]
            tt = tt[~np.isnan(tt)]
            served.append(int(ok.sum()))
            p50.append(float(np.percentile(tt, 50)) if len(tt) else None)
            p90.append(float(np.percentile(tt, 90)) if len(tt) else None)
            it.append(((r["summary"].get("sites") or {}).get(sid) or {}).get("it_kwh"))
        rows += [(sid, "完成请求", served), (sid, "TTFT P50 (s)", p50), (sid, "TTFT P90 (s)", p90),
                 (sid, "IT 电量 (kWh)", it)]
    return names, rows
