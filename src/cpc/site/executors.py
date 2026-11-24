"""请求执行器与节点钩子。

- EmulatedExecutor：按性能模型"睡眠"相应时间并返回 OpenAI 格式的假文本（含流式输出）。
- OpenAIExecutor：把请求转发给真实 OpenAI 兼容推理实例（vLLM、SGLang、llama.cpp server 等）。
- RealNodeHooks：真实节点的开关机（Docker 启停容器，可选）、功率封顶（NVML，可选）、就绪检查（HTTP）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx

from ..config import NodeProfile
from ..models import prefill_s, tpot_s
from .core import Node

log = logging.getLogger("cpc.site.exec")

WORDS = "the of and to in is that for it as with was on be by this are from at or an".split()


def estimate_tokens(body: dict) -> tuple[int, int]:
    """估计输入/输出 token 数。输入：按空白分词数与字符数/4 取大；输出：max_tokens（压测通常配合 ignore_eos）。"""
    if "messages" in body:
        text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            for m in body.get("messages", [])
        )
    else:
        p = body.get("prompt", "")
        text = p if isinstance(p, str) else " ".join(map(str, p))
    n_in = max(1, len(text.split()), len(text) // 4)
    n_out = int(body.get("max_tokens") or body.get("max_completion_tokens") or 128)
    return n_in, max(1, n_out)


def _fake_text(n: int, offset: int = 0) -> str:
    return " ".join(WORDS[(offset + i) % len(WORDS)] for i in range(n))


class ExecResult:
    def __init__(self, status: int, body: dict | None, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.status = status
        self.body = body
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class EmulatedExecutor:
    def __init__(self, profile: NodeProfile, model_name: str = "cpc-emulated", chunk_tokens: int = 16):
        self.p = profile
        self.model_name = model_name
        self.chunk_tokens = chunk_tokens

    async def models(self) -> dict:
        return {"object": "list", "data": [{"id": self.model_name, "object": "model", "owned_by": "cpc"}]}

    def _timing(self, node: Node, body: dict) -> tuple[int, int, float, float]:
        n_in, n_out = estimate_tokens(body)
        occ = node.busy / node.slots
        return n_in, n_out, prefill_s(self.p, n_in, node.cap_ratio), tpot_s(self.p, occ, node.cap_ratio)

    async def complete(self, node: Node, path: str, body: dict) -> ExecResult:
        n_in, n_out, pre, tp = self._timing(node, body)
        await asyncio.sleep(pre + n_out * tp)
        rid = f"cmpl-{uuid.uuid4().hex[:16]}"
        usage = {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}
        text = _fake_text(n_out)
        if path.endswith("chat/completions"):
            choice = {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "length"}
            obj = "chat.completion"
        else:
            choice = {"index": 0, "text": text, "finish_reason": "length"}
            obj = "text_completion"
        body_out = {"id": rid, "object": obj, "created": int(time.time()), "model": self.model_name, "choices": [choice], "usage": usage}
        return ExecResult(200, body_out, n_in, n_out)

    async def stream(self, node: Node, path: str, body: dict, out: ExecResult) -> AsyncIterator[bytes]:
        n_in, n_out, pre, tp = self._timing(node, body)
        chat = path.endswith("chat/completions")
        rid = f"cmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())
        obj = "chat.completion.chunk" if chat else "text_completion"

        def chunk(text: str, finish: str | None = None, first: bool = False) -> bytes:
            if chat:
                delta = {"role": "assistant", "content": text} if first else {"content": text}
                ch = {"index": 0, "delta": delta, "finish_reason": finish}
            else:
                ch = {"index": 0, "text": text, "finish_reason": finish}
            d = {"id": rid, "object": obj, "created": created, "model": self.model_name, "choices": [ch]}
            return f"data: {json.dumps(d)}\n\n".encode()

        await asyncio.sleep(pre + tp)  # 首 token
        yield chunk(_fake_text(1), first=True)
        sent = 1
        while sent < n_out:
            k = min(self.chunk_tokens, n_out - sent)
            await asyncio.sleep(k * tp)
            yield chunk(" " + _fake_text(k, sent), "length" if sent + k >= n_out else None)
            sent += k
        out.prompt_tokens, out.completion_tokens = n_in, n_out
        if (body.get("stream_options") or {}).get("include_usage"):
            u = {"id": rid, "object": obj, "created": created, "model": self.model_name, "choices": [],
                 "usage": {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}}
            yield f"data: {json.dumps(u)}\n\n".encode()
        yield b"data: [DONE]\n\n"


class OpenAIExecutor:
    """转发到真实推理实例。node.upstream.url 例如 http://127.0.0.1:8000。"""

    def __init__(self, timeout_s: float = 600.0):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0))

    async def models(self, nodes: list[Node]) -> dict:
        for n in nodes:
            if n.state == "on" and n.upstream:
                try:
                    r = await self.client.get(n.upstream.url.rstrip("/") + "/v1/models")
                    return r.json()
                except httpx.HTTPError:
                    continue
        return {"object": "list", "data": []}

    async def complete(self, node: Node, path: str, body: dict) -> ExecResult:
        assert node.upstream is not None
        try:
            r = await self.client.post(node.upstream.url.rstrip("/") + path, json=body)
        except httpx.HTTPError as e:
            return ExecResult(502, {"error": {"message": f"upstream {node.id} error: {e}"}})
        try:
            data = r.json()
        except ValueError:
            data = {"error": {"message": r.text[:500]}}
        usage = data.get("usage") or {}
        return ExecResult(r.status_code, data, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))

    async def stream(self, node: Node, path: str, body: dict, out: ExecResult) -> AsyncIterator[bytes]:
        assert node.upstream is not None
        url = node.upstream.url.rstrip("/") + path
        try:
            async with self.client.stream("POST", url, json=body) as r:
                out.status = r.status_code
                async for chunk in r.aiter_raw():
                    _scan_usage(chunk, out)
                    yield chunk
        except httpx.HTTPError as e:
            out.status = 502
            yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n".encode()


def _scan_usage(chunk: bytes, out: ExecResult) -> None:
    """从 SSE 片段中提取 usage（需请求中设置 stream_options.include_usage）。"""
    if b'"usage"' not in chunk:
        return
    for line in chunk.split(b"\n"):
        if line.startswith(b"data: {") and b'"usage"' in line:
            try:
                u = json.loads(line[6:]).get("usage") or {}
                out.prompt_tokens = u.get("prompt_tokens", out.prompt_tokens)
                out.completion_tokens = u.get("completion_tokens", out.completion_tokens)
            except ValueError:
                pass


class RealNodeHooks:
    """真实节点钩子。

    - 开关机：若 upstream.container 已配置且安装了 docker SDK，则启停该容器；否则为"逻辑开关"
      （只停止向其派发请求，GPU 仍处于空载供电状态——测得的功率会如实反映这一点）。
    - 功率封顶：若提供 NvmlPower 且进程有权限，调用 nvmlDeviceSetPowerManagementLimit。
    - 就绪检查：GET {url}/health（vLLM 提供），失败再试 /v1/models。
    """

    def __init__(self, nvml=None, enable_docker: bool = True, enable_power_cap: bool = False):
        self.nvml = nvml
        self.enable_power_cap = enable_power_cap
        self.client = httpx.AsyncClient(timeout=2.0)
        self.docker = None
        if enable_docker:
            try:
                import docker  # type: ignore

                self.docker = docker.from_env()
            except Exception as e:  # noqa: BLE001
                log.info("未启用 Docker 控制（%s），节点开关为逻辑开关", e)

    async def power_on(self, node: Node) -> None:
        if self.docker and node.upstream and node.upstream.container:
            await asyncio.to_thread(lambda: self.docker.containers.get(node.upstream.container).start())
            log.info("已启动容器 %s", node.upstream.container)

    async def power_off(self, node: Node) -> None:
        if self.docker and node.upstream and node.upstream.container:
            await asyncio.to_thread(lambda: self.docker.containers.get(node.upstream.container).stop())
            log.info("已停止容器 %s", node.upstream.container)

    async def set_cap(self, node: Node, ratio: float) -> None:
        if not (self.enable_power_cap and self.nvml and node.upstream):
            log.info("节点 %s 功率封顶比例 %.2f（未启用硬件封顶，仅记录）", node.id, ratio)
            return
        for i in node.upstream.gpu_indices:
            try:
                await asyncio.to_thread(self.nvml.set_power_limit, i, ratio)
            except Exception as e:  # noqa: BLE001
                log.warning("设置 GPU %s 功率上限失败（通常需要 root 权限）：%s", i, e)

    async def is_ready(self, node: Node) -> bool:
        if not node.upstream:
            return True
        base = node.upstream.url.rstrip("/")
        for p in ("/health", "/v1/models"):
            try:
                r = await self.client.get(base + p)
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
        return False
