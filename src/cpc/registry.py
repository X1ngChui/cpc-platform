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

from importlib.metadata import entry_points
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self.kind = kind
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
            raise KeyError(f"未知{self.kind} {name}，可选：{sorted(self._items)}")
        return self._items[name]

    def names(self) -> list[str]:
        self._load_entry_points()
        return sorted(self._items)

    def items(self) -> dict[str, type[T]]:
        self._load_entry_points()
        return dict(self._items)
