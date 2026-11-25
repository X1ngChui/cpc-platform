"""Plugin registry: built-in implementations register with a decorator; third-party ones as entry points.

Entry-point groups and the interface each provides:
    cpc.routing    global routing policy          (cpc.sched.RoutingPolicy)
    cpc.capacity   global capacity policy         (cpc.sched.CapacityPolicy)
    cpc.dispatch   local dispatch policy          (cpc.sched.DispatchPolicy)
    cpc.power      local power-management policy  (cpc.sched.PowerPolicy)
    cpc.sensors    power sensor                   (cpc.sensors.Sensor)

A package declares its implementations in pyproject.toml, for example:
    [project.entry-points."cpc.routing"]
    my_policy = "my_package.policies:MyPolicy"
A name that clashes with a built-in or another package is an error.
"""

from __future__ import annotations

import inspect
from importlib.metadata import entry_points
from typing import Callable, Generic, TypeVar

from .config import ConfigError, did_you_mean

T = TypeVar("T")

LABELS = {"routing": "routing policy", "capacity": "capacity policy", "dispatch": "dispatch policy",
          "power": "power policy", "sensors": "sensor type"}

__all__ = ["LABELS", "Registry", "accepted_params", "did_you_mean", "unknown_params"]


def accepted_params(cls: type) -> list[str]:
    """Keyword parameters accepted by the constructors of cls and its bases (without self, *args, **kwargs)."""
    names: dict[str, None] = {}
    for klass in cls.__mro__:
        if klass is object or (init := vars(klass).get("__init__")) is None:
            continue
        names |= {p.name: None for p in inspect.signature(init).parameters.values()
                  if p.name != "self" and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    return list(names)


def unknown_params(label: str, obj: object, params: dict) -> ConfigError:
    """Error for parameters a plugin does not accept: lists them, a close match and the accepted ones."""
    bad = sorted(params)
    ok = accepted_params(type(obj))
    hint = did_you_mean(bad[0], ok) if len(bad) == 1 else ""
    avail = f"accepted: {', '.join(ok)}" if ok else "it takes no parameters"
    name = getattr(obj, "name", type(obj).__name__)
    return ConfigError(f"{label} {name} does not accept {', '.join(bad)}{hint}; {avail}")


class Registry(Generic[T]):
    def __init__(self, kind: str):
        self.kind = kind
        self.label = LABELS.get(kind, kind)
        self.group = f"cpc.{kind}"
        self._items: dict[str, type[T]] = {}
        self._loaded = False

    def _add(self, name: str, cls: type[T], origin: str) -> None:
        if self._items.get(name, cls) is not cls:
            raise ValueError(f"{origin}: {self.label} name {name} is already registered")
        cls.name = name  # type: ignore[attr-defined]
        self._items[name] = cls

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def deco(cls: type[T]) -> type[T]:
            self._add(name, cls, cls.__qualname__)
            return cls

        return deco

    def _load_entry_points(self) -> None:
        if not self._loaded:
            self._loaded = True
            for ep in entry_points(group=self.group):
                self._add(ep.name, ep.load(), f"entry point {ep.value}")

    def get(self, name: str) -> type[T]:
        self._load_entry_points()
        if name not in self._items:
            raise ConfigError(f"unknown {self.label} {name}{did_you_mean(name, self._items)}; "
                              f"available: {', '.join(sorted(self._items))}")
        return self._items[name]

    def names(self) -> list[str]:
        self._load_entry_points()
        return sorted(self._items)

    def all(self) -> dict[str, type[T]]:
        """All registered implementations (name -> class), entry points included."""
        self._load_entry_points()
        return dict(self._items)
