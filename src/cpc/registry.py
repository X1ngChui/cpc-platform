"""插件注册：内置实现以装饰器注册；第三方实现以 Python 包的 entry points 提供。

entry point 组与类别：
    cpc.routing    跨站点路由策略（RoutingPolicy）
    cpc.capacity   跨站点容量策略（CapacityPolicy）
    cpc.dispatch   站内派发策略（DispatchPolicy）
    cpc.power      站内电源管理策略（PowerPolicy）
    cpc.sensors    功率传感器（Sensor）

第三方包在 pyproject.toml 中声明，例如：
    [project.entry-points."cpc.routing"]
    my_policy = "my_package.policies:MyPolicy"
名称冲突（与内置实现或其他包重名）时报错。
"""

from __future__ import annotations

import difflib
import inspect
from importlib.metadata import entry_points
from typing import Callable, Generic, Iterable, TypeVar

from .config import ConfigError

T = TypeVar("T")

LABELS = {"routing": "路由策略", "capacity": "容量策略", "dispatch": "派发策略", "power": "电源管理策略",
          "sensors": "功率传感器类型"}


def did_you_mean(word: str, choices: Iterable[str]) -> str:
    """与 word 最接近的候选名，形如"（是否要写 nearest？）"；没有接近的候选时为空串。"""
    m = difflib.get_close_matches(word, list(choices), n=1, cutoff=0.6)
    return f"（是否要写 {m[0]}？）" if m else ""


def accepted_params(cls: type) -> list[str]:
    """类及其基类的构造参数名（不含 self、*args、**kwargs）。"""
    out: list[str] = []
    for klass in cls.__mro__:
        init = vars(klass).get("__init__")
        if init is None or klass is object:
            continue
        for p in inspect.signature(init).parameters.values():
            if p.name != "self" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) and p.name not in out:
                out.append(p.name)
    return out


def unknown_params(label: str, obj: object, params: dict) -> ConfigError:
    """策略收到不接受的参数时的错误：列出不接受的参数、相近的参数名与可用参数。"""
    bad = sorted(params)
    ok = accepted_params(type(obj))
    hint = did_you_mean(bad[0], ok) if len(bad) == 1 else ""
    avail = f"可用参数：{'、'.join(ok)}" if ok else "该策略没有参数"
    return ConfigError(f"{label} {getattr(obj, 'name', type(obj).__name__)} 不接受参数 {'、'.join(bad)}{hint}。{avail}")


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self.kind = kind
        self.label = LABELS.get(kind, kind)
        self.group = f"cpc.{kind}"
        self._items: dict[str, type[T]] = {}
        self._loaded = False

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def deco(cls: type[T]) -> type[T]:
            if name in self._items and self._items[name] is not cls:
                raise ValueError(f"{self.kind} 名称重复：{name}")
            cls.name = name  # type: ignore[attr-defined]
            self._items[name] = cls
            return cls

        return deco

    def _load_entry_points(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for ep in entry_points(group=self.group):
            cls = ep.load()
            if ep.name in self._items and self._items[ep.name] is not cls:
                raise ValueError(f"{self.group} 的 entry point {ep.name} 与已有实现重名")
            cls.name = ep.name
            self._items[ep.name] = cls

    def get(self, name: str) -> type[T]:
        self._load_entry_points()
        if name not in self._items:
            raise ConfigError(f"未知的{self.label} {name}{did_you_mean(name, self._items)}。"
                              f"可选：{'、'.join(sorted(self._items))}")
        return self._items[name]

    def names(self) -> list[str]:
        self._load_entry_points()
        return sorted(self._items)

    def all(self) -> dict[str, type[T]]:
        """全部已注册的实现（名称 → 类），含 entry points 提供的实现。"""
        self._load_entry_points()
        return dict(self._items)
