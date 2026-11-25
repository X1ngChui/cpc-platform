"""Runtime health: every external dependency (power sensor, upstream service) keeps its own state,
exported as Prometheus metrics.

Principles (design document, "Failure handling and fault isolation"):
- every access to a dependency updates its own state (ok / fail); a fault affects only the data
  that dependency provides;
- the state is exposed explicitly (components in /state, metrics); missing data is never replaced
  by old, default or modelled values;
- transitions (healthy -> failed, failed -> recovered) are logged; repeated errors while a fault
  persists are counted but not logged again.

Metrics (kind sets the name prefix):
    kind="sensor"    cpc_sensor_up{<labels>, sensor}        cpc_sensor_errors_total{<labels>, sensor}
    kind="upstream"  cpc_upstream_up{<labels>, upstream}    cpc_upstream_errors_total{<labels>, upstream}
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from prometheus_client import CollectorRegistry, Counter, Gauge

log = logging.getLogger("cpc.health")


@dataclass(slots=True)
class Health:
    """Health of one dependency; up is None until it has been checked."""

    name: str
    up: bool | None = None
    errors_total: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    last_ok: float | None = None  # Unix time
    last_change: float | None = None
    detail: dict = field(default_factory=dict)

    def snapshot(self, now: float) -> dict:
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
        return f"{type(err).__name__}: {msg}" if (msg := str(err)) else type(err).__name__
    return err


class HealthBoard:
    """Health of a group of dependencies of one kind, with the matching Prometheus metrics."""

    def __init__(self, kind: Literal["sensor", "upstream"], registry: CollectorRegistry | None,
                 labels: dict[str, str] | None = None, key: str | None = None):
        self.kind = kind
        self.key = key or kind
        self.labels = dict(labels or {})
        self.items: dict[str, Health] = {}
        names = [*self.labels, self.key]
        self._up = self._err = None
        if registry is not None:
            self._up = Gauge(f"cpc_{kind}_up", f"whether the {kind} is healthy (1) or failed (0)", names, registry=registry)
            self._err = Counter(f"cpc_{kind}_errors", f"failed {kind} accesses", names, registry=registry)

    def _metrics(self, name: str):
        values = [*self.labels.values(), name]
        return self._up.labels(*values), self._err.labels(*values)

    def register(self, name: str) -> Health:
        """Register a dependency at startup so that its metrics exist from the beginning."""
        if name not in self.items:
            self.items[name] = Health(name)
            if self._up is not None:
                up, err = self._metrics(name)
                up.set(0)
                err.inc(0)
        return self.items[name]

    def get(self, name: str) -> Health:
        return self.items.get(name) or self.register(name)

    def ok(self, name: str, now: float | None = None) -> None:
        h, now = self.get(name), time.time() if now is None else now
        if h.up is not True:
            if h.up is False:
                log.info("%s %s recovered", self.kind, name, extra={"event": f"{self.kind}_up", self.key: name, **self.labels})
            h.last_change = now
        h.up, h.consecutive_errors, h.last_ok = True, 0, now
        if self._up is not None:
            self._metrics(name)[0].set(1)

    def fail(self, name: str, err: BaseException | str, now: float | None = None) -> None:
        h, now, msg = self.get(name), time.time() if now is None else now, describe(err)
        if h.up is not False:
            log.warning("%s %s failed: %s", self.kind, name, msg,
                        extra={"event": f"{self.kind}_down", self.key: name, "error": msg, **self.labels})
            h.last_change = now
        else:
            log.debug("%s %s still failing: %s", self.kind, name, msg)
        h.up, h.last_error = False, msg
        h.errors_total += 1
        h.consecutive_errors += 1
        if self._up is not None:
            up, errors = self._metrics(name)
            up.set(0)
            errors.inc()

    def is_up(self, name: str) -> bool:
        return bool((h := self.items.get(name)) and h.up)

    def stale(self, name: str, max_age_s: float, now: float | None = None) -> bool:
        """The last success is older than max_age_s, or there has been none."""
        h, now = self.items.get(name), time.time() if now is None else now
        return h is None or h.last_ok is None or now - h.last_ok > max_age_s

    def snapshot(self) -> dict[str, dict]:
        now = time.time()
        return {k: v.snapshot(now) for k, v in self.items.items()}
