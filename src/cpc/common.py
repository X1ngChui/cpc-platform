"""服务公共工具：日志、Prometheus 指标输出、JSONL 记录、HTTP 客户端。"""

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


class JsonFormatter(logging.Formatter):
    """结构化日志：每条记录一行 JSON，含时间、级别、来源与调用方通过 extra 传入的字段（event、site、error 等）。"""

    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        d.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, fmt: str | None = None) -> None:
    """日志级别取 CPC_LOG_LEVEL（默认 INFO），格式取 CPC_LOG_FORMAT：text（默认）或 json。"""
    fmt = (fmt or os.environ.get("CPC_LOG_FORMAT", "text")).lower()
    if fmt not in ("text", "json"):
        raise ValueError(f"CPC_LOG_FORMAT 只能为 text 或 json，当前为 {fmt}")
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=(level or os.environ.get("CPC_LOG_LEVEL", "INFO")).upper(), handlers=[handler], force=True)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def admin_token() -> str | None:
    """管理接口的共享令牌（环境变量 CPC_ADMIN_TOKEN）。未设置时管理接口不鉴权，仅适合本机/内网开发。"""
    return os.environ.get("CPC_ADMIN_TOKEN") or None


def admin_headers() -> dict[str, str]:
    """调用其他服务管理接口时携带的请求头。"""
    tok = admin_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def require_admin(request: Request) -> None:
    """FastAPI 依赖：校验 Authorization: Bearer <CPC_ADMIN_TOKEN>。"""
    tok = admin_token()
    if tok is None:
        return
    got = request.headers.get("authorization", "")
    if not hmac.compare_digest(got.encode(), f"Bearer {tok}".encode()):
        raise HTTPException(status_code=401, detail="admin token required")


class StartupError(Exception):
    """服务启动检查失败（依赖服务在期限内不可达等）：进程以非零状态退出。"""


async def sync_clock(client, grid_url: str, timeout_s: float):
    """从电网信号服务同步信号时钟；在 timeout_s 内不可达则抛出 StartupError。"""
    import httpx

    from .health import describe
    from .models import SimClock

    deadline = time.monotonic() + timeout_s
    last: BaseException | None = None
    while True:
        try:
            r = await client.get(grid_url.rstrip("/") + "/v1/clock", timeout=2.0)
            r.raise_for_status()
            return SimClock.from_dict(r.json())
        except (httpx.HTTPError, ValueError, KeyError) as e:
            last = e
        if time.monotonic() >= deadline:
            raise StartupError(f"电网信号服务 {grid_url} 在 {timeout_s:.0f} s 内不可达，无法同步信号时钟：{describe(last)}")
        await asyncio.sleep(0.5)


def warn_if_open(service: str, host: str) -> None:
    if admin_token() is None and host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("cpc").warning(
            "%s 监听 %s 且未设置 CPC_ADMIN_TOKEN：/admin/* 管理接口无鉴权。公网/云服务器部署请务必设置该环境变量。", service, host)


def metrics_response(registry: CollectorRegistry) -> Response:
    """每个服务使用独立的 registry，便于在同一进程中运行多个服务（本机开发模式）。"""
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


class JsonlWriter:
    """异步追加写 JSONL（请求日志、决策日志），避免阻塞事件循环。"""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._q: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._q = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    def write(self, rec: dict[str, Any]) -> None:
        if self._q is not None:
            self._q.put_nowait(rec)

    async def _run(self) -> None:
        assert self._q is not None and self.path is not None
        done = False
        while not done:
            batch = [await self._q.get()]
            while not self._q.empty() and len(batch) < 1000:
                batch.append(self._q.get_nowait())
            if None in batch:  # 结束标记
                done = True
                batch = [r for r in batch if r is not None]
            if batch:
                lines = "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in batch)
                await asyncio.to_thread(self._append, lines)

    def _append(self, text: str) -> None:
        assert self.path is not None
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    async def stop(self) -> None:
        """写完队列中剩余记录后退出。"""
        if self._task is None or self._q is None:
            return
        self._q.put_nowait(None)  # type: ignore[arg-type]
        await self._task


ATTR_PREFIX = "x-cpc-attr-"


def attr_headers(attrs: dict) -> dict[str, str]:
    """把请求扩展属性编码为请求头：x-cpc-attr-<字段名>: <JSON 值>（字段名按 HTTP 惯例不区分大小写）。"""
    return {ATTR_PREFIX + k.lower(): json.dumps(v, ensure_ascii=True, separators=(",", ":")) for k, v in attrs.items() if v is not None}


def request_attrs(headers) -> dict:
    """从请求头解析扩展属性；值不是合法 JSON 时按字符串处理。"""
    out = {}
    for k, v in headers.items():
        k = k.lower()
        if k.startswith(ATTR_PREFIX):
            try:
                out[k[len(ATTR_PREFIX):]] = json.loads(v)
            except ValueError:
                out[k[len(ATTR_PREFIX):]] = v
    return out
