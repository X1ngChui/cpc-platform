"""Scenario and endpoint configuration (YAML).

A scenario describes the system under study, independently of how it is run (simulation or live
experiment) and how it is deployed:

    sources          regions where user requests originate
    node_profiles    server types: power and performance model parameters
    sites            data centres: location, PUE, grid region, server pools and local policies
    network          round-trip latency model between source regions and sites
    grid             grid signals (price, carbon intensity) and demand-response events
    workload         workload dataset and explicit transformations
    slo, scheduler   service objectives; global policies and runtime parameters

An endpoints file gives the service addresses of a live deployment. Configuration errors raise
ConfigError while loading. Relative paths resolve against the directory of the scenario file and
may use ${CPC_DATA_DIR} (default ~/.cache/cpc/datasets) and other environment variables.
"""

from __future__ import annotations

import os
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CST = timezone(timedelta(hours=8))


class ConfigError(Exception):
    """Configuration or environment error, reported at startup."""


def did_you_mean(word: str, choices) -> str:
    """Suffix naming the closest candidate, e.g. " (did you mean 'nearest'?)"; empty if none is close."""
    import difflib

    m = difflib.get_close_matches(word, list(choices), n=1, cutoff=0.6)
    return f" (did you mean '{m[0]}'?)" if m else ""


def duplicates(items) -> list:
    """Items that occur more than once, sorted."""
    return sorted(k for k, n in Counter(items).items() if n > 1)


# ---------------------------------------------------------------- validation messages

_TIME_HINT = "should be a time with a UTC offset, e.g. 2026-07-15T11:00:00+08:00"
_ERROR_TEXT = {
    "missing": "required field is missing",
    "string_type": "should be a string",
    **dict.fromkeys(("int_type", "int_parsing", "int_from_float"), "should be an integer"),
    **dict.fromkeys(("float_type", "float_parsing"), "should be a number"),
    **dict.fromkeys(("bool_type", "bool_parsing"), "should be true or false"),
    **dict.fromkeys(("list_type", "tuple_type"), "should be a list"),
    **dict.fromkeys(("dict_type", "model_type", "model_attributes_type"), "should be a mapping (key: value)"),
    **dict.fromkeys(("datetime_type", "datetime_parsing", "datetime_from_date_parsing"), _TIME_HINT),
    "timezone_aware": "needs a UTC offset, e.g. +08:00",
}
_BOUNDS = {"greater_than": ("gt", "should be greater than"), "greater_than_equal": ("ge", "should be at least"),
           "less_than": ("lt", "should be less than"), "less_than_equal": ("le", "should be at most")}


def _unwrap(t: Any) -> Any:
    """X | None -> X."""
    if get_origin(t) in (Union, types.UnionType):
        args = [a for a in get_args(t) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return t


def _is_model(t: Any) -> bool:
    return isinstance(t, type) and issubclass(t, BaseModel)


def _fields_at(model: type[BaseModel], loc: tuple) -> list[str]:
    """Field names of the model found by following the field path loc; empty if it leads elsewhere."""
    t: Any = model
    for part in loc:
        t = _unwrap(t)
        if _is_model(t):
            if not isinstance(part, str) or (f := t.model_fields.get(part)) is None:
                return []
            t = f.annotation
        elif get_origin(t) in (list, tuple, dict) and (args := get_args(t)):
            t = args[-1]
        else:
            return []
    t = _unwrap(t)
    return [k for k, f in t.model_fields.items() if not f.exclude] if _is_model(t) else []


def _path(loc: tuple) -> str:
    return "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc).lstrip(".")


def _short(v: Any) -> str:
    r = repr(v)
    return r if len(r) <= 40 else r[:37] + "..."


def _message(err: dict, model: type[BaseModel]) -> str:
    typ, loc, ctx = err["type"], tuple(err["loc"]), err.get("ctx") or {}
    match typ:
        case "extra_forbidden":
            ok = _fields_at(model, loc[:-1])
            return "unknown field" + (did_you_mean(str(loc[-1]), ok) or (f"; valid fields: {', '.join(ok)}" if ok else ""))
        case "value_error" | "assertion_error" if "error" in ctx:
            return str(ctx["error"])
        case _ if typ in _ERROR_TEXT:
            msg = _ERROR_TEXT[typ]
        case _ if typ in _BOUNDS:
            key, text = _BOUNDS[typ]
            v = ctx.get(key)
            msg = f"{text} {v:g}" if isinstance(v, (int, float)) else f"{text} {v}"
        case "too_short":
            msg = f"needs at least {ctx.get('min_length')} item(s)"
        case "literal_error" | "enum":
            msg = f"should be {ctx.get('expected')}"
        case _:
            msg = err["msg"]
    if typ != "missing" and isinstance(err.get("input"), (str, int, float, bool, type(None))):
        msg += f" (got {_short(err['input'])})"
    return msg


def describe_validation(e: ValidationError, model: type[BaseModel], what: str) -> str:
    """Turn a pydantic validation error into one line per problem: field path and reason."""
    lines = [f"{where}: {msg}" if (where := _path(tuple(err["loc"]))) else msg
             for err in e.errors() for msg in [_message(err, model)]]
    if len(lines) == 1:
        return f"{what}: {lines[0]}"
    return f"{what} has {len(lines)} errors:\n" + "\n".join(f"  {line}" for line in lines)


def parse_yaml(text: str, what: str) -> dict:
    """Parse a YAML mapping; raise ConfigError on syntax errors or a non-mapping top level."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{what} is not valid YAML: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"the top level of {what} should be a mapping (key: value)")
    return raw


def read_yaml(path: Path, what: str) -> dict:
    """Read a YAML mapping from a file; raise ConfigError if it cannot be read or parsed."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"cannot read {what} {path}: {e.strerror or e}") from e
    return parse_yaml(text, f"{what} {path}")


def validate(model: type[BaseModel], raw: dict, what: str):
    """model.model_validate(raw), reporting errors as ConfigError."""
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(describe_validation(e, model, what)) from e


# ---------------------------------------------------------------- scenario


class _Strict(BaseModel):
    """Rejects unknown fields, so misspelt or obsolete fields fail loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


class PolicyConfig(_Strict):
    policy: str
    params: dict[str, Any] = Field(default_factory=dict)


Table = list[tuple[float, float]]


def _check_table(name: str, t: Table, from_zero: bool) -> None:
    xs = [x for x, _ in t]
    if not t or xs != sorted(set(xs)) or xs[0] < 0 or xs[-1] > 1:
        raise ValueError(f"{name}: occupancy must increase strictly within [0, 1], got {xs}")
    if from_zero and xs[0] != 0:
        raise ValueError(f"{name}: the first point must be at occupancy 0 (idle power), got {xs[0]}")
    if any(y < 0 for _, y in t):
        raise ValueError(f"{name}: values must not be negative")


class NodeProfile(_Strict):
    """Server type. Power and time per output token are tables over occupancy, interpolated linearly.

    Occupancy u = requests in service / concurrency slots.
    power_w   [[u, server IT power W], ...]; the first point is u = 0 (online, no requests)
    tpot_s    [[u, time per output token s], ...]
    Values outside a table take the end points. Tables come from measurements (cpc profile) or
    from any formula evaluated beforehand.
    """

    description: str = ""  # hardware, model and data source
    slots: int = Field(ge=1)  # concurrency slots (upper bound of the continuous batch)
    power_w: Table
    tpot_s: Table
    prefill_tokens_per_s: float = Field(gt=0)
    boot_s: float = Field(ge=0)  # time from power-on until the server can serve
    off_w: float = Field(0.0, ge=0)  # power while off
    cap_min_ratio: float = Field(0.5, gt=0, le=1)  # lowest power-cap ratio
    cap_perf_exponent: float = Field(0.4, ge=0)  # relative speed under a cap = ratio ** exponent

    @model_validator(mode="after")
    def _tables(self) -> NodeProfile:
        _check_table("power_w", self.power_w, from_zero=True)
        _check_table("tpot_s", self.tpot_s, from_zero=False)
        if len(self.power_w) < 2:
            raise ValueError("power_w needs at least 2 points (idle and at least one busy occupancy)")
        if any(y <= 0 for _, y in self.tpot_s):
            raise ValueError("tpot_s values must be positive")
        return self


class SourceRegion(_Strict):
    """Region where user requests originate."""

    id: str
    name: str
    lat: float
    lon: float
    share: float = 1.0  # share of requests (normalised)


class UpstreamConfig(_Strict):
    """A real inference instance (e.g. one vLLM process): one server of a site.

    The accelerators it uses are named as the sensors report them: gpu_indices [0, 1] is shorthand
    for devices [gpu0, gpu1] (NVML); devices names accelerators of other sensors, including plugins.
    CPU-only instances give neither.
    """

    id: str
    url: str
    gpu_indices: list[int] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)
    container: str | None = None  # Docker container started and stopped as power on/off; None: logical switch
    max_inflight: int = Field(16, ge=1)  # concurrency limit (the slots of this server)
    ready_path: str = "/health"

    @model_validator(mode="after")
    def _one_of(self) -> UpstreamConfig:
        if self.gpu_indices and self.devices:
            raise ValueError(f"upstream {self.id}: give gpu_indices or devices, not both")
        return self

    @property
    def device_names(self) -> list[str]:
        return self.devices or [f"gpu{i}" for i in self.gpu_indices]


class MeasurementConfig(_Strict):
    """Power measurement of the real servers of a site (sensors read by the site agent).

    sensors  list of sensors, e.g. [{type: nvml}, {type: ipmi}]; instances of one type differ by name
    host     how the host part of server power is obtained: server = whole-server reading minus
             accelerators; cpu_dram = sum of CPU and memory readings; excluded = not counted
             (server IT power covers the accelerators only)
    """

    sensors: list[dict[str, Any]]
    host: Literal["server", "cpu_dram", "excluded"]

    @model_validator(mode="after")
    def _names(self) -> MeasurementConfig:
        if missing := [s for s in self.sensors if "type" not in s]:
            raise ValueError(f"sensor without a type: {missing[0]}")
        if dup := duplicates(s.get("name") or s["type"] for s in self.sensors):
            raise ValueError(f"duplicate sensor names {dup} (give sensors of the same type distinct names)")
        return self


class PoolConfig(_Strict):
    """A group of servers of one type.

    backend: emulated  servers emulated with the profile; count gives their number
    backend: openai    each upstream is a real server (OpenAI-compatible inference instance) whose
                       power is measured by the sensors of the site
    In simulation a real pool is emulated with its profile, one server per upstream.
    """

    id: str
    profile: str
    backend: Literal["emulated", "openai"] = "emulated"
    count: int = 0
    upstreams: list[UpstreamConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _backend(self) -> PoolConfig:
        if self.backend == "emulated" and (self.count < 1 or self.upstreams):
            raise ValueError(f"pool {self.id}: an emulated pool needs count >= 1 and no upstreams")
        if self.backend == "openai" and (not self.upstreams or self.count):
            raise ValueError(f"pool {self.id}: a real pool needs upstreams and no count")
        return self

    @property
    def size(self) -> int:
        return self.count if self.backend == "emulated" else len(self.upstreams)


class SiteScheduler(_Strict):
    """Local scheduling: dispatch (request -> server) and power management (server on/off)."""

    dispatch: PolicyConfig = PolicyConfig(policy="least_loaded")
    power: PolicyConfig = PolicyConfig(policy="ordered")


class SiteConfig(_Strict):
    """A site (data centre)."""

    id: str
    name: str
    region: str  # grid region: default region key in the grid-signal datasets
    lat: float
    lon: float
    pue: float = Field(1.3, ge=1)
    pools: list[PoolConfig]
    measurement: MeasurementConfig | None = None  # required when the site has real pools
    min_active: int = Field(1, ge=0)
    grid_cap_kw: float | None = Field(None, gt=0)  # facility power limit
    scheduler: SiteScheduler = SiteScheduler()

    @property
    def size(self) -> int:
        return sum(p.size for p in self.pools)

    @property
    def real(self) -> bool:
        return any(p.backend == "openai" for p in self.pools)

    @property
    def upstreams(self) -> list[UpstreamConfig]:
        return [u for p in self.pools for u in p.upstreams]


class ClockConfig(_Strict):
    """Signal clock: signal time = start + elapsed time x speedup (speedup has no effect in simulation)."""

    start: datetime = datetime(2026, 7, 15, 0, 0, tzinfo=CST)
    speedup: float = Field(1.0, gt=0)

    @model_validator(mode="after")
    def _tz(self) -> ClockConfig:
        if self.start.tzinfo is None:
            raise ValueError("clock.start needs a UTC offset")
        return self


class NetworkConfig(_Strict):
    """Wide-area latency model: RTT = base_ms + 2 x distance x route_factor / km_per_ms."""

    base_ms: float = 4.0
    route_factor: float = 1.5
    km_per_ms: float = 200.0
    emulate_wan: bool = True  # whether the gateway injects the RTT in live experiments
    default_source: str | None = None  # source region of requests without an x-cpc-source header


class DREvent(_Strict):
    """Demand-response event: the facility power of the site must not exceed cap_kw during the period."""

    site: str
    start: datetime
    end: datetime
    cap_kw: float = Field(gt=0)
    kind: str = "peak_shaving"

    @model_validator(mode="after")
    def _period(self) -> DREvent:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start and end of a demand-response event need a UTC offset")
        if self.end <= self.start:
            raise ValueError(f"a demand-response event must end after it starts: {self.start} - {self.end}")
        return self


class SignalSource(_Strict):
    """Source of one grid signal: grid_signal dataset, signal name and site -> region map (default: site region)."""

    dataset: str
    signal: str
    regions: dict[str, str] = Field(default_factory=dict)


class ExtraSignal(SignalSource):
    """Additional grid signal passed unchanged to policies as GridSignal.extra[name]."""

    name: str


class GridConfig(_Strict):
    price: SignalSource
    carbon: SignalSource
    extras: list[ExtraSignal] = Field(default_factory=list)
    dr_events: list[DREvent] = Field(default_factory=list)


class WorkloadConfig(_Strict):
    """Workload dataset, extension fields passed to policies, and explicit transformations.

    attributes names extension fields of the dataset that policies see as RequestInfo.attrs (carried
    as x-cpc-attr-* headers in live experiments). The time window is in original trace seconds.
    """

    dataset: str
    attributes: list[str] = Field(default_factory=list)
    start_s: float = Field(0.0, ge=0)
    duration_s: float | None = Field(None, gt=0)
    time_scale: float = Field(1.0, gt=0)
    rate_scale: float = Field(1.0, gt=0)
    seed: int = 1


class SLOConfig(_Strict):
    ttft_s: float = Field(2.0, gt=0)
    tpot_s: float = Field(0.15, gt=0)


RetryCause = Literal["overloaded", "connect_error"]


class RetryConfig(_Strict):
    """Gateway retries: overloaded (site returned 503) and connect_error; at most max_attempts sites."""

    max_attempts: int = Field(3, ge=1)
    retry_on: list[RetryCause] = Field(default_factory=lambda: ["overloaded", "connect_error"])


class SchedulerConfig(_Strict):
    """Global scheduling and runtime parameters."""

    routing: PolicyConfig = PolicyConfig(policy="nearest")
    capacity: PolicyConfig = PolicyConfig(policy="static")
    control_interval_s: float = Field(15.0, gt=0)  # capacity control period in live experiments
    sim_control_interval_s: float = Field(300.0, gt=0)  # capacity control period in simulation
    carbon_price_per_kg: float = Field(0.1, ge=0)  # carbon price (currency of the price signal per kgCO2)
    queue_limit_per_slot: float = Field(4.0, gt=0)  # site queue limit = max(online slots, largest server) x this
    rate_window_s: float = Field(60.0, gt=0)  # time constant of the arrival-rate estimate
    retry: RetryConfig = RetryConfig()
    state_refresh_s: float = Field(1.0, gt=0)
    state_stale_s: float = Field(5.0, gt=0)
    # Longest gap between two chunks of a streaming response before the request is aborted as failed.
    stream_idle_timeout_s: float = Field(120.0, gt=0)


class Scenario(_Strict):
    name: str = "scenario"
    clock: ClockConfig = ClockConfig()
    sources: list[SourceRegion]
    node_profiles: dict[str, NodeProfile]
    sites: list[SiteConfig]
    network: NetworkConfig = NetworkConfig()
    grid: GridConfig
    workload: WorkloadConfig
    slo: SLOConfig = SLOConfig()
    scheduler: SchedulerConfig = SchedulerConfig()

    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        ids = [s.id for s in self.sites]
        if dup := duplicates(ids):
            raise ValueError(f"duplicate site ids: {dup}")
        if dup := duplicates(s.id for s in self.sources):
            raise ValueError(f"duplicate source region ids: {dup}")
        for s in self.sites:
            self._check_site(s)
        if (src := self.network.default_source) is not None and src not in {s.id for s in self.sources}:
            raise ValueError(f"network.default_source refers to an unknown source region: {src}")
        if bad := sorted({e.site for e in self.grid.dr_events} - set(ids)):
            raise ValueError(f"demand-response events refer to unknown sites: {bad}")
        signals = [("grid.price", self.grid.price), ("grid.carbon", self.grid.carbon),
                   *((f"grid.extras[{x.name}]", x) for x in self.grid.extras)]
        for name, src in signals:
            if bad := sorted(set(src.regions) - set(ids)):
                raise ValueError(f"{name}.regions refers to unknown sites: {bad}")
        if dup := duplicates(x.name for x in self.grid.extras):
            raise ValueError(f"duplicate grid.extras names: {dup}")
        return self

    def _check_site(self, s: SiteConfig) -> None:
        pids = [p.id for p in s.pools]
        if not pids or duplicates(pids):
            raise ValueError(f"site {s.id}: pools must be non-empty with distinct ids")
        if bad := [p.id for p in s.pools if p.profile not in self.node_profiles]:
            raise ValueError(f"site {s.id}: pools {bad} refer to unknown node profiles")
        if s.real != (s.measurement is not None):
            raise ValueError(f"site {s.id}: measurement is required with real pools and not allowed otherwise")
        ups = s.upstreams
        if s.measurement is not None and s.measurement.host == "excluded" and (
                bare := [u.id for u in ups if not u.device_names]):
            raise ValueError(f"site {s.id}: with measurement.host excluded, server power covers accelerators only, so "
                             f"upstreams {bare} need gpu_indices or devices; for CPU-only instances set host to "
                             "server or cpu_dram")
        if duplicates(u.id for u in ups) or duplicates(d for u in ups for d in u.device_names):
            raise ValueError(f"site {s.id}: duplicate upstream ids or accelerators")

    def site(self, site_id: str) -> SiteConfig:
        if (s := next((s for s in self.sites if s.id == site_id), None)) is None:
            raise KeyError(site_id)
        return s

    def region(self, site_id: str, src: SignalSource) -> str:
        return src.regions.get(site_id) or self.site(site_id).region


def scenario_from_dict(raw: dict, what: str, base_dir: Path | None = None) -> Scenario:
    return validate(Scenario, {**raw, "base_dir": base_dir or Path.cwd()}, what)


def load_scenario(path: str | os.PathLike) -> Scenario:
    path = Path(path).resolve()
    return scenario_from_dict(read_yaml(path, "scenario file"), f"scenario file {path}", path.parent)


def scenario_from_text(text: str, what: str, base_dir: Path | None = None) -> Scenario:
    """Build a scenario from the text of a scenario file (e.g. fetched from the control node)."""
    return scenario_from_dict(parse_yaml(text, what), what, base_dir)


def override_policy(current: PolicyConfig, name: str | None, params: dict | None) -> PolicyConfig:
    """Override a global policy: a name selects that policy with params; params alone update the current policy."""
    if name is not None:
        return PolicyConfig(policy=name, params=params or {})
    if params is not None:
        return PolicyConfig(policy=current.policy, params=current.params | params)
    return current


# ---------------------------------------------------------------- endpoints


class Endpoints(_Strict):
    """Service addresses of a live deployment."""

    grid: str
    gateway: str
    controller: str
    sites: dict[str, str]


def host_port(url: str) -> tuple[str, int]:
    u = urlparse(url)
    return u.hostname or "127.0.0.1", u.port or 80


def default_local_endpoints(scn: Scenario, host: str = "127.0.0.1") -> Endpoints:
    """Local addresses: grid 9000, gateway 8080, controller 9300, sites from 9101 in scenario order."""
    return Endpoints(grid=f"http://{host}:9000", gateway=f"http://{host}:8080", controller=f"http://{host}:9300",
                     sites={s.id: f"http://{host}:{9101 + i}" for i, s in enumerate(scn.sites)})


def load_endpoints(path: str | os.PathLike | None, scn: Scenario, local: bool = False) -> Endpoints:
    """Read the endpoints file (argument or CPC_ENDPOINTS); local=True uses the local addresses instead."""
    path = path or os.environ.get("CPC_ENDPOINTS")
    if local:
        if path:
            raise ConfigError(f"--local uses the local addresses; do not also give the endpoints file {path}")
        return default_local_endpoints(scn)
    if not path:
        raise ConfigError("no service addresses: give an endpoints file with -e or CPC_ENDPOINTS, or use --local")
    ep = validate(Endpoints, read_yaml(Path(path), "endpoints file"), f"endpoints file {path}")
    if set(ep.sites) != (ids := {s.id for s in scn.sites}):
        raise ConfigError(f"endpoints file {path}: sites {sorted(ep.sites)} differ from the scenario {sorted(ids)}")
    return ep
