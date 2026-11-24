"""调度策略插件。外部策略模块可通过环境变量 CPC_PLUGINS（逗号分隔的模块名）在启动时导入注册。"""

import importlib
import os

from .capacity import CAPACITY_POLICIES, CapacityPlan, CapacityPolicy, ControlView, make_capacity, register_capacity
from .routing import (
    ROUTING_POLICIES, ClusterView, RequestInfo, RouteDecision, RoutingPolicy, SiteView, make_routing, register_routing,
)


def load_plugins(modules: str | None = None) -> list[str]:
    mods = [m.strip() for m in (modules or os.environ.get("CPC_PLUGINS", "")).split(",") if m.strip()]
    for m in mods:
        importlib.import_module(m)
    return mods


__all__ = [
    "CAPACITY_POLICIES", "ROUTING_POLICIES", "CapacityPlan", "CapacityPolicy", "ClusterView", "ControlView",
    "RequestInfo", "RouteDecision", "RoutingPolicy", "SiteView", "load_plugins", "make_capacity", "make_routing",
    "register_capacity", "register_routing",
]
