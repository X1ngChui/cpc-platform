"""Run results (data packages) and experiment descriptions.

The result of a run (simulation with cpc sim / cpc run, live experiment with cpc live) is a directory
forming a Data Package:
    datapackage.yaml   descriptor: resources, fields and provenance (cpc.kind = run_result)
    run.yaml           run description: mode (simulation / live), the expanded scenario, policies and
                       parameters, duration, platform version, creation time
    summary.json       summary metrics
    timeseries.csv     per time bin: IT power, online servers, arrival rate and grid signals of each site
    requests.parquet   per request: arrival, source, site, status, routing reason, latencies, SLO met
    decisions.jsonl    capacity decisions per control period

Experiment description (cpc run): one scenario and variants overriding policies or workload
parameters; each variant runs separately into <out>/<variant>/, and the comparison goes to
<out>/comparison.csv.
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
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import ConfigError, PolicyConfig, Scenario, duplicates, read_yaml, validate


@dataclass
class RunResult:
    """Result of a run: summary, time series (one row per bin), per-request columns (name -> array), decisions."""

    summary: dict
    timeseries: list[dict]
    requests: dict[str, np.ndarray]
    decisions: list[dict]


def _cell(v: str) -> float | str | None:
    """Cell of the time-series CSV: empty -> None, numbers -> float, other text (the time column) unchanged."""
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return v


def read_run(path: str | Path) -> dict:
    """Read a result package: summary (dict), run (dict), requests (pyarrow.Table), timeseries (rows), decisions (list).

    Each time-series row is a dict: numeric columns are float, empty cells (no power limit, missing
    readings) None, the time column a string.
    """
    d = Path(path)
    if not (d / "summary.json").is_file():
        raise FileNotFoundError(f"{d} is not a result directory (summary.json is missing)")
    with (d / "timeseries.csv").open(encoding="utf-8") as f:
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
    routing: PolicyConfig | None = None  # default: scheduler.routing of the scenario
    capacity: PolicyConfig | None = None  # default: scheduler.capacity of the scenario
    workload: dict[str, Any] = Field(default_factory=dict)  # overrides fields of the scenario workload (rate_scale, seed, ...)


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    scenario: str  # relative to the experiment file
    hours: float = Field(24.0, gt=0)
    bin_s: float = Field(300.0, gt=0)
    variants: list[Variant]
    base_dir: Path = Field(default=Path("."), exclude=True)

    def scenario_path(self) -> Path:
        p = Path(self.scenario)
        return p if p.is_absolute() else (self.base_dir / p).resolve()


def load_experiment(path: str | Path) -> Experiment:
    path = Path(path).resolve()
    exp = validate(Experiment, {**read_yaml(path, "experiment file"), "base_dir": path.parent}, f"experiment file {path}")
    names = [v.name for v in exp.variants]
    if not names or duplicates(names):
        raise ConfigError(f"experiment file {path}: variants must be non-empty with distinct names")
    return exp


def _type(t: pa.DataType) -> str:
    if pa.types.is_integer(t):
        return "integer"
    if pa.types.is_floating(t):
        return "number"
    return "boolean" if pa.types.is_boolean(t) else "string"


def write_run(out: str | Path, res: RunResult, scn: Scenario, run: dict) -> Path:
    """Write the result package of a run; run describes the run (mode, policies, duration, ...)."""
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps(res.summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (d / "timeseries.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(res.timeseries[0]))
        w.writeheader()
        w.writerows(res.timeseries)
    (d / "decisions.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in res.decisions),
                                       encoding="utf-8")
    req = pa.table({k: pa.array(v.tolist(), type=pa.string()) if isinstance(v, np.ndarray) and v.dtype == object
                    else pa.array(v) for k, v in res.requests.items()})
    pq.write_table(req, d / "requests.parquet")
    meta = {"platform": f"cpc-platform {__version__}", "python": platform.python_version(),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), **run,
            "scenario": scn.model_dump(mode="json")}
    (d / "run.yaml").write_text(yaml.safe_dump(meta, allow_unicode=True, sort_keys=False), encoding="utf-8")
    desc = {
        "name": str(run.get("name", d.name)).lower().replace(" ", "-"),
        "created": meta["created"],
        "cpc": {"kind": "run_result", "profile": 1, "run": "run.yaml", "workload": res.summary.get("workload", {}).get("ref"),
                "grid": res.summary.get("grid")},
        "resources": [
            {"name": "requests", "type": "table", "path": "requests.parquet", "format": "parquet",
             "schema": {"fields": [{"name": f.name, "type": _type(f.type)} for f in req.schema]}},
            {"name": "timeseries", "type": "table", "path": "timeseries.csv", "format": "csv",
             "schema": {"fields": [{"name": k, "type": "string" if k == "time" else "number"} for k in res.timeseries[0]]}},
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
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMPARE_KEYS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return p


def _site_stats(req: pa.Table, sid: str) -> tuple[int, float | None, float | None]:
    site = np.array(req.column("site").to_pylist(), dtype=object)
    ok = (site == sid) & (np.array(req.column("status").to_pylist(), dtype=object) == "served")
    tt = np.array([np.nan if v is None else v for v in req.column("ttft_s").to_pylist()], dtype=float)[ok]
    tt = tt[~np.isnan(tt)]
    q = lambda p: float(np.percentile(tt, p)) if len(tt) else None  # noqa: E731
    return int(ok.sum()), q(50), q(90)


def compare_runs(paths: list[str | Path]) -> tuple[list[str], list[tuple[str, str, list]]]:
    """Compare result directories side by side: (directory names, [(scope, metric, values per directory)]).
    The scope is "total" or a site id.

    Metrics: served, failed (rejected + unrouted + failed), SLO attainment, TTFT P50 / P90 (P99 in
    total), IT energy, facility energy, cost and carbon.
    """
    runs = [read_run(p) for p in paths]
    names = [Path(p).name for p in paths]
    total = lambda key: [r["summary"].get(key) for r in runs]  # noqa: E731
    failed = [sum(r["summary"].get(k) or 0 for k in ("rejected", "unrouted", "failed")) for r in runs]
    cur = next((c for r in runs if (c := r["summary"].get("currency"))), "")
    rows: list[tuple[str, str, list]] = [
        ("total", "served", total("served")), ("total", "failed", failed),
        ("total", "SLO attainment", total("slo_attainment")), ("total", "TTFT P50 (s)", total("ttft_p50_s")),
        ("total", "TTFT P99 (s)", total("ttft_p99_s")), ("total", "IT energy (kWh)", total("it_kwh")),
        ("total", "facility energy (kWh)", total("facility_kwh")), ("total", f"cost ({cur})", total("cost")),
        ("total", "carbon (kg)", total("carbon_kg"))]
    for sid in sorted({sid for r in runs for sid in r["summary"].get("sites") or {}}):
        stats = [_site_stats(r["requests"], sid) for r in runs]
        it = [((r["summary"].get("sites") or {}).get(sid) or {}).get("it_kwh") for r in runs]
        rows += [(sid, "served", [s[0] for s in stats]), (sid, "TTFT P50 (s)", [s[1] for s in stats]),
                 (sid, "TTFT P90 (s)", [s[2] for s in stats]), (sid, "IT energy (kWh)", it)]
    return names, rows
