"""Reading and validating table resources (Apache Arrow).

Columns are parsed with the Table Schema field types, then checked against the constraints
(required, minimum, maximum, exclusiveMinimum, exclusiveMaximum, enum, unique) and the requirements
of the kind specification on core fields. Any violation raises DatasetError; the platform never
corrects, drops or fills data.
"""

from __future__ import annotations

import operator
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
    """Local file of a resource; an error if it is missing."""
    if not (p := d.local_path(r)).is_file():
        raise DatasetError(f"dataset {d.ref}: the file {p} of resource {r.name} is missing")
    return p


def _read_csv(p: Path, r: Resource, cols: list[Field]) -> pa.Table:
    opts = pacsv.ConvertOptions(column_types={f.name: ARROW_TYPES[f.type] for f in cols}, include_columns=[f.name for f in cols],
                                null_values=list(r.missing_values), strings_can_be_null=True, quoted_strings_can_be_null=True)
    try:
        return pacsv.read_csv(p, convert_options=opts)
    except (pa.ArrowInvalid, KeyError) as e:
        raise DatasetError(f"{p} does not match the fields of the descriptor: {e}") from e


def _parquet_column(p: Path, f: Field, col: pa.ChunkedArray) -> pa.ChunkedArray:
    if f.type == "datetime":
        if not pa.types.is_timestamp(col.type) or col.type.tz is None:
            raise DatasetError(f"{p}: column {f.name} must be a timestamp with a time zone (got {col.type})")
        return col.cast(ARROW_TYPES["datetime"])
    if f.type == "list" and pa.types.is_list(col.type):
        return col
    try:
        return col.cast(ARROW_TYPES[f.type], safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as e:
        raise DatasetError(f"{p}: column {f.name} cannot be read as {f.type}: {e}") from e


def _read_parquet(p: Path, cols: list[Field]) -> pa.Table:
    names = [f.name for f in cols]
    try:
        t = pq.read_table(p, columns=names)
    except (pa.ArrowInvalid, OSError) as e:
        raise DatasetError(f"cannot read columns {names} of {p}: {e}") from e
    return pa.table([_parquet_column(p, f, t.column(f.name)) for f in cols], names=names)


def _split_lists(t: pa.Table, cols: list[Field]) -> pa.Table:
    for f in cols:
        if f.type != "list" or not pa.types.is_string((col := t.column(f.name)).type):
            continue
        if (item := LIST_ITEM_TYPES.get(f.item_type or "string")) is None:
            raise DatasetError(f"field {f.name}: unsupported itemType {f.item_type}")
        try:
            parts = pc.split_pattern(col, f.delimiter).cast(pa.list_(item))
        except pa.ArrowInvalid as e:
            raise DatasetError(f"field {f.name}: list items cannot be read as {f.item_type}: {e}") from e
        t = t.set_column(t.schema.get_field_index(f.name), f.name, parts)
    return t


def _check(d: Descriptor, r: Resource, f: Field, col: pa.ChunkedArray, core: dict) -> None:
    where = f"dataset {d.ref}, resource {r.name}, field {f.name}"
    spec = core.get(f.name)
    if (f.required or spec is not None) and col.null_count:
        raise DatasetError(f"{where}: {col.null_count} empty values (the field is {'a core field' if spec else 'required'})")
    if not len(col):
        return
    if pa.types.is_integer(col.type) or pa.types.is_floating(col.type) or pa.types.is_timestamp(col.type):
        mm = pc.min_max(col)
        lo, hi = mm["min"].as_py(), mm["max"].as_py()
        c = f.constraints
        bounds = [("minimum", c.get("minimum"), lo, operator.ge), ("maximum", c.get("maximum"), hi, operator.le),
                  ("exclusiveMinimum", c.get("exclusiveMinimum"), lo, operator.gt),
                  ("exclusiveMaximum", c.get("exclusiveMaximum"), hi, operator.lt)]
        if spec is not None:
            bounds += [("specification minimum", spec.minimum, lo, operator.ge),
                       ("specification exclusive minimum", spec.exclusive_minimum, lo, operator.gt)]
        for label, bound, v, ok in bounds:
            if bound is not None and v is not None and not ok(v, bound):
                raise DatasetError(f"{where}: value {v} violates {label} {bound}")
        if pa.types.is_floating(col.type) and pc.any(pc.is_nan(col)).as_py():
            raise DatasetError(f"{where}: contains NaN")
    if "enum" in f.constraints:
        allowed = pa.array(f.constraints["enum"], type=col.type)
        if bad := [x for x in pc.unique(pc.filter(col, pc.invert(pc.is_in(col, value_set=allowed)))).to_pylist() if x is not None]:
            raise DatasetError(f"{where}: values {bad[:5]} are not in enum")
    if f.constraints.get("unique") and pc.count_distinct(col).as_py() != len(col) - col.null_count:
        raise DatasetError(f"{where}: values are not unique")


def read_table(d: Descriptor, resource: str, columns: list[str] | None = None) -> pa.Table:
    """Read and validate a resource. columns lists the fields needed (default: all declared fields),
    which must be declared in the descriptor."""
    r = d.resource(resource)
    cols = []
    for n in columns or [f.name for f in r.fields]:
        if (f := r.field(n)) is None:
            raise DatasetError(f"dataset {d.ref}: resource {r.name} does not declare field {n}")
        cols.append(f)
    p = resource_file(d, r)
    t = _split_lists(_read_csv(p, r, cols) if r.format == "csv" else _read_parquet(p, cols), cols)
    core = KINDS[d.kind].core if KINDS[d.kind].resource == r.name else {}
    for f in cols:
        _check(d, r, f, t.column(f.name), core)
    return t


def validate(d: Descriptor) -> dict[str, int]:
    """Validate every resource of a dataset; returns the row count of each."""
    return {r.name: read_table(d, r.name).num_rows for r in d.resources}
