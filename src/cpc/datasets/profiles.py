"""Dataset kind specifications (cpc profile): resource, core fields and conventional extension fields.

Core fields are required for basic scheduling: the descriptor must declare them with a compatible
type and the data must not leave them empty. Conventional extension fields have agreed names and
types that reference policies may use. Any other field is an extension kept as is and passed to
policies on request.

| kind           | resource      | core fields                                                      |
|----------------|---------------|------------------------------------------------------------------|
| workload       | requests      | arrival_s, input_tokens, output_tokens                           |
| grid_signal    | signals       | region, start, end, signal, value                                |
| power_profile  | measurements  | device, device_count, model, concurrency, power_w, itl_s         |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .descriptor import DatasetError, Descriptor

PROFILE_VERSION = 1

CORE_SIGNALS = {"price": "/kWh", "carbon_intensity": "kgCO2/kWh"}  # signals used by the platform and their unit suffix


@dataclass(frozen=True)
class FieldSpec:
    types: tuple[str, ...]  # allowed Table Schema types
    unit: str | None = None  # required unit (None: no requirement)
    minimum: float | None = None
    exclusive_minimum: float | None = None
    description: str = ""


@dataclass(frozen=True)
class KindSpec:
    resource: str
    core: dict[str, FieldSpec]
    conventional: dict[str, FieldSpec] = field(default_factory=dict)
    description: str = ""


KINDS: dict[str, KindSpec] = {
    "workload": KindSpec(
        resource="requests",
        description="inference request workload, one request per row",
        core={
            "arrival_s": FieldSpec(("number", "integer"), unit="s", minimum=0, description="arrival time (s from the dataset origin)"),
            "input_tokens": FieldSpec(("integer",), minimum=0, description="input (prompt) tokens"),
            "output_tokens": FieldSpec(("integer",), minimum=1, description="output tokens"),
        },
        conventional={
            "request_id": FieldSpec(("string", "integer"), description="request identifier"),
            "model": FieldSpec(("string",), description="requested model"),
            "session_id": FieldSpec(("string", "integer"), description="session identifier (multi-turn conversations)"),
            "priority": FieldSpec(("integer",), description="priority, higher first"),
            "slo_class": FieldSpec(("string",), description="service class"),
            "tenant": FieldSpec(("string",), description="tenant or application"),
            "prefix_hashes": FieldSpec(("list", "string"), description="prefix block hashes (KV-cache reuse analysis)"),
        },
    ),
    "grid_signal": KindSpec(
        resource="signals",
        description="grid signals per region and period (long table: one region, period and signal per row)",
        core={
            "region": FieldSpec(("string",), description="region key"),
            "start": FieldSpec(("datetime",), description="period start (with time zone)"),
            "end": FieldSpec(("datetime",), description="period end (with time zone, exclusive)"),
            "signal": FieldSpec(("string",), description="signal, e.g. price, carbon_intensity"),
            "value": FieldSpec(("number", "integer"), description="value; units in cpc.signals of the descriptor"),
        },
        conventional={
            "label": FieldSpec(("string",), description="period label, e.g. peak or valley of a time-of-use tariff"),
            "is_estimated": FieldSpec(("boolean",), description="whether the value is an estimate"),
        },
    ),
    "power_profile": KindSpec(
        resource="measurements",
        description="power characteristics: mean power and inter-token latency at a given concurrency "
                    "(one steady-state measurement per row)",
        core={
            "device": FieldSpec(("string",), description="device model, e.g. H100"),
            "device_count": FieldSpec(("integer",), minimum=1, description="devices used by one inference instance"),
            "model": FieldSpec(("string",), description="model name"),
            "concurrency": FieldSpec(("number", "integer"), minimum=0, description="mean concurrency (running sequences)"),
            "power_w": FieldSpec(("number",), unit="W", minimum=0, description="mean total power of all devices"),
            "itl_s": FieldSpec(("number",), unit="s", exclusive_minimum=0, description="mean inter-token latency"),
        },
        conventional={
            "max_concurrency": FieldSpec(("integer",), description="concurrency limit (e.g. vLLM max_num_seqs)"),
            "throughput_tokens_per_s": FieldSpec(("number",), description="output throughput"),
            "ttft_s": FieldSpec(("number",), description="mean time to first token"),
            "energy_per_token_j": FieldSpec(("number",), description="energy per output token"),
            "task": FieldSpec(("string",), description="workload task"),
        },
    ),
}


def check_descriptor(d: Descriptor) -> None:
    """Check a descriptor against its kind specification (without reading data)."""
    if d.kind not in KINDS:
        raise DatasetError(f"dataset {d.ref}: undefined kind {d.kind} (available: {sorted(KINDS)})")
    if d.profile != PROFILE_VERSION:
        raise DatasetError(f"dataset {d.ref}: unsupported profile version {d.profile} (current: {PROFILE_VERSION})")
    spec = KINDS[d.kind]
    r = d.resource(spec.resource)
    for name, fs in (spec.core | spec.conventional).items():
        if (f := r.field(name)) is None:
            if name in spec.core:
                raise DatasetError(f"dataset {d.ref}: resource {r.name} lacks the core field {name} ({fs.description})")
            continue
        if f.type not in fs.types:
            raise DatasetError(f"dataset {d.ref}: field {name} has type {f.type}; the specification requires {fs.types}")
        if fs.unit and f.unit != fs.unit:
            raise DatasetError(f"dataset {d.ref}: field {name} must declare unit: {fs.unit} (got {f.unit})")
    match d.kind:
        case "workload" if "origin" in d.cpc:
            try:
                origin = datetime.fromisoformat(str(d.cpc["origin"]).replace("Z", "+00:00"))
            except ValueError as e:
                raise DatasetError(f"dataset {d.ref}: cpc.origin is not an ISO 8601 time: {d.cpc['origin']}") from e
            if origin.tzinfo is None:
                raise DatasetError(f"dataset {d.ref}: cpc.origin needs a UTC offset")
        case "grid_signal":
            if not isinstance(sig := d.cpc.get("signals"), dict) or not sig:
                raise DatasetError(f"dataset {d.ref}: declare the signals and their units in cpc.signals")
            for name, meta in sig.items():
                if not isinstance(meta, dict) or not meta.get("unit"):
                    raise DatasetError(f"dataset {d.ref}: signal {name} needs a unit")
                if name in CORE_SIGNALS and not str(meta["unit"]).endswith(CORE_SIGNALS[name]):
                    raise DatasetError(f"dataset {d.ref}: the unit of signal {name} must be *{CORE_SIGNALS[name]}, "
                                       f"got {meta['unit']}")


def extension_fields(d: Descriptor) -> list[str]:
    """Fields other than the core fields (conventional and arbitrary extensions)."""
    spec = KINDS[d.kind]
    return [f.name for f in d.resource(spec.resource).fields if f.name not in spec.core]
