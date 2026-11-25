"""Shared service utilities: logging, admin authentication, Prometheus output, JSONL records, SSE checks."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class JsonFormatter(logging.Formatter):
    """Structured logs: one JSON object per record with time, level, logger and the fields passed in extra."""

    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **{k: v for k, v in vars(record).items() if k not in _STD_ATTRS},
        }
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Level from CPC_LOG_LEVEL (default INFO); format from CPC_LOG_FORMAT: text (default) or json."""
    fmt = (fmt or os.environ.get("CPC_LOG_FORMAT", "text")).lower()
    if fmt not in ("text", "json"):
        raise ValueError(f"CPC_LOG_FORMAT must be text or json, got {fmt}")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=(level or os.environ.get("CPC_LOG_LEVEL", "INFO")).upper(), handlers=[handler], force=True)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- admin interface


def admin_token() -> str | None:
    """Shared token of the admin interfaces (CPC_ADMIN_TOKEN). Without it they are open (local development only)."""
    return os.environ.get("CPC_ADMIN_TOKEN") or None


def admin_headers() -> dict[str, str]:
    """Headers for calling the admin interface of another service."""
    return {"Authorization": f"Bearer {tok}"} if (tok := admin_token()) else {}


def require_admin(request: Request) -> None:
    """FastAPI dependency checking Authorization: Bearer <CPC_ADMIN_TOKEN>."""
    if (tok := admin_token()) is None:
        return
    got = request.headers.get("authorization", "")
    if not hmac.compare_digest(got.encode(), f"Bearer {tok}".encode()):
        raise HTTPException(status_code=401, detail="admin token required")


def warn_if_open(service: str, host: str) -> None:
    if admin_token() is None and host not in LOCAL_HOSTS:
        logging.getLogger("cpc").warning(
            "%s listens on %s without CPC_ADMIN_TOKEN: the /admin/* interface is not authenticated; "
            "set the variable for any deployment reachable from other hosts", service, host)


# ---------------------------------------------------------------- startup


class StartupError(Exception):
    """A startup check failed (e.g. a dependency stayed unreachable); the process exits with an error."""


async def sync_clock(client, grid_url: str, timeout_s: float):
    """Fetch the signal clock from the grid-signal service; StartupError if unreachable for timeout_s."""
    import httpx

    from .health import describe
    from .models import SimClock

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            r = await client.get(grid_url.rstrip("/") + "/v1/clock", timeout=2.0)
            r.raise_for_status()
            return SimClock.from_dict(r.json())
        except (httpx.HTTPError, ValueError, KeyError) as e:
            last = e
        if time.monotonic() >= deadline:
            raise StartupError(f"grid-signal service {grid_url} unreachable for {timeout_s:.0f} s; "
                               f"cannot synchronise the signal clock: {describe(last)}")
        await asyncio.sleep(0.5)


def metrics_response(registry: CollectorRegistry) -> Response:
    """Each service has its own registry so that several services can share one process (cpc up)."""
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- JSONL records


class JsonlWriter:
    """Appends records to a JSONL file from a background task, without blocking the event loop."""

    _END = object()

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._q: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._q = asyncio.Queue()
            self._task = asyncio.create_task(self._run())

    def write(self, rec: dict[str, Any]) -> None:
        if self._q is not None:
            self._q.put_nowait(rec)

    async def _run(self) -> None:
        assert self._q is not None
        done = False
        while not done:
            batch = [await self._q.get()]
            while not self._q.empty() and len(batch) < 1000:
                batch.append(self._q.get_nowait())
            done = self._END in batch
            if lines := "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in batch if r is not self._END):
                await asyncio.to_thread(self._append, lines)

    def _append(self, text: str) -> None:
        assert self.path is not None
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text)

    async def stop(self) -> None:
        """Write the queued records, then stop."""
        if self._task is not None and self._q is not None:
            self._q.put_nowait(self._END)
            await self._task


# ---------------------------------------------------------------- request attributes

ATTR_PREFIX = "x-cpc-attr-"


def attr_headers(attrs: dict) -> dict[str, str]:
    """Encode request attributes as headers x-cpc-attr-<field>: <JSON value> (field names are case-insensitive)."""
    return {ATTR_PREFIX + k.lower(): json.dumps(v, ensure_ascii=True, separators=(",", ":"))
            for k, v in attrs.items() if v is not None}


def request_attrs(headers) -> dict:
    """Decode request attributes from headers; values that are not valid JSON are kept as strings."""
    out = {}
    for k, v in headers.items():
        if (k := k.lower()).startswith(ATTR_PREFIX):
            try:
                out[k.removeprefix(ATTR_PREFIX)] = json.loads(v)
            except ValueError:
                out[k.removeprefix(ATTR_PREFIX)] = v
    return out


# ---------------------------------------------------------------- streaming responses


class SSECheck:
    """Checks an OpenAI-compatible streaming response (SSE): ends with data: [DONE], carries no error event.

    feed() the received byte chunks in order; problem is None for a complete stream without errors,
    otherwise the reason: "stream_error: <message>" or "stream_incomplete".
    """

    def __init__(self) -> None:
        self._buf = b""
        self.done = False
        self.error: str | None = None

    def feed(self, chunk: bytes) -> None:
        *lines, self._buf = (self._buf + chunk).split(b"\n")
        for line in map(bytes.strip, lines):
            if line == b"data: [DONE]":
                self.done = True
            elif self.error is None and line.startswith(b"data: {") and b'"error"' in line:
                try:
                    err = json.loads(line[6:]).get("error")
                except ValueError:
                    continue
                if err:
                    self.error = str(err.get("message", err) if isinstance(err, dict) else err)[:200]

    @property
    def problem(self) -> str | None:
        if self.error is not None:
            return f"stream_error: {self.error}"
        return None if self.done else "stream_incomplete"
