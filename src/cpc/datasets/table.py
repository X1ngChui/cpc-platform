"""表格资源的读取与校验（Apache Arrow）。

按 Table Schema 的字段类型解析列，再检查约束（required、minimum、maximum、exclusiveMinimum、exclusiveMaximum、enum、unique）
与类别规范对核心字段的要求。任何不符合都抛出 DatasetError，平台不修正、不丢弃、不填补数据。
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .descriptor import DatasetError, Descriptor, Field, Resource
from .profiles import KINDS

ARROW_TYPES = {
    "string": pa.string(), "number": pa.float64(), "integer": pa.int64(), "boolean": pa.bool_(),
    "datetime": pa.timestamp("us", tz="UTC"), "date": pa.date32(),
    "list": pa.string(), "object": pa.string(), "array": pa.string(), "any": pa.string(),
}
LIST_ITEM_TYPES = {"string": pa.string(), "integer": pa.int64(), "number": pa.float64()}


def resource_file(d: Descriptor, r: Resource) -> Path:
    """资源的本地文件；不存在时报错。"""
    p = d.local_path(r)
    if not p.is_file():
        hint = f"运行 cpc data fetch {d.file}" if r.is_remote else "检查数据集目录"
        raise DatasetError(f"数据集 {d.ref} 的资源 {r.name} 缺少文件 {p}；{hint}")
    return p


def _read_csv(p: Path, r: Resource, cols: list[Field]) -> pa.Table:
    opts = pacsv.ConvertOptions(column_types={f.name: ARROW_TYPES[f.type] for f in cols}, include_columns=[f.name for f in cols],
                                null_values=list(r.missing_values), strings_can_be_null=True, quoted_strings_can_be_null=True)
    try:
        return pacsv.read_csv(p, convert_options=opts)
    except (pa.ArrowInvalid, KeyError) as e:
        raise DatasetError(f"{p} 的内容与描述文件的字段定义不符：{e}") from e


def _read_parquet(p: Path, cols: list[Field]) -> pa.Table:
    try:
        t = pq.read_table(p, columns=[f.name for f in cols])
    except (pa.ArrowInvalid, OSError) as e:
        raise DatasetError(f"无法读取 {p} 的列 {[f.name for f in cols]}：{e}") from e
    out = []
    for f in cols:
        col = t.column(f.name)
        if f.type == "datetime":
            if not pa.types.is_timestamp(col.type) or col.type.tz is None:
                raise DatasetError(f"{p} 的列 {f.name} 须为带时区的时间戳（当前为 {col.type}）")
            col = col.cast(ARROW_TYPES["datetime"])
        elif f.type == "list" and pa.types.is_list(col.type):
            pass
        else:
            try:
                col = col.cast(ARROW_TYPES[f.type], safe=True)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
                raise DatasetError(f"{p} 的列 {f.name} 无法按 {f.type} 解析：{e}") from e
        out.append(col)
    return pa.table(out, names=[f.name for f in cols])


def _split_lists(t: pa.Table, cols: list[Field]) -> pa.Table:
    for f in cols:
        col = t.column(f.name)
        if f.type == "list" and pa.types.is_string(col.type):
            item = LIST_ITEM_TYPES.get(f.item_type or "string")
            if item is None:
                raise DatasetError(f"字段 {f.name} 的 itemType {f.item_type} 不受支持")
            parts = pc.split_pattern(col, f.delimiter)
            try:
                parts = parts.cast(pa.list_(item))
            except pa.ArrowInvalid as e:
                raise DatasetError(f"字段 {f.name} 的列表元素无法按 {f.item_type} 解析：{e}") from e
            t = t.set_column(t.schema.get_field_index(f.name), f.name, parts)
    return t


def _check(d: Descriptor, r: Resource, f: Field, col: pa.ChunkedArray, core: dict) -> None:
    where = f"数据集 {d.ref} 资源 {r.name} 字段 {f.name}"
    spec = core.get(f.name)
    if (f.required or spec is not None) and col.null_count:
        raise DatasetError(f"{where}：有 {col.null_count} 个空值（该字段{'为核心字段' if spec else '声明为 required'}）")
    if not len(col):
        return
    numeric = pa.types.is_integer(col.type) or pa.types.is_floating(col.type) or pa.types.is_timestamp(col.type)
    if numeric:
        mm = pc.min_max(col)
        lo, hi = mm["min"].as_py(), mm["max"].as_py()
        bounds = [("minimum", f.constraints.get("minimum"), lambda v, b: v >= b),
                  ("maximum", f.constraints.get("maximum"), lambda v, b: v <= b),
                  ("exclusiveMinimum", f.constraints.get("exclusiveMinimum"), lambda v, b: v > b),
                  ("exclusiveMaximum", f.constraints.get("exclusiveMaximum"), lambda v, b: v < b)]
        if spec is not None:
            bounds += [("规范下限", spec.minimum, lambda v, b: v >= b), ("规范下限（不含）", spec.exclusive_minimum, lambda v, b: v > b)]
        for label, b, ok in bounds:
            if b is None or lo is None:
                continue
            v = hi if label.startswith(("maximum", "exclusiveMaximum")) else lo
            if not ok(v, b):
                raise DatasetError(f"{where}：取值 {v} 不满足 {label} {b}")
        if pa.types.is_floating(col.type) and pc.any(pc.is_nan(col)).as_py():
            raise DatasetError(f"{where}：含 NaN")
    if "enum" in f.constraints:
        bad = pc.unique(pc.filter(col, pc.invert(pc.is_in(col, value_set=pa.array(f.constraints["enum"], type=col.type)))))
        bad = [x for x in bad.to_pylist() if x is not None]
        if bad:
            raise DatasetError(f"{where}：取值 {bad[:5]} 不在 enum 中")
    if f.constraints.get("unique") and pc.count_distinct(col).as_py() != len(col) - col.null_count:
        raise DatasetError(f"{where}：取值不唯一")


def read_table(d: Descriptor, resource: str, columns: list[str] | None = None) -> pa.Table:
    """读取并校验资源。columns 为需要的字段（缺省为全部声明的字段）；所需字段须在描述文件中声明。"""
    r = d.resource(resource)
    names = columns or [f.name for f in r.fields]
    cols = []
    for n in names:
        f = r.field(n)
        if f is None:
            raise DatasetError(f"数据集 {d.ref} 的资源 {r.name} 未声明字段 {n}")
        cols.append(f)
    p = resource_file(d, r)
    t = _read_csv(p, r, cols) if r.format == "csv" else _read_parquet(p, cols)
    t = _split_lists(t, cols)
    core = KINDS[d.kind].core if KINDS[d.kind].resource == r.name else {}
    for f in cols:
        _check(d, r, f, t.column(f.name), core)
    return t


def validate(d: Descriptor) -> dict[str, int]:
    """完整校验数据集的全部资源；返回各资源的行数。"""
    return {r.name: read_table(d, r.name).num_rows for r in d.resources}
