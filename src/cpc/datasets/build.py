"""写出符合平台规范的数据集（数据文件 + datapackage.yaml）。供数据转换脚本与测试使用。

用法：
    write_dataset(out_dir, name="my-trace", version="1.0.0", kind="workload", table=arrow_table,
                  fields=[{"name": "arrival_s", "type": "number", "unit": "s"}, ...],
                  cpc={"origin": "2024-05-12T00:00:00Z"}, licenses=[...], sources=[...])
写出后按类别规范完整校验一次，不符合时抛出 DatasetError。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .descriptor import Descriptor, load_descriptor, write_descriptor
from .profiles import KINDS, PROFILE_VERSION
from .table import validate


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return "sha256:" + h.hexdigest()


def write_dataset(directory: str | os.PathLike, *, name: str, version: str, kind: str, table: pa.Table, fields: list[dict],
                  cpc: dict[str, Any] | None = None, title: str = "", description: str = "", licenses: list[dict] | None = None,
                  sources: list[dict] | None = None, fmt: str = "parquet", extra: dict[str, Any] | None = None) -> Descriptor:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    resource = KINDS[kind].resource
    names = [f["name"] for f in fields]
    if names != table.column_names:
        raise ValueError(f"字段定义 {names} 与表的列 {table.column_names} 不一致")
    path = out / f"{resource}.{fmt}"
    if fmt == "parquet":
        pq.write_table(table, path)
    elif fmt == "csv":
        pacsv.write_csv(table, path)
    else:
        raise ValueError(f"不支持的格式 {fmt}")
    desc = {
        "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
        "name": name, "version": version,
        **({"title": title} if title else {}), **({"description": description} if description else {}),
        "licenses": licenses or [], "sources": sources or [],
        "cpc": {"kind": kind, "profile": PROFILE_VERSION, **(cpc or {})},
        **(extra or {}),
        "resources": [{"name": resource, "type": "table", "path": path.name, "format": fmt, "bytes": path.stat().st_size,
                       "hash": _sha256(path), "schema": {"fields": fields}}],
    }
    write_descriptor(desc, out)
    d = load_descriptor(out)
    validate(d)
    return d
