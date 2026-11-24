"""数据集：清单与校验、下载与错误、负载变换、解析器、电网信号数据集的缺失规则、功率特性标定。

不访问网络：远程文件用临时目录中的小文件与 httpx.MockTransport 代替。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx
import numpy as np
import pytest

from cpc.datasets import DatasetError, Manifest, RemoteFile, all_datasets, make_dataset
from cpc.datasets.base import Dataset, data_dir
from cpc.datasets.grid import EIA_FUELS, Eia930Carbon2024, SignalUnavailable
from cpc.datasets.power import PowerPoint, calibrate
from cpc.datasets.workload import (
    Azure2024Conv, BurstGPT1, TraceTransform, WorkloadTrace, _Mooncake, assign_sources, load_workload,
)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def with_files(cls, files: dict[str, bytes], root):
    """以临时目录中的文件替换数据集清单中的远程文件（校验和按内容计算）。"""
    m = cls.manifest
    rf = tuple(RemoteFile(n, f"https://example.invalid/{n}", sha(b), len(b)) for n, b in files.items())
    sub = type(cls.__name__ + "T", (cls,), {"manifest": Manifest(**{**m.to_dict(), "files": rf, "bundled": ()})})
    d = root / m.name / m.version
    d.mkdir(parents=True)
    for n, b in files.items():
        (d / n).write_bytes(b)
    return sub


# ------------------------------------------------------------------ 清单与校验


def test_every_dataset_has_complete_manifest():
    for name, cls in all_datasets().items():
        m = cls.manifest
        assert m.name == name and m.kind in ("workload", "grid_signal", "power_profile")
        assert m.source and m.license and m.version
        if not m.synthetic:
            assert m.citation, name
            assert m.files or m.bundled, name
        for f in m.files:
            assert len(f.sha256) == 64 and f.size > 0 and f.url.startswith("https://"), (name, f.name)
        for b in m.bundled:
            assert len(b.sha256) == 64 and b.origin


def test_bundled_grid_datasets_verify():
    for name in ("cn-tou-tariffs", "cn-provincial-carbon-2023"):
        ds = make_dataset(name)
        assert ds.status() == {ds.manifest.bundled[0].name: "ok"}


def test_missing_and_corrupt_files_are_errors(tmp_path):
    cls = with_files(Azure2024Conv, {Azure2024Conv.FILE: b"TIMESTAMP,ContextTokens,GeneratedTokens\n"}, tmp_path)
    ds = cls(root=tmp_path)
    ds.verify()
    (ds.dir / Azure2024Conv.FILE).write_bytes(b"tampered")
    ds2 = cls(root=tmp_path)
    with pytest.raises(DatasetError, match="校验失败"):
        ds2.verify()
    ds3 = Azure2024Conv(root=tmp_path / "empty")
    with pytest.raises(DatasetError, match="cpc data fetch azure-llm-2024-conv"):
        ds3.verify()  # 不自动下载，也不退回合成数据


def test_fetch_downloads_verifies_and_reports_errors(tmp_path, monkeypatch):
    body = b"x" * 1000
    cls = type("D", (Dataset,), {"manifest": Manifest("t-ds", "1", "workload", "t", "https://example.invalid", "MIT", "c",
                                                       files=(RemoteFile("f.bin", "https://example.invalid/f.bin", sha(body), 1000),))})
    real_stream = httpx.stream

    def fake_stream(method, url, **kw):
        transport = httpx.MockTransport(lambda req: httpx.Response(200, content=body) if "ok" in mode else httpx.Response(404))
        return httpx.Client(transport=transport).stream(method, url)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    mode = "ok"
    ds = cls(root=tmp_path)
    assert ds.fetch()[0].read_bytes() == body and ds.status() == {"f.bin": "ok"}
    mode = "fail"
    with pytest.raises(DatasetError, match="下载 .* 失败"):
        cls(root=tmp_path / "b").fetch()
    monkeypatch.setattr(httpx, "stream", real_stream)


def test_hf_dataset_requires_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    ds = make_dataset("mlenergy-benchmark-v3-llm", root=tmp_path)
    with pytest.raises(DatasetError, match="HF_TOKEN"):
        ds.fetch()


def test_data_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CPC_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path


# ------------------------------------------------------------------ 负载：解析与变换


def test_azure_parser_and_derived_cache(tmp_path):
    csv = (b"TIMESTAMP,ContextTokens,GeneratedTokens\n"
           b"2024-05-12 00:00:01.500000+00:00,100,10\n"
           b"2024-05-12 00:00:00.001163+00:00,200,20\n"
           b"2024-05-12 00:00:03+00:00,300,30\n")
    cls = with_files(Azure2024Conv, {Azure2024Conv.FILE: csv}, tmp_path)
    tr = cls(root=tmp_path).load_trace()
    assert list(tr.input_tokens) == [200, 100, 300]  # 按时间排序
    assert tr.t[0] == 0.0 and tr.t[2] == pytest.approx(2.998837)
    assert tr.origin == datetime(2024, 5, 12, 0, 0, 0, 1163, tzinfo=timezone.utc)
    assert (cls(root=tmp_path).dir / "derived" / "trace.npz").is_file()
    tr2 = cls(root=tmp_path).load_trace()  # 第二次从派生缓存读取
    assert np.array_equal(tr.t, tr2.t)


def test_burstgpt_requires_explicit_failure_policy(tmp_path):
    csv = (b"Timestamp,Model,Request tokens,Response tokens,Total tokens,Log Type\n"
           b"5,ChatGPT,472,18,490,Conversation log\n45,GPT-4,1087,0,1087,API log\n118,GPT-4,417,276,693,Conversation log\n")
    cls = with_files(BurstGPT1, {BurstGPT1.FILE: csv}, tmp_path)
    with pytest.raises(TypeError, match="include_failed"):
        cls(root=tmp_path)
    assert len(cls(root=tmp_path, include_failed=True).load_trace()) == 3
    tr = cls(root=tmp_path, include_failed=False).load_trace()
    assert len(tr) == 2 and list(tr.model) == ["ChatGPT", "GPT-4"] and tr.origin is None


def test_mooncake_parser(tmp_path):
    mc = next(c for n, c in all_datasets().items() if n == "mooncake-fast25-conversation")
    lines = b"".join(json.dumps({"timestamp": ts, "input_length": i, "output_length": o, "hash_ids": [1, 2]}).encode() + b"\n"
                     for ts, i, o in [(0, 10, 1), (1500, 20, 2)])
    cls = with_files(mc, {mc.FILE: lines}, tmp_path)
    assert issubclass(cls, _Mooncake)
    tr = cls(root=tmp_path).load_trace()
    assert list(tr.t) == [0.0, 1.5] and list(tr.output_tokens) == [1, 2]


def trace(n=1000, dur=100.0):
    t = np.linspace(0, dur, n)
    return WorkloadTrace(t=t, input_tokens=np.arange(n), output_tokens=np.ones(n, dtype=np.int64), ref="t@1")


def test_transform_window_compression_and_rate():
    tr = trace()
    w = TraceTransform(start_s=50, duration_s=20, time_scale=2.0).apply(tr)
    assert w.t.min() >= 0 and w.t.max() < 10.0 and len(w) == pytest.approx(200, abs=2)
    thin = TraceTransform(rate_scale=0.5, seed=3).apply(tr)
    assert 400 < len(thin) < 600 and np.all(np.diff(thin.t) >= 0)
    up = TraceTransform(rate_scale=2.5, seed=3).apply(tr)
    assert 2300 < len(up) < 2700 and np.all(np.diff(up.t) >= 0)
    with pytest.raises(DatasetError, match="超出"):
        TraceTransform(start_s=90, duration_s=20).apply(tr)
    with pytest.raises(DatasetError, match="超出"):
        TraceTransform(start_s=200).apply(tr)


def test_source_assignment_follows_shares():
    src = assign_sources(20000, ["a", "b"], [0.25, 0.75], seed=1)
    assert abs(np.mean(src == "a") - 0.25) < 0.02
    with pytest.raises(ValueError):
        assign_sources(10, ["a"], [0.0], seed=1)


def test_load_workload_uses_scenario_clock_for_synthetic(scn):
    tr = load_workload(scn)
    assert tr.ref == "synthetic-poisson@1" and tr.origin == scn.clock.start
    assert 10 < len(tr) / tr.duration_s < 14  # 12 请求/秒的日内波动负载


# ------------------------------------------------------------------ 电网信号：EIA-930 的缺失规则


def eia_csv(rows: list[tuple[str, str, dict[str, str]]]) -> bytes:
    head = ["Balancing Authority", "Data Date", "Hour Number", "Local Time at End of Hour", "UTC Time at End of Hour"]
    head += [f"Net Generation (MW) from {v} (Adjusted)" for v in EIA_FUELS.values()]
    out = [",".join(f'"{h}"' for h in head)]
    for ba, utc, gen in rows:
        out.append(",".join([ba, "", "", "", utc] + [gen.get(k, "") for k in EIA_FUELS]))
    return ("\n".join(out) + "\n").encode()


def test_eia_carbon_rules(tmp_path):
    rows = [("X", "05/14/2024 1:00:00 AM", {"coal": "100", "wind": "100", "solar": "0"}),
            ("X", "05/14/2024 2:00:00 AM", {"coal": "", "wind": "100", "solar": "0"}),  # 已上报的类别为空：缺失
            ("X", "05/14/2024 3:00:00 AM", {"coal": "0", "wind": "100", "solar": "5"}),
            ("Y", "05/14/2024 1:00:00 AM", {"coal": "50", "petroleum": "50"})]
    cls = with_files(Eia930Carbon2024, {Eia930Carbon2024.FILES[0]: eia_csv(rows)}, tmp_path)
    ds = cls(root=tmp_path, factors_g_per_kwh={"coal": 800, "wind": 10, "solar": 40}, series="adjusted")
    t = lambda h: datetime(2024, 5, 14, h, 30, tzinfo=timezone.utc)  # noqa: E731
    assert ds.value("carbon", "X", t(0)).value == pytest.approx((100 * 800 + 100 * 10) / 200 / 1000)
    with pytest.raises(SignalUnavailable, match="缺失"):
        ds.value("carbon", "X", t(1))
    assert ds.value("carbon", "X", t(2)).value == pytest.approx((100 * 10 + 5 * 40) / 105 / 1000)
    with pytest.raises(SignalUnavailable, match="petroleum"):
        ds.value("carbon", "Y", t(0))  # 有发电但没有排放因子
    with pytest.raises(SignalUnavailable):
        ds.value("carbon", "X", t(10))  # 超出数据
    with pytest.raises(TypeError):
        cls(root=tmp_path, factors_g_per_kwh={"lignite": 1}, series="adjusted")


def test_grid_model_reports_missing_sites(scn):
    from cpc.grid.signals import GridModel

    scn.grid.price.dataset, scn.grid.price.params = "fixed-price", {"prices": {"shanghai": 0.7, "guangdong": 0.6,
                                                                               "guizhou": 0.5, "inner_mongolia": 0.4}}
    g = GridModel(scn)
    g.price.prices.pop("guizhou")  # 运行期某区域数据缺失
    sig, missing = g.snapshot(scn.clock.start)
    assert "gz" in missing and set(sig) == {"sh", "gd", "nm"}


# ------------------------------------------------------------------ 功率特性：标定


def test_calibration_recovers_model_parameters():
    a, pmax, alpha, d0, k, slots = 300.0, 600.0, 0.8, 0.02, 0.5, 128
    pts = []
    for b in (8, 16, 32, 64, 96, 128):
        u = b / slots
        pts.append(PowerPoint("H100", "m", "chat", 2, b, float(b), 2 * (a + (pmax - a) * u**alpha), d0 * (1 + k * u), 0, 0, True))
    cal = calibrate(pts, slots)
    p = cal.params
    assert p["gpus"] == 2 and p["slots"] == slots
    assert p["gpu_active_min_w"] == pytest.approx(a, rel=0.02) and p["gpu_max_w"] == pytest.approx(pmax, rel=0.01)
    assert p["power_exponent"] == pytest.approx(alpha, abs=0.02)
    assert p["decode_s_per_token"] == pytest.approx(d0, rel=1e-3) and p["decode_slowdown"] == pytest.approx(k, rel=1e-3)
    assert "gpu_idle_w" not in p  # 空载功率不由标定给出
    with pytest.raises(ValueError, match="至少需要 3 次"):
        calibrate(pts[:2], slots)

