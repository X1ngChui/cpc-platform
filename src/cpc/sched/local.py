"""站内调度策略（数据中心内部）：派发与电源管理。

- 派发策略（DispatchPolicy）：对到达站点的请求选择服务器。结构参照 Kubernetes 调度框架：
  先过滤（filter）可接收该请求的服务器，再打分（score），取分数最小者；没有可选服务器时请求排队。
  priority() 给出排队顺序（数值大者先出队，相同时先到先出）。
- 电源管理策略（PowerPolicy）：把跨站点策略下发的目标在线服务器数落实到具体服务器，给出每台服务器的目标状态（on / off）；
  站点运行时据此执行状态转移（off→booting→on，on→draining→off）。

策略只读取 ServerView（服务器状态）与 RequestInfo（含扩展属性 attrs），不修改它们；仿真与实验共用同一实现。
"""

from __future__ import annotations

import hashlib
from typing import Iterable, Protocol

from ..registry import Registry
from .routing import RequestInfo


class ServerView(Protocol):
    """站内策略可见的服务器状态。"""

    id: str
    pool: str
    state: str  # off / booting / on / draining
    busy: int
    slots: int
    cap_ratio: float

    @property
    def accepting(self) -> bool: ...

    @property
    def peak_w(self) -> float: ...  # 服务器满载 IT 功率（模型参数）

    @property
    def idle_w(self) -> float: ...  # 服务器在线空载 IT 功率（模型参数）


class DispatchPolicy:
    name = "base"

    def __init__(self, **params):
        if params:
            raise TypeError(f"派发策略 {self.name} 不接受参数 {sorted(params)}")

    def filter(self, req: RequestInfo, s: ServerView) -> bool:
        return s.accepting

    def score(self, req: RequestInfo, s: ServerView) -> float:
        raise NotImplementedError

    def select(self, req: RequestInfo, servers: Iterable[ServerView]) -> ServerView | None:
        best, best_score = None, None
        for s in servers:
            if not self.filter(req, s):
                continue
            sc = self.score(req, s)
            if best_score is None or sc < best_score:
                best, best_score = s, sc
        return best

    def priority(self, req: RequestInfo) -> float:
        return 0.0


class PowerPolicy:
    name = "base"

    def __init__(self, **params):
        if params:
            raise TypeError(f"电源管理策略 {self.name} 不接受参数 {sorted(params)}")

    def plan(self, want: int, servers: list[ServerView]) -> dict[str, str]:
        """返回需要改变状态的服务器及其目标状态（"on" / "off"）；want 为目标在线（含启动中）服务器数。"""
        raise NotImplementedError


DISPATCH: Registry[DispatchPolicy] = Registry("dispatch")
POWER: Registry[PowerPolicy] = Registry("power")


def make_dispatch(name: str, params: dict | None = None) -> DispatchPolicy:
    return DISPATCH.get(name)(**(params or {}))


def make_power(name: str, params: dict | None = None) -> PowerPolicy:
    return POWER.get(name)(**(params or {}))


@DISPATCH.register("least_loaded")
class LeastLoaded(DispatchPolicy):
    """占用率（在服务请求数 / 并发槽数）最低的服务器（负载均衡）。"""

    def score(self, req, s):
        return s.busy / s.slots


@DISPATCH.register("packing")
class Packing(DispatchPolicy):
    """占用率最高的可接收服务器（集中负载，便于空闲服务器关机）。"""

    def score(self, req, s):
        return -s.busy / s.slots


@DISPATCH.register("efficient_first")
class EfficientFirst(DispatchPolicy):
    """单位并发槽满载功率最低的服务器优先，同类服务器间按占用率均衡（异构服务器池）。"""

    def score(self, req, s):
        return s.peak_w / s.slots + 1e-6 * s.busy / s.slots


@DISPATCH.register("affinity")
class Affinity(DispatchPolicy):
    """按请求属性 key（如 session_id）的哈希选择首选服务器，首选服务器不可接收时选占用率最低者。

    用于利用请求的扩展字段：同一会话的请求落在同一服务器上，便于复用 KV 缓存。属性缺失的请求按占用率选择。
    """

    def __init__(self, key: str = "session_id", **params):
        super().__init__(**params)
        self.key = key

    def select(self, req, servers):
        cands = [s for s in servers if self.filter(req, s)]
        if not cands:
            return None
        v = req.attrs.get(self.key)
        if v is not None:
            ordered = sorted(cands, key=lambda s: s.id)
            h = int(hashlib.blake2b(str(v).encode(), digest_size=8).hexdigest(), 16)
            return ordered[h % len(ordered)]
        return min(cands, key=lambda s: s.busy / s.slots)


@DISPATCH.register("priority")
class PriorityQueue(LeastLoaded):
    """排队时按请求属性 key（缺省 priority，数值大者优先）出队；服务器选择同 least_loaded。"""

    def __init__(self, key: str = "priority", **params):
        super().__init__(**params)
        self.key = key

    def priority(self, req):
        v = req.attrs.get(self.key)
        return float(v) if v is not None else 0.0


@POWER.register("ordered")
class Ordered(PowerPolicy):
    """扩容时先恢复排空中的服务器，再按配置顺序启动关机服务器；缩容时先取消启动中的服务器，再停止在服务请求最少的服务器。"""

    def plan(self, want, servers):
        live = [s for s in servers if s.state in ("on", "booting")]
        out: dict[str, str] = {}
        if want > len(live):
            need = want - len(live)
            for s in [s for s in servers if s.state == "draining"][:need]:
                out[s.id] = "on"
                need -= 1
            for s in [s for s in servers if s.state == "off"][:need]:
                out[s.id] = "on"
        elif want < len(live):
            drop = len(live) - want
            booting = [s for s in live if s.state == "booting"][:drop]
            for s in booting:
                out[s.id] = "off"
            for s in sorted([s for s in live if s.state == "on"], key=lambda s: s.busy)[:drop - len(booting)]:
                out[s.id] = "off"
        return out


@POWER.register("efficient")
class Efficient(PowerPolicy):
    """优先保留单位并发槽满载功率低的服务器：扩容时先开启高效服务器，缩容时先停止低效服务器（异构服务器池）。"""

    def plan(self, want, servers):
        eff = lambda s: s.peak_w / s.slots  # noqa: E731
        live = [s for s in servers if s.state in ("on", "booting")]
        out: dict[str, str] = {}
        if want > len(live):
            need = want - len(live)
            for s in sorted([s for s in servers if s.state in ("draining", "off")], key=lambda s: (s.state != "draining", eff(s)))[:need]:
                out[s.id] = "on"
        elif want < len(live):
            for s in sorted(live, key=lambda s: (s.state != "booting", -eff(s), s.busy))[:len(live) - want]:
                out[s.id] = "off"
        return out
