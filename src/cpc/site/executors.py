"""Request executors and executor hooks of real servers.

- EmulatedExecutor: emulated servers. Waits as long as the performance model of the server says and
  returns placeholder text in OpenAI format (streaming included).
- OpenAIExecutor: real servers. Forwards requests to an OpenAI-compatible inference instance (vLLM,
  SGLang, llama.cpp server, ...).
- SiteExecutor: chooses one of the two per server.
- RealNodeHooks: power on/off of real servers (Docker containers when configured), power capping
  (when enabled) and HTTP health checks.

Request tokens: the input is estimated with an approximate tokenisation (max(whitespace words,
characters / 4)), used by policy views and emulated timing; the output is max_tokens (or
max_completion_tokens). Sites with emulated servers require an output limit and return 400 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from ..config import ConfigError, UpstreamConfig
from ..datacenter import Server
from ..models import prefill_s, tpot_s

log = logging.getLogger("cpc.site.exec")

WORDS = "the of and to in is that for it as with was on be by this are from at or an".split()


class InvalidRequest(Exception):
    """The request does not meet the requirements of the backend (400)."""


def approx_prompt_tokens(body: dict) -> int:
    """Approximate input tokens: the larger of whitespace words and characters / 4 (no model tokenizer)."""
    if "messages" in body:
        text = " ".join(c if isinstance(c := m.get("content", ""), str) else json.dumps(c) for m in body["messages"])
    else:
        p = body.get("prompt", "")
        text = p if isinstance(p, str) else " ".join(map(str, p))
    return max(1, len(text.split()), len(text) // 4)


def requested_output_tokens(body: dict) -> int | None:
    """Output token limit of the request (max_tokens / max_completion_tokens); None if not given."""
    v = body.get("max_tokens") or body.get("max_completion_tokens")
    return None if v is None else max(1, int(v))


def _fake_text(n: int, offset: int = 0) -> str:
    return " ".join(WORDS[(offset + i) % len(WORDS)] for i in range(n))


def _sse(d: dict) -> bytes:
    return f"data: {json.dumps(d)}\n\n".encode()


def error_chunk(message: str) -> bytes:
    return _sse({"error": {"message": message}})


@dataclass(slots=True)
class ExecResult:
    status: int
    body: dict | None
    prompt_tokens: int = 0
    completion_tokens: int = 0


class EmulatedExecutor:
    def __init__(self, model_name: str = "cpc-emulated", chunk_tokens: int = 16):
        self.model_name = model_name
        self.chunk_tokens = chunk_tokens

    async def models(self) -> dict:
        return {"object": "list", "data": [{"id": self.model_name, "object": "model", "owned_by": "cpc"}]}

    @staticmethod
    def validate(body: dict) -> None:
        if requested_output_tokens(body) is None:
            raise InvalidRequest("emulated servers require max_tokens (or max_completion_tokens)")

    @staticmethod
    def _timing(node: Server, body: dict) -> tuple[int, int, float, float]:
        n_in, n_out = approx_prompt_tokens(body), requested_output_tokens(body)
        assert n_out is not None  # ensured by validate
        p = node.profile
        return n_in, n_out, prefill_s(p, n_in, node.cap_ratio), tpot_s(p, node.occupancy, node.cap_ratio)

    def _envelope(self, rid: str, obj: str, choices: list, **extra) -> dict:
        return {"id": rid, "object": obj, "created": int(time.time()), "model": self.model_name, "choices": choices, **extra}

    async def complete(self, node: Server, path: str, body: dict) -> ExecResult:
        n_in, n_out, pre, tp = self._timing(node, body)
        await asyncio.sleep(pre + n_out * tp)
        text = _fake_text(n_out)
        if path.endswith("chat/completions"):
            obj, choice = "chat.completion", {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "length"}
        else:
            obj, choice = "text_completion", {"index": 0, "text": text, "finish_reason": "length"}
        usage = {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}
        return ExecResult(200, self._envelope(f"cmpl-{uuid.uuid4().hex[:16]}", obj, [choice], usage=usage), n_in, n_out)

    async def stream(self, node: Server, path: str, body: dict, out: ExecResult) -> AsyncIterator[bytes]:
        n_in, n_out, pre, tp = self._timing(node, body)
        chat = path.endswith("chat/completions")
        rid = f"cmpl-{uuid.uuid4().hex[:16]}"
        obj = "chat.completion.chunk" if chat else "text_completion"

        def chunk(text: str, finish: str | None = None, first: bool = False) -> bytes:
            if chat:
                ch = {"index": 0, "delta": {"role": "assistant", "content": text} if first else {"content": text},
                      "finish_reason": finish}
            else:
                ch = {"index": 0, "text": text, "finish_reason": finish}
            return _sse(self._envelope(rid, obj, [ch]))

        await asyncio.sleep(pre + tp)  # first token
        yield chunk(_fake_text(1), first=True)
        sent = 1
        while sent < n_out:
            k = min(self.chunk_tokens, n_out - sent)
            await asyncio.sleep(k * tp)
            yield chunk(" " + _fake_text(k, sent), "length" if sent + k >= n_out else None)
            sent += k
        out.prompt_tokens, out.completion_tokens = n_in, n_out
        if (body.get("stream_options") or {}).get("include_usage"):
            yield _sse(self._envelope(rid, obj, [], usage={"prompt_tokens": n_in, "completion_tokens": n_out,
                                                           "total_tokens": n_in + n_out}))
        yield b"data: [DONE]\n\n"


class OpenAIExecutor:
    """Forwards to a real inference instance; server.upstream.url is e.g. http://127.0.0.1:8000."""

    def __init__(self, timeout_s: float = 600.0, idle_timeout_s: float = 120.0):
        self.idle_timeout_s = idle_timeout_s  # longest gap between two chunks of a streaming response
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0), trust_env=False)

    @staticmethod
    def validate(body: dict) -> None:
        return None

    async def models(self, nodes: list[Server]) -> tuple[int, dict]:
        """Forwarded to the first online real server; 503 without one, 502 when the upstream fails."""
        if (node := next((n for n in nodes if n.state == "on" and n.upstream), None)) is None:
            return 503, {"error": {"message": "no real server online", "type": "unavailable"}}
        try:
            r = await self.client.get(node.upstream.url.rstrip("/") + "/v1/models")
            return r.status_code, r.json()
        except (httpx.HTTPError, ValueError) as e:
            return 502, {"error": {"message": f"upstream {node.id} error: {e}", "type": "upstream_error"}}

    async def complete(self, node: Server, path: str, body: dict) -> ExecResult:
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

    async def stream(self, node: Server, path: str, body: dict, out: ExecResult) -> AsyncIterator[bytes]:
        assert node.upstream is not None
        try:
            async with self.client.stream("POST", node.upstream.url.rstrip("/") + path, json=body) as r:
                out.status = r.status_code
                chunks = aiter(r.aiter_raw())
                while True:
                    try:
                        chunk = await asyncio.wait_for(anext(chunks), self.idle_timeout_s)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        out.status = 502
                        yield error_chunk(f"upstream {node.id} stalled: no data in {self.idle_timeout_s:g} s")
                        return
                    _scan_usage(chunk, out)
                    yield chunk
        except httpx.HTTPError as e:
            out.status = 502
            yield error_chunk(str(e))


def _scan_usage(chunk: bytes, out: ExecResult) -> None:
    """Extract usage from an SSE chunk (needs stream_options.include_usage in the request)."""
    if b'"usage"' not in chunk:
        return
    for line in chunk.split(b"\n"):
        if line.startswith(b"data: {") and b'"usage"' in line:
            try:
                u = json.loads(line[6:]).get("usage") or {}
            except ValueError:
                continue
            out.prompt_tokens = u.get("prompt_tokens", out.prompt_tokens)
            out.completion_tokens = u.get("completion_tokens", out.completion_tokens)


class SiteExecutor:
    """Chooses the executor per server: emulated -> EmulatedExecutor, real -> OpenAIExecutor."""

    def __init__(self, servers: list[Server], idle_timeout_s: float = 120.0):
        self.servers = servers
        self.has_emulated = any(s.upstream is None for s in servers)
        self.emulated = EmulatedExecutor()
        self.real = OpenAIExecutor(idle_timeout_s=idle_timeout_s) if any(s.upstream is not None for s in servers) else None

    def validate(self, body: dict) -> None:
        if self.has_emulated:
            EmulatedExecutor.validate(body)

    def of(self, server: Server):
        return self.emulated if server.upstream is None else self.real

    async def models(self) -> tuple[int, dict]:
        if self.real is not None:
            return await self.real.models(self.servers)
        return 200, await self.emulated.models()


class RealNodeHooks:
    """Executor hooks of real servers.

    - power on/off: starts and stops the Docker container of the upstream when configured; otherwise a
      logical switch (no more requests; the hardware keeps drawing idle power, which the measurement shows);
    - power capping: when enabled (--enable-power-cap), sets the power limit through the sensor that
      manages the device; otherwise only the target ratio is recorded;
    - health check: GET {url}{ready_path} (default /health, served by vLLM) returns 200.

    Startup checks: configured containers must be reachable through the Docker Engine; with power
    capping every accelerator must be managed by a sensor that supports it (check_power_cap, after the
    sensors have started). Failing actions raise; the site agent records them in last_error.
    """

    def __init__(self, upstreams: list[UpstreamConfig], power=None, enable_power_cap: bool = False):
        self.power = power
        self.enable_power_cap = enable_power_cap
        self.client = httpx.AsyncClient(timeout=2.0, trust_env=False)
        self.docker = None
        if containers := [u.container for u in upstreams if u.container]:
            try:
                import docker  # type: ignore
            except ImportError as e:
                raise ConfigError(f"upstreams name containers {containers}, which needs the Docker SDK "
                                  '(pip install "cpc-platform[docker]")') from e
            try:
                self.docker = docker.from_env()
                for c in containers:
                    self.docker.containers.get(c)
            except docker.errors.DockerException as e:
                raise ConfigError(f"cannot reach containers {containers} through the Docker Engine: {e}") from e
        if enable_power_cap:
            from .power import SensorPower

            if not isinstance(power, SensorPower):
                raise ConfigError("power capping needs measured power from sensors")
            if bare := [u.id for u in upstreams if not u.device_names]:
                raise ConfigError(f"servers {bare} declare no accelerators, so they cannot be power-capped")

    def check_power_cap(self, upstreams: list[UpstreamConfig]) -> None:
        """With power capping, check that the sensor of every accelerator supports it (after the sensors started)."""
        if self.enable_power_cap and (bad := [d for u in upstreams for d in u.device_names
                                              if not self.power.sensor_for(d).supports_power_limit]):
            raise ConfigError(f"the sensors of accelerators {bad} do not support power capping")

    async def _container(self, node: Server, action: str) -> None:
        if self.docker and node.upstream and (name := node.upstream.container):
            await asyncio.to_thread(lambda: getattr(self.docker.containers.get(name), action)())
            log.info("container %s: %s", name, action)

    async def power_on(self, node: Server) -> None:
        await self._container(node, "start")

    async def power_off(self, node: Server) -> None:
        await self._container(node, "stop")

    async def set_cap(self, node: Server, ratio: float) -> None:
        if not self.enable_power_cap:
            log.info("server %s: power-cap ratio %.2f recorded (hardware capping is not enabled)", node.id, ratio)
            return
        for dev in node.upstream.device_names:
            await asyncio.to_thread(self.power.set_power_limit, dev, ratio)

    async def is_ready(self, node: Server) -> bool:
        if not node.upstream:
            return True
        try:
            r = await self.client.get(node.upstream.url.rstrip("/") + node.upstream.ready_path)
        except httpx.HTTPError:
            return False
        return r.status_code == 200
