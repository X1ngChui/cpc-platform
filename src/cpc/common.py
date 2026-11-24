"""服务公共工具：日志、Prometheus 指标输出、JSONL 记录、HTTP 客户端。"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(level=(level or os.environ.get("CPC_LOG_LEVEL", "INFO")).upper(), format=LOG_FORMAT)
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
