"""数据集：描述文件与类别规范的校验、列式读取、负载变换、电网信号查询、功率特性标定。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
import yaml

from cpc.cli import main as cli_main
from cpc.datasets import DatasetError, expand, load_descriptor
from cpc.datasets.build import write_dataset
from cpc.datasets.grid import SignalSet, SignalUnavailable
from cpc.datasets.power import load_points, profile_from_points
from cpc.datasets.table import validate
from cpc.datasets.workload import TraceTransform, load_trace

DATA = Path(__file__).resolve().parent / "data" / "datasets"
CORE = [{"name": "arrival_s", "type": "number", "unit": "s"}, {"name": "input_tokens", "type": "integer"},
        {"name": "output_tokens", "type": "integer"}]
UTC = timezone.utc


def workload(tmp_path, t, extra=None, fields=None, **kw):
    cols = {"arrival_s": t, "input_tokens": [10] * len(t), "output_tokens": [5] * len(t), **(extra or {})}
    return write_dataset(tmp_path / "w", name="w", version="1", kind="workload", table=pa.table(cols),
                         fields=fields or CORE + [{"name": k, "type": "string"} for k in (extra or {})], **kw)


# ------------------------------------------------------------------ 描述文件与规范


def test_descriptor_and_profile_checks(tmp_path):
    d = workload(tmp_path, [0.0, 1.0, 2.0], cpc={"origin": "2024-05-10T00:00:00Z"})
    assert d.kind == "workload" and d.ref == "w@1" and d.resource("requests").hash.startswith("sha256:")
    raw = yaml.safe_load((d.root / "datapackage.yaml").read_text(encoding="utf-8"))

    def broken(mut, match):
        r = yaml.safe_load(yaml.safe_dump(raw))
        mut(r)
        (d.root / "datapackage.yaml").write_text(yaml.safe_dump(r, allow_unicode=True), encoding="utf-8")
        with pytest.raises(DatasetError, match=match):
            load_descriptor(d.root)

    fields = lambda r: r["resources"][0]["schema"]["fields"]  # noqa: E731
    broken(lambda r: fields(r).pop(2), "output_tokens")  # 缺核心字段
    broken(lambda r: fields(r)[0].update(unit="ms"), "unit")  # 单位不符
    broken(lambda r: fields(r)[1].update(type="string"), "类型")  # 类型不符
    broken(lambda r: r["cpc"].update(kind="trace"), "未定义")
    broken(lambda r: r["cpc"].update(origin="2024-05-10T00:00:00"), "时区")
    broken(lambda r: r["resources"][0].update(format="xlsx"), "xlsx")


def test_table_constraints_and_order(tmp_path):
    d = workload(tmp_path, [0.0, 2.0, 1.0])
    with pytest.raises(DatasetError, match="非降序"):
        load_trace(d)
    with pytest.raises(DatasetError, match="不满足"):
        write_dataset(tmp_path / "neg", name="n", version="1", kind="workload", fields=CORE,
                      table=pa.table({"arrival_s": [-1.0], "input_tokens": [1], "output_tokens": [1]}))
    with pytest.raises(DatasetError, match="空值"):
        write_dataset(tmp_path / "null", name="n", version="1", kind="workload", fields=CORE,
                      table=pa.table({"arrival_s": [0.0], "input_tokens": pa.array([None], pa.int64()), "output_tokens": [1]}))
    with pytest.raises(DatasetError, match="不满足"):  # output_tokens 规范要求 ≥ 1
        write_dataset(tmp_path / "zero", name="n", version="1", kind="workload", fields=CORE,
                      table=pa.table({"arrival_s": [0.0], "input_tokens": [1], "output_tokens": [0]}))
    d = workload(tmp_path, [0.0])
    (d.root / "requests.parquet").unlink()
    with pytest.raises(DatasetError, match="缺少文件"):
        validate(d)


def test_csv_list_field_and_missing_values(tmp_path):
    p = tmp_path / "c"
    p.mkdir()
    (p / "requests.csv").write_text("arrival_s,input_tokens,output_tokens,prefix_hashes,model\n"
                                    "0,10,5,1;2;3,a\n1.5,20,6,,NA\n", encoding="utf-8")
    desc = {"name": "c", "version": "1", "cpc": {"kind": "workload", "profile": 1},
            "resources": [{"name": "requests", "path": "requests.csv", "format": "csv", "schema": {
                "missingValues": ["", "NA"],
                "fields": CORE + [{"name": "prefix_hashes", "type": "list", "itemType": "integer", "delimiter": ";"},
                                  {"name": "model", "type": "string"}]}}]}
    (p / "datapackage.yaml").write_text(yaml.safe_dump(desc), encoding="utf-8")
    tr = load_trace(load_descriptor(p), ["prefix_hashes", "model"])
    assert tr.attrs["prefix_hashes"] == [[1, 2, 3], None] and tr.attrs["model"] == ["a", None]
    assert tr.attrs_of(0) == {"prefix_hashes": [1, 2, 3], "model": "a"}
    with pytest.raises(DatasetError, match="核心字段"):
        load_trace(load_descriptor(p), ["arrival_s"])
    with pytest.raises(DatasetError, match="未声明"):
        load_trace(load_descriptor(p), ["tenant"])


def test_expand_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("CPC_DATA_DIR", str(tmp_path))
    assert expand("${CPC_DATA_DIR}/x") == tmp_path / "x"
    assert expand("rel/x", base=tmp_path) == tmp_path / "rel" / "x"
    monkeypatch.delenv("NO_SUCH_VAR", raising=False)
    with pytest.raises(DatasetError, match="NO_SUCH_VAR"):
        expand("${NO_SUCH_VAR}/x")


# ------------------------------------------------------------------ 负载


def test_trace_transform(tmp_path):
    t = np.arange(0, 100, 1.0)
    tr = load_trace(workload(tmp_path, t.tolist(), cpc={"origin": "2024-05-10T00:00:00Z"}))
    w = TraceTransform(start_s=10, duration_s=20, time_scale=2.0).apply(tr)
    assert len(w) == 20 and w.t[0] == 0.0 and w.t[-1] == pytest.approx(9.5)
    assert w.origin == datetime(2024, 5, 10, 0, 0, 10, tzinfo=UTC)
    up = TraceTransform(rate_scale=2.0, seed=3).apply(tr)
    assert len(up) == 200 and np.all(np.diff(up.t) >= 0)  # 整数倍：每个请求复制一份
    down = TraceTransform(rate_scale=0.5, seed=3).apply(tr)
    assert 30 < len(down) < 70
    with pytest.raises(DatasetError, match="超出"):
        TraceTransform(start_s=50, duration_s=100).apply(tr)


# ------------------------------------------------------------------ 电网信号


def signals(tmp_path, rows, meta=None):
    ts = pa.timestamp("us", tz="UTC")
    tbl = pa.table({"region": [r[0] for r in rows], "start": pa.array([r[1] for r in rows], ts),
                    "end": pa.array([r[2] for r in rows], ts), "signal": [r[3] for r in rows],
                    "value": pa.array([float(r[4]) for r in rows])})
    fields = [{"name": "region", "type": "string"}, {"name": "start", "type": "datetime"}, {"name": "end", "type": "datetime"},
              {"name": "signal", "type": "string"}, {"name": "value", "type": "number"}]
    return write_dataset(tmp_path / "g", name="g", version="1", kind="grid_signal", table=tbl, fields=fields, fmt="csv",
                         cpc={"signals": meta or {"price": {"unit": "EUR/kWh"}, "carbon_intensity": {"unit": "kgCO2/kWh"}}})


def test_signal_lookup_gaps_and_overlaps(tmp_path):
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    h = lambda k: t0 + timedelta(hours=k)  # noqa: E731
    ss = SignalSet(signals(tmp_path, [("A", h(0), h(1), "price", 0.1), ("A", h(2), h(3), "price", 0.3),
                                      ("A", h(0), h(3), "carbon_intensity", 0.5)]))
    assert ss.value("price", "A", h(0.5)).value == 0.1 and ss.value("price", "A", h(2)).value == 0.3
    assert ss.coverage("price", "A") == (h(0), h(3)) and ss.unit("price") == "EUR/kWh"
    with pytest.raises(SignalUnavailable, match="没有数据"):
        ss.value("price", "A", h(1.5))  # 时段空隙
    with pytest.raises(SignalUnavailable):
        ss.value("price", "B", h(0))
    with pytest.raises(DatasetError, match="重叠"):
        SignalSet(signals(tmp_path, [("A", h(0), h(2), "price", 0.1), ("A", h(1), h(3), "price", 0.2)]))
    with pytest.raises(DatasetError, match="未在 cpc.signals"):
        SignalSet(signals(tmp_path, [("A", h(0), h(1), "renewable_share", 0.4)]))
    with pytest.raises(DatasetError, match="单位"):
        signals(tmp_path, [("A", h(0), h(1), "price", 0.1)], meta={"price": {"unit": "EUR"}})


def test_converted_cn_grid_dataset():
    ss = SignalSet(load_descriptor(DATA / "cn-grid-2026-07"))
    cst = timezone(timedelta(hours=8))
    v = ss.value("price", "guangdong", datetime(2026, 7, 15, 11, 30, tzinfo=cst))
    assert v.label == "sharp" and v.value == pytest.approx(0.65 * 1.7 * 1.25)
    assert ss.value("price", "guangdong", datetime(2026, 7, 15, 3, tzinfo=cst)).value == pytest.approx(0.65 * 0.38)
    assert ss.value("carbon_intensity", "guizhou", datetime(2026, 7, 20, tzinfo=cst)).value == pytest.approx(0.5683)
    with pytest.raises(SignalUnavailable):
        ss.value("price", "guangdong", datetime(2026, 8, 1, 0, 30, tzinfo=cst))  # 转换范围之外


# ------------------------------------------------------------------ 功率特性


def test_power_profile_calibration():
    d = load_descriptor(DATA / "power-test")
    pts = load_points(d, {"device": "X1"})
    assert len(pts) == 4 and pts[0].attrs == {}
    p = profile_from_points(pts, slots=64, idle_w=500, host_w=100, prefill_tokens_per_s=9000, boot_s=30)
    assert p["power_w"][0] == [0.0, 500] and p["power_w"][1] == [0.125, 1700.0] and p["power_w"][-1] == [1.0, 2450.0]
    assert p["tpot_s"][0] == [0.125, 0.021] and p["slots"] == 64 and p["boot_s"] == 30
    from cpc.config import NodeProfile

    NodeProfile.model_validate(p)  # 生成的表可直接写入场景
    with pytest.raises(DatasetError, match="同一组"):
        profile_from_points(load_points(d), slots=64, idle_w=500)


# ------------------------------------------------------------------ 命令行


def test_cli_data_commands(capsys):
    cli_main(["data", "check", str(DATA / "power-test"), str(DATA / "cn-grid-2026-07")])
    out = capsys.readouterr().out
    assert "power-test@1（power_profile）符合规范" in out and "signals 1029 行" in out and "inner_mongolia" in out
    cli_main(["data", "show", str(DATA / "power-test")])
    assert '"licenses"' in capsys.readouterr().out
    cli_main(["profile", str(DATA / "power-test"), "--where", "device=X1", "device_count=4", "--slots", "64",
              "--idle-w", "500", "--name", "x1"])
    out = capsys.readouterr().out
    assert out.startswith("x1:") and "- [0.0, 500.0]" in out and "须补充" in out
    cli_main(["data", "kinds"])
    assert "prefix_hashes" in capsys.readouterr().out
