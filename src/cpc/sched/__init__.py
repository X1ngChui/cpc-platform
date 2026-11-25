"""Scheduling policies.

Global (across data centres): RoutingPolicy chooses a site per request; CapacityPolicy periodically
sets the instructions of every site and optional routing weights.
Local (inside a data centre): DispatchPolicy chooses a server per request; PowerPolicy switches servers.
Built-in implementations register with decorators; third-party ones are entry points (see cpc.registry).
"""

from .capacity import CAPACITY, CapacityPlan, CapacityPolicy, ControlView, make_capacity, register_capacity
from .local import DISPATCH, POWER, DispatchPolicy, PowerPolicy, ServerView, make_dispatch, make_power
from .routing import ROUTING, ClusterView, RequestInfo, RouteDecision, RoutingPolicy, SiteView, make_routing, register_routing

__all__ = [
    "CAPACITY", "DISPATCH", "POWER", "ROUTING", "CapacityPlan", "CapacityPolicy", "ClusterView", "ControlView", "DispatchPolicy",
    "PowerPolicy", "RequestInfo", "RouteDecision", "RoutingPolicy", "ServerView", "SiteView", "make_capacity", "make_dispatch",
    "make_power", "make_routing", "register_capacity", "register_routing",
]
