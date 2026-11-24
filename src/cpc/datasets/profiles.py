"""数据集类别规范（cpc profile）：每类数据集的资源、核心字段与约定扩展字段。

核心字段是平台完成基本调度所必需的字段，描述文件中须声明且类型相容，数据中不得为空；
约定扩展字段有统一的名称与类型，平台的参考策略可能使用；其余字段为任意扩展，平台原样保留并按需传给策略。

| 类别           | 资源          | 核心字段                                                         |
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

# 平台使用的电网信号及其单位要求
CORE_SIGNALS = {"price": "/kWh", "carbon_intensity": "kgCO2/kWh"}


@dataclass(frozen=True)
class FieldSpec:
    types: tuple[str, ...]  # 允许的 Table Schema 类型
    unit: str | None = None  # 要求的单位（None 表示不要求）
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
        description="推理请求负载：每行一个请求",
        core={
            "arrival_s": FieldSpec(("number", "integer"), unit="s", minimum=0, description="到达时间（相对数据集零点的秒数）"),
            "input_tokens": FieldSpec(("integer",), minimum=0, description="输入（提示）token 数"),
            "output_tokens": FieldSpec(("integer",), minimum=1, description="输出 token 数"),
        },
        conventional={
            "request_id": FieldSpec(("string", "integer"), description="请求标识"),
            "model": FieldSpec(("string",), description="请求的模型名"),
            "session_id": FieldSpec(("string", "integer"), description="会话标识（多轮对话）"),
            "priority": FieldSpec(("integer",), description="优先级，数值越大越优先"),
            "slo_class": FieldSpec(("string",), description="服务等级类别"),
            "tenant": FieldSpec(("string",), description="租户或业务标识"),
            "prefix_hashes": FieldSpec(("list", "string"), description="前缀块哈希序列（KV 缓存复用分析）"),
        },
    ),
    "grid_signal": KindSpec(
        resource="signals",
        description="电网信号：按区域与时段给出的信号值（长表，每行一个区域、一个时段、一种信号）",
        core={
            "region": FieldSpec(("string",), description="区域键"),
            "start": FieldSpec(("datetime",), description="时段起点（含时区）"),
            "end": FieldSpec(("datetime",), description="时段终点（含时区，不含）"),
            "signal": FieldSpec(("string",), description="信号类型，如 price、carbon_intensity"),
            "value": FieldSpec(("number", "integer"), description="信号值，单位见描述文件 cpc.signals"),
        },
        conventional={
            "label": FieldSpec(("string",), description="时段标签，如分时电价的 peak、valley"),
            "is_estimated": FieldSpec(("boolean",), description="是否为估计值"),
        },
    ),
    "power_profile": KindSpec(
        resource="measurements",
        description="功率特性：设备在给定并发下的平均功率与 token 间隔（每行一次稳态测量）",
        core={
            "device": FieldSpec(("string",), description="设备型号，如 H100"),
            "device_count": FieldSpec(("integer",), minimum=1, description="一个推理实例占用的设备数"),
            "model": FieldSpec(("string",), description="模型名"),
            "concurrency": FieldSpec(("number", "integer"), minimum=0, description="平均并发（运行中的序列数）"),
            "power_w": FieldSpec(("number",), unit="W", minimum=0, description="全部设备的平均总功率"),
            "itl_s": FieldSpec(("number",), unit="s", exclusive_minimum=0, description="平均 token 间隔"),
        },
        conventional={
            "max_concurrency": FieldSpec(("integer",), description="并发上限（如 vLLM max_num_seqs）"),
            "throughput_tokens_per_s": FieldSpec(("number",), description="输出吞吐"),
            "ttft_s": FieldSpec(("number",), description="平均首 token 时延"),
            "energy_per_token_j": FieldSpec(("number",), description="每输出 token 能耗"),
            "task": FieldSpec(("string",), description="负载任务名"),
        },
    ),
}


def check_descriptor(d: Descriptor) -> None:
    """按类别规范检查描述文件（不读取数据）。"""
    if d.kind not in KINDS:
        raise DatasetError(f"数据集 {d.ref} 的类别 {d.kind} 未定义（可用 {sorted(KINDS)}）")
    if d.profile != PROFILE_VERSION:
        raise DatasetError(f"数据集 {d.ref} 的规范版本 {d.profile} 不受支持（当前为 {PROFILE_VERSION}）")
    spec = KINDS[d.kind]
    r = d.resource(spec.resource)
    for name, fs in {**spec.core, **spec.conventional}.items():
        f = r.field(name)
        if f is None:
            if name in spec.core:
                raise DatasetError(f"数据集 {d.ref} 的资源 {r.name} 缺少核心字段 {name}（{fs.description}）")
            continue
        if f.type not in fs.types:
            raise DatasetError(f"数据集 {d.ref} 的字段 {name} 类型为 {f.type}，规范要求 {fs.types}")
        if fs.unit and f.unit != fs.unit:
            raise DatasetError(f"数据集 {d.ref} 的字段 {name} 须声明 unit: {fs.unit}（当前为 {f.unit}）")
    if d.kind == "workload" and "origin" in d.cpc:
        try:
            o = datetime.fromisoformat(str(d.cpc["origin"]).replace("Z", "+00:00"))
        except ValueError as e:
            raise DatasetError(f"数据集 {d.ref} 的 cpc.origin 不是 ISO 8601 时间：{d.cpc['origin']}") from e
        if o.tzinfo is None:
            raise DatasetError(f"数据集 {d.ref} 的 cpc.origin 须带时区")
    if d.kind == "grid_signal":
        sig = d.cpc.get("signals")
        if not isinstance(sig, dict) or not sig:
            raise DatasetError(f"数据集 {d.ref} 须在 cpc.signals 中声明所含信号及其单位")
        for name, meta in sig.items():
            if not isinstance(meta, dict) or not meta.get("unit"):
                raise DatasetError(f"数据集 {d.ref} 的信号 {name} 须声明 unit")
            if name in CORE_SIGNALS and not str(meta["unit"]).endswith(CORE_SIGNALS[name]):
                raise DatasetError(f"数据集 {d.ref} 的信号 {name} 单位须为 *{CORE_SIGNALS[name]}，当前为 {meta['unit']}")


def extension_fields(d: Descriptor) -> list[str]:
    """核心字段以外的字段（约定扩展与任意扩展）。"""
    spec = KINDS[d.kind]
    return [f.name for f in d.resource(spec.resource).fields if f.name not in spec.core]
