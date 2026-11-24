"""运行期健康状态：每个外部依赖（功率传感器、上游服务）独立维护状态，并导出为 Prometheus 指标。

设计原则（见《平台设计说明书》"失败处理与故障隔离"一节）：
- 依赖的每一次访问结果都记入其自身的状态（ok / fail），故障只影响该依赖所提供的数据；
- 状态以显式字段（/state 中的 components）与指标暴露，不以旧值、默认值或模型值替代缺失的数据；
- 状态变化（正常 → 故障、故障 → 恢复）写结构化日志；故障持续期间的重复错误只计数，不重复告警。

指标（kind 决定指标名前缀）：
    kind="sensor"    cpc_sensor_up{<labels>, sensor}        cpc_sensor_errors_total{<labels>, sensor}
    kind="upstream"  cpc_upstream_up{<labels>, upstream}    cpc_upstream_errors_total{<labels>, upstream}
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Gauge

log = logging.getLogger("cpc.health")


@dataclass
class Health:
    """单个依赖的健康状态。up 为 None 表示尚未检查过。"""

    name: str
    up: bool | None = None
    errors_total: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    last_ok: float | None = None  # Unix 时间
    last_change: float | None = None
    detail: dict = field(default_factory=dict)

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "up": self.up,
            "errors_total": self.errors_total,
            "consecutive_errors": self.consecutive_errors,
            "last_error": self.last_error,
            "last_ok_age_s": None if self.last_ok is None else round(now - self.last_ok, 3),
            **self.detail,
        }


def describe(err: BaseException | str) -> str:
    if isinstance(err, BaseException):
        msg = str(err)
        return f"{type(err).__name__}: {msg}" if msg else type(err).__name__
    return err


class HealthBoard:
    """一组同类依赖的健康状态与对应的 Prometheus 指标。"""

    def __init__(self, kind: str, registry: CollectorRegistry | None, labels: dict[str, str] | None = None,
                 key: str | None = None):
        if kind not in ("sensor", "upstream"):
            raise ValueError(f"未知的健康状态类别 {kind}")
        self.kind = kind
        self.key = key or kind
        self.labels = dict(labels or {})
        self.items: dict[str, Health] = {}
        names = [*self.labels, self.key]
        self._up = self._err = None
        if registry is not None:
            self._up = Gauge(f"cpc_{kind}_up", f"{kind} 是否正常（1 正常，0 故障）", names, registry=registry)
            self._err = Counter(f"cpc_{kind}_errors", f"{kind} 访问失败次数", names, registry=registry)

    def _lv(self, name: str) -> list[str]:
        return [*self.labels.values(), name]

    def register(self, name: str) -> Health:
        """登记一个依赖（启动时调用），使其指标从一开始就存在。"""
        if name not in self.items:
            self.items[name] = Health(name)
            if self._up is not None:
                self._up.labels(*self._lv(name)).set(0)
                self._err.labels(*self._lv(name)).inc(0)
        return self.items[name]

    def get(self, name: str) -> Health:
        return self.items.get(name) or self.register(name)

    def ok(self, name: str, now: float | None = None) -> None:
        h = self.get(name)
        now = time.time() if now is None else now
        if h.up is not True:
            if h.up is False:
                log.info("%s %s 恢复", self.kind, name,
                         extra={"event": f"{self.kind}_up", self.key: name, **self.labels})
            h.last_change = now
        h.up = True
        h.consecutive_errors = 0
        h.last_ok = now
        if self._up is not None:
            self._up.labels(*self._lv(name)).set(1)

    def fail(self, name: str, err: BaseException | str, now: float | None = None) -> None:
        h = self.get(name)
        now = time.time() if now is None else now
        msg = describe(err)
        if h.up is not False:
            log.warning("%s %s 故障：%s", self.kind, name, msg,
                        extra={"event": f"{self.kind}_down", self.key: name, "error": msg, **self.labels})
            h.last_change = now
        else:
            log.debug("%s %s 仍处于故障：%s", self.kind, name, msg)
        h.up = False
        h.errors_total += 1
        h.consecutive_errors += 1
        h.last_error = msg
        if self._up is not None:
            self._up.labels(*self._lv(name)).set(0)
            self._err.labels(*self._lv(name)).inc()

    def is_up(self, name: str) -> bool:
        h = self.items.get(name)
        return bool(h and h.up)

    def stale(self, name: str, max_age_s: float, now: float | None = None) -> bool:
        """最近一次成功距今超过 max_age_s（或从未成功）。"""
        h = self.items.get(name)
        now = time.time() if now is None else now
        return h is None or h.last_ok is None or now - h.last_ok > max_age_s

    def snapshot(self) -> dict[str, dict]:
        now = time.time()
        return {k: v.snapshot(now) for k, v in self.items.items()}
