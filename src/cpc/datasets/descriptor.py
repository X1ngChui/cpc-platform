"""Dataset descriptors: a subset of Frictionless Data Package v2 plus the platform property `cpc`.

The descriptor is `datapackage.yaml` or `datapackage.json` in the dataset directory. The platform reads:

    name, version, title, description, licenses, sources, contributors   package metadata (Data Package v2)
    resources[]: name, path, format, hash, bytes, schema                 resources (Data Resource v2); path is relative
    resources[].schema: fields[], missingValues                           table schema (Table Schema v2)
    fields[]: name, type, format, constraints, unit, description          fields; unit is a custom property
    cpc: {kind, profile, ...}                                             dataset kind, profile version, kind properties

hash is optional provenance in the "algorithm prefix" form of Data Package v2 (e.g. `sha256:<hex>`);
the platform does not verify files against it. Other properties of the descriptor, resources and
fields are kept in extra and not interpreted.
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
FIELD_TYPES = frozenset({"string", "number", "integer", "boolean", "datetime", "date", "list", "object", "array", "any"})
CONSTRAINTS = frozenset({"required", "unique", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "enum",
                         "pattern", "minLength", "maxLength"})
HASH_RE = re.compile(r"^[a-z0-9]+:[0-9a-f]+$")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class DatasetError(Exception):
    """Unusable dataset: invalid descriptor, missing file, failed validation or kind specification violated."""


def _extra(d: dict, known: set[str]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in known}


@dataclass(frozen=True)
class Field:
    name: str
    type: str = "any"
    format: str = "default"
    constraints: dict[str, Any] = field(default_factory=dict)
    unit: str | None = None
    description: str = ""
    item_type: str | None = None  # element type of list fields (Table Schema itemType)
    delimiter: str = ","  # separator of list fields in CSV
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> bool:
        return bool(self.constraints.get("required"))


@dataclass(frozen=True)
class Resource:
    name: str
    path: str  # relative to the descriptor directory
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
    root: Path  # directory of the descriptor
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
    cpc: dict[str, Any] = field(default_factory=dict)  # the cpc property (kind, profile and kind properties)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    def resource(self, name: str) -> Resource:
        if (r := next((r for r in self.resources if r.name == name), None)) is None:
            raise DatasetError(f"dataset {self.ref} has no resource {name} (it has {[x.name for x in self.resources]})")
        return r

    def local_path(self, r: Resource) -> Path:
        """Local path of a resource (inside the descriptor directory)."""
        if (p := Path(r.path)).is_absolute() or ".." in p.parts:
            raise DatasetError(f"dataset {self.ref}: the path of resource {r.name} must be relative and inside "
                               f"the dataset directory: {r.path}")
        return self.root / p

    def summary(self) -> dict:
        return {"name": self.name, "version": self.version, "kind": self.kind, "profile": self.profile, "title": self.title,
                "licenses": list(self.licenses), "sources": list(self.sources),
                "resources": [{"name": r.name, "path": r.path, "format": r.format, "hash": r.hash, "bytes": r.bytes,
                               "fields": [f.name for f in r.fields]} for r in self.resources],
                "cpc": self.cpc}


def data_dir() -> Path:
    """Dataset directory: CPC_DATA_DIR, default ~/.cache/cpc/datasets."""
    return Path(os.environ.get("CPC_DATA_DIR") or Path.home() / ".cache" / "cpc" / "datasets")


def expand(path: str | os.PathLike, base: Path | None = None) -> Path:
    """Expand ~ and ${VAR}; ${CPC_DATA_DIR} defaults to data_dir(), other undefined variables are errors.
    Relative paths resolve against base."""
    s = str(path).replace("${CPC_DATA_DIR}", str(data_dir()))
    if undefined := [v for v in ENV_RE.findall(s) if v not in os.environ]:
        raise DatasetError(f"path {s} refers to the undefined environment variable {undefined[0]}")
    p = Path(os.path.expandvars(s)).expanduser()
    return base / p if base is not None and not p.is_absolute() else p


def find_descriptor(path: str | os.PathLike, base: Path | None = None) -> Path:
    p = expand(path, base)
    if p.is_dir():
        if not (found := [p / n for n in DESCRIPTOR_NAMES if (p / n).is_file()]):
            raise DatasetError(f"directory {p} has no dataset descriptor ({' / '.join(DESCRIPTOR_NAMES)})")
        return found[0]
    if p.is_file():
        return p
    hint = f"; ${{CPC_DATA_DIR}} is {data_dir()}" if "${CPC_DATA_DIR}" in str(path) else ""
    raise DatasetError(f"dataset {p} does not exist: prepare it first (see the guide, chapter \"Data\"){hint}")


def _field(d: dict, where: str) -> Field:
    if "name" not in d:
        raise DatasetError(f"{where}: a field has no name")
    if (t := d.get("type", "any")) not in FIELD_TYPES:
        raise DatasetError(f"{where}: field {d['name']} has the unsupported type {t} (supported: {sorted(FIELD_TYPES)})")
    cons = dict(d.get("constraints") or {})
    if unknown := set(cons) - CONSTRAINTS:
        raise DatasetError(f"{where}: field {d['name']} has unsupported constraints {sorted(unknown)}")
    return Field(name=d["name"], type=t, format=d.get("format", "default"), constraints=cons, unit=d.get("unit"),
                 description=d.get("description", ""), item_type=d.get("itemType"), delimiter=d.get("delimiter", ","),
                 extra=_extra(d, {"name", "type", "format", "constraints", "unit", "description", "itemType", "delimiter"}))


def _resource(d: dict, where: str) -> Resource:
    if missing := [k for k in ("name", "path") if k not in d]:
        raise DatasetError(f"{where}: a resource has no {missing[0]} (resources must be files given by path; "
                           "inline data are not supported)")
    name = d["name"]
    if not isinstance(d["path"], str):
        raise DatasetError(f"{where}: the path of resource {name} must be a single file")
    if (fmt := d.get("format") or d["path"].rsplit(".", 1)[-1].lower()) not in ("csv", "parquet"):
        raise DatasetError(f"{where}: resource {name} has the unsupported format {fmt} (csv, parquet)")
    if (h := d.get("hash")) is not None and not HASH_RE.match(h):
        raise DatasetError(f"{where}: the hash of resource {name} must be <algorithm>:<hex digest>, got {h}")
    if not isinstance(schema := d.get("schema"), dict) or not schema.get("fields"):
        raise DatasetError(f"{where}: resource {name} needs an inline schema.fields")
    fields = tuple(_field(f, f"{where}, resource {name}") for f in schema["fields"])
    if len({f.name for f in fields}) != len(fields):
        raise DatasetError(f"{where}: resource {name} has duplicate field names")
    missing_values = tuple(x["value"] if isinstance(x, dict) else x for x in schema.get("missingValues", [""]))
    return Resource(name=name, path=d["path"], format=fmt, hash=h, bytes=d.get("bytes"), fields=fields,
                    missing_values=missing_values,
                    extra=_extra(d, {"name", "path", "format", "hash", "bytes", "schema", "type", "mediatype", "encoding"}))


def load_descriptor(path: str | os.PathLike, base: Path | None = None) -> Descriptor:
    """Read a descriptor and check its structure and kind (without reading the data files).
    path is the descriptor or its directory."""
    f = find_descriptor(path, base)
    try:
        text = f.read_text(encoding="utf-8")
        raw = json.loads(text) if f.suffix == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise DatasetError(f"cannot read the dataset descriptor {f}: {e}") from e
    if not isinstance(raw, dict):
        raise DatasetError(f"the top level of the dataset descriptor {f} must be an object")
    if missing := [k for k in ("name", "version", "resources", "cpc") if k not in raw]:
        raise DatasetError(f"{f}: {missing[0]} is missing")
    if not isinstance(cpc := raw["cpc"], dict) or "kind" not in cpc or "profile" not in cpc:
        raise DatasetError(f"{f}: cpc needs kind and profile")
    if not isinstance(res := raw["resources"], list) or not res:
        raise DatasetError(f"{f}: resources must be a non-empty array")
    d = Descriptor(root=f.parent, file=f, name=str(raw["name"]), version=str(raw["version"]), kind=str(cpc["kind"]),
                   profile=int(cpc["profile"]), resources=tuple(_resource(r, str(f)) for r in res),
                   title=raw.get("title", ""), description=raw.get("description", ""),
                   licenses=tuple(raw.get("licenses") or ()), sources=tuple(raw.get("sources") or ()), cpc=dict(cpc),
                   extra=_extra(raw, {"name", "version", "title", "description", "licenses", "sources", "resources",
                                      "cpc", "$schema"}))
    from .profiles import check_descriptor

    check_descriptor(d)
    return d


def write_descriptor(d: dict, directory: str | os.PathLike) -> Path:
    """Write datapackage.yaml (used by conversion scripts)."""
    p = Path(directory) / "datapackage.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p
