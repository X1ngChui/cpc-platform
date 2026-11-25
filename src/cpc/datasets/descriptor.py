"""数据集描述文件：Frictionless Data Package v2 的子集与平台扩展属性 `cpc`。

描述文件为数据集目录中的 `datapackage.yaml` 或 `datapackage.json`。平台读取的属性：

    name, version, title, description, licenses, sources, contributors   数据包级元数据（Data Package v2）
    resources[]: name, path, format, hash, bytes, schema                 资源（Data Resource v2）；path 为相对路径或 URL
    resources[].schema: fields[], missingValues                           表结构（Table Schema v2）
    fields[]: name, type, format, constraints, unit, description          字段；unit 为平台使用的自定义属性
    cpc: {kind, profile, ...}                                             平台扩展：数据集类别与规范版本，以及类别相关的属性

hash 为可选的出处信息，采用 Data Package v2 的"算法前缀"写法（如 `sha256:<64 位十六进制>`），平台当前不据此校验文件。
描述文件、资源与字段中的其他属性保留在 extra 中，平台不解释。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DESCRIPTOR_NAMES = ("datapackage.yaml", "datapackage.yml", "datapackage.json")
FIELD_TYPES = {"string", "number", "integer", "boolean", "datetime", "date", "list", "object", "array", "any"}
CONSTRAINTS = {"required", "unique", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "enum", "pattern",
               "minLength", "maxLength"}
HASH_RE = re.compile(r"^[a-z0-9]+:[0-9a-f]+$")


class DatasetError(Exception):
    """数据集不可用：描述文件无效、文件缺失、校验失败或不符合类别规范。"""


@dataclass(frozen=True)
class Field:
    name: str
    type: str = "any"
    format: str = "default"
    constraints: dict[str, Any] = field(default_factory=dict)
    unit: str | None = None
    description: str = ""
    item_type: str | None = None  # list 类型的元素类型（Table Schema 的 itemType）
    delimiter: str = ","  # list 类型在 CSV 中的分隔符
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> bool:
        return bool(self.constraints.get("required"))


@dataclass(frozen=True)
class Resource:
    name: str
    path: str  # 相对于描述文件目录的路径
    format: str
    hash: str | None
    bytes: int | None
    fields: tuple[Field, ...]
    missing_values: tuple[str, ...] = ("",)
    extra: dict[str, Any] = field(default_factory=dict)

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass(frozen=True)
class Descriptor:
    root: Path  # 描述文件所在目录
    file: Path
    name: str
    version: str
    kind: str
    profile: int
    resources: tuple[Resource, ...]
    title: str = ""
    description: str = ""
    licenses: tuple[dict, ...] = ()
    sources: tuple[dict, ...] = ()
    cpc: dict[str, Any] = field(default_factory=dict)  # cpc 扩展属性（含 kind、profile 及类别相关属性）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def resource(self, name: str) -> Resource:
        r = next((r for r in self.resources if r.name == name), None)
        if r is None:
            raise DatasetError(f"数据集 {self.ref} 没有资源 {name}（已有 {[x.name for x in self.resources]}）")
        return r

    def local_path(self, r: Resource) -> Path:
        """资源的本地路径（相对于描述文件目录）。"""
        p = Path(r.path)
        if p.is_absolute() or ".." in p.parts:
            raise DatasetError(f"数据集 {self.ref} 的资源 {r.name} 路径须为描述文件目录内的相对路径：{r.path}")
        return self.root / p

    def summary(self) -> dict:
        return {"name": self.name, "version": self.version, "kind": self.kind, "profile": self.profile, "title": self.title,
                "licenses": list(self.licenses), "sources": list(self.sources),
                "resources": [{"name": r.name, "path": r.path, "format": r.format, "hash": r.hash, "bytes": r.bytes,
                               "fields": [f.name for f in r.fields]} for r in self.resources],
                "cpc": self.cpc}


def data_dir() -> Path:
    """数据集目录：环境变量 CPC_DATA_DIR，未设置时为 ~/.cache/cpc/datasets。"""
    return Path(os.environ.get("CPC_DATA_DIR") or Path.home() / ".cache" / "cpc" / "datasets")


def expand(path: str | os.PathLike, base: Path | None = None) -> Path:
    """展开 ~ 与 ${VAR} 形式的环境变量；${CPC_DATA_DIR} 未设置时取 data_dir()，其他未定义的变量报错。
    相对路径相对于 base 解析。"""
    s = str(path).replace("${CPC_DATA_DIR}", str(data_dir()))
    for var in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", s):
        if var not in os.environ:
            raise DatasetError(f"路径 {s} 引用了未定义的环境变量 {var}")
    p = Path(os.path.expanduser(os.path.expandvars(s)))
    if not p.is_absolute() and base is not None:
        p = base / p
    return p


def find_descriptor(path: str | os.PathLike, base: Path | None = None) -> Path:
    p = expand(path, base)
    if p.is_dir():
        found = [p / n for n in DESCRIPTOR_NAMES if (p / n).is_file()]
        if not found:
            raise DatasetError(f"目录 {p} 中没有数据集描述文件（{' / '.join(DESCRIPTOR_NAMES)}）")
        return found[0]
    if p.is_file():
        return p
    hint = f"；路径中的 ${{CPC_DATA_DIR}} 为 {data_dir()}" if "${CPC_DATA_DIR}" in str(path) else ""
    raise DatasetError(f"数据集 {p} 不存在：请先准备该数据集（见《使用手册》\"数据\"一章）{hint}")


def _field(d: dict, where: str) -> Field:
    if "name" not in d:
        raise DatasetError(f"{where}：字段缺少 name")
    t = d.get("type", "any")
    if t not in FIELD_TYPES:
        raise DatasetError(f"{where}：字段 {d['name']} 的类型 {t} 不受支持（可用 {sorted(FIELD_TYPES)}）")
    cons = dict(d.get("constraints") or {})
    unknown = set(cons) - CONSTRAINTS
    if unknown:
        raise DatasetError(f"{where}：字段 {d['name']} 含不支持的约束 {sorted(unknown)}")
    known = {"name", "type", "format", "constraints", "unit", "description", "itemType", "delimiter"}
    return Field(name=d["name"], type=t, format=d.get("format", "default"), constraints=cons, unit=d.get("unit"),
                 description=d.get("description", ""), item_type=d.get("itemType"), delimiter=d.get("delimiter", ","),
                 extra={k: v for k, v in d.items() if k not in known})


def _resource(d: dict, where: str) -> Resource:
    for k in ("name", "path"):
        if k not in d:
            raise DatasetError(f"{where}：资源缺少 {k}（平台要求资源以 path 给出文件，不支持内联 data）")
    if not isinstance(d["path"], str):
        raise DatasetError(f"{where}：资源 {d['name']} 的 path 须为单个文件")
    fmt = d.get("format") or d["path"].rsplit(".", 1)[-1].lower()
    if fmt not in ("csv", "parquet"):
        raise DatasetError(f"{where}：资源 {d['name']} 的格式 {fmt} 不受支持（csv、parquet）")
    h = d.get("hash")
    if h is not None and not HASH_RE.match(h):
        raise DatasetError(f"{where}：资源 {d['name']} 的 hash 须为 <算法>:<十六进制摘要>，当前为 {h}")
    schema = d.get("schema")
    if not isinstance(schema, dict) or not schema.get("fields"):
        raise DatasetError(f"{where}：资源 {d['name']} 须内联给出 schema.fields")
    fields = tuple(_field(f, f"{where} 资源 {d['name']}") for f in schema["fields"])
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise DatasetError(f"{where}：资源 {d['name']} 的字段名重复")
    mv = schema.get("missingValues", [""])
    mv = tuple(x["value"] if isinstance(x, dict) else x for x in mv)
    known = {"name", "path", "format", "hash", "bytes", "schema", "type", "mediatype", "encoding"}
    return Resource(name=d["name"], path=d["path"], format=fmt, hash=h, bytes=d.get("bytes"), fields=fields,
                    missing_values=mv, extra={k: v for k, v in d.items() if k not in known})


def load_descriptor(path: str | os.PathLike, base: Path | None = None) -> Descriptor:
    """读取并检查描述文件的结构（不读取数据文件）。path 为描述文件或其所在目录。"""
    f = find_descriptor(path, base)
    try:
        text = f.read_text(encoding="utf-8")
        raw = json.loads(text) if f.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise DatasetError(f"无法读取数据集描述文件 {f}：{e}") from e
    if not isinstance(raw, dict):
        raise DatasetError(f"数据集描述文件 {f} 的顶层须为对象")
    where = str(f)
    for k in ("name", "version", "resources", "cpc"):
        if k not in raw:
            raise DatasetError(f"{where}：缺少 {k}")
    cpc = raw["cpc"]
    if not isinstance(cpc, dict) or "kind" not in cpc or "profile" not in cpc:
        raise DatasetError(f"{where}：cpc 须给出 kind 与 profile")
    res = raw["resources"]
    if not isinstance(res, list) or not res:
        raise DatasetError(f"{where}：resources 须为非空数组")
    resources = tuple(_resource(r, where) for r in res)
    known = {"name", "version", "title", "description", "licenses", "sources", "resources", "cpc", "$schema"}
    d = Descriptor(root=f.parent, file=f, name=str(raw["name"]), version=str(raw["version"]), kind=str(cpc["kind"]),
                   profile=int(cpc["profile"]), resources=resources, title=raw.get("title", ""),
                   description=raw.get("description", ""), licenses=tuple(raw.get("licenses") or ()),
                   sources=tuple(raw.get("sources") or ()), cpc=dict(cpc), extra={k: v for k, v in raw.items() if k not in known})
    from .profiles import check_descriptor

    check_descriptor(d)
    return d


def write_descriptor(d: dict, directory: str | os.PathLike) -> Path:
    """写出 datapackage.yaml（供数据转换脚本使用）。"""
    p = Path(directory) / "datapackage.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p
