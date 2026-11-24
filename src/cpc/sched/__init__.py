"""调度策略。

全局（跨数据中心）：路由策略 RoutingPolicy（逐请求选择站点）、容量策略 CapacityPolicy（周期性给出各站点的指令与路由权重）。
本地（数据中心内部）：派发策略 DispatchPolicy（逐请求选择服务器）、电源管理策略 PowerPolicy（服务器开关）。
内置实现以装饰器注册；第三方实现以 entry points 提供（见 cpc.registry）。
"""

from .capacity import CAPACITY, CapacityPlan, CapacityPolicy, ControlView, make_capacity, register_capacity
from .local import DISPATCH, POWER, DispatchPolicy, PowerPolicy, ServerView, make_dispatch, make_power
from .routing import ROUTING, ClusterView, RequestInfo, RouteDecision, RoutingPolicy, SiteView, make_routing, register_routing

__all__ = [
    "CAPACITY", "DISPATCH", "POWER", "ROUTING", "CapacityPlan", "CapacityPolicy", "ClusterView", "ControlView", "DispatchPolicy",
    "PowerPolicy", "RequestInfo", "RouteDecision", "RoutingPolicy", "ServerView", "SiteView", "make_capacity", "make_dispatch",
    "make_power", "make_routing", "register_capacity", "register_routing",
]
