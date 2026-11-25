"""请求执行器与真实服务器的执行器钩子。

- EmulatedExecutor：仿真服务器。按该服务器的性能模型等待相应时间，返回 OpenAI 格式的占位文本（含流式输出）。
- OpenAIExecutor：真实服务器。把请求转发给 OpenAI 兼容推理实例（vLLM、SGLang、llama.cpp server 等）。
- SiteExecutor：按服务器类型选择上述执行器。
- RealNodeHooks：真实服务器的开关机（配置了容器名时通过 Docker 启停容器）、功率封顶（显式开启时）、就绪检查（HTTP）。

请求的 token 数：输入按近似分词（max(空白分词数, 字符数/4)）估计，用于策略视图与仿真服务器的计时；
输出取请求中的 max_tokens（或 max_completion_tokens）。站点有仿真服务器时要求请求给出输出上限，缺失时返回 400。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator

import httpx

from ..config import ConfigError, UpstreamConfig
from ..datacenter import Server
from ..models import prefill_s, tpot_s

log = logging.getLogger("cpc.site.exec")

WORDS = "the of and to in is that for it as with was on be by this are from at or an".split()


class InvalidRequest(Exception):
    """请求不满足后端要求（返回 400）。"""


def approx_prompt_tokens(body: dict) -> int:
    """输入 token 数的近似值：按空白分词数与字符数/4 取大（不依赖具体模型的分词器）。"""
    if "messages" in body:
        text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))
            for m in body.get("messages", [])
        )
    else:
        p = body.get("prompt", "")
        text = p if isinstance(p, str) else " ".join(map(str, p))
    return max(1, len(text.split()), len(text) // 4)


def requested_output_tokens(body: dict) -> int | None:
    """请求给出的输出 token 上限（max_tokens / max_completion_tokens）；未给出时为 None。"""
    v = body.get("max_tokens") or body.get("max_completion_tokens")
    return None if v is None else max(1, int(v))


def _fake_text(n: int, offset: int = 0) -> str:
    return " ".join(WORDS[(offset + i) % len(WORDS)] for i in range(n))


class ExecResult:
    def __init__(self, status: int, body: dict | None, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.status = status
        self.body = body
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class EmulatedExecutor:
    def __init__(self, model_name: str = "cpc-emulated", chunk_tokens: int = 16):
        self.model_name = model_name
        self.chunk_tokens = chunk_tokens

    async def models(self) -> dict:
        return {"object": "list", "data": [{"id": self.model_name, "object": "model", "owned_by": "cpc"}]}

    @staticmethod
    def validate(body: dict) -> None:
        if requested_output_tokens(body) is None:
            raise InvalidRequest("仿真服务器要求请求给出 max_tokens（或 max_completion_tokens）")

    def _timing(self, node: Server, body: dict) -> tuple[int, int, float, float]:
        n_in, n_out = approx_prompt_tokens(body), requested_output_tokens(body)
        assert n_out is not None  # 已由 validate 保证
        p = node.profile
        return n_in, n_out, prefill_s(p, n_in, node.cap_ratio), tpot_s(p, node.occupancy, node.cap_ratio)

    async def complete(self, node: Server, path: str, body: dict) -> ExecResult:
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

    async def stream(self, node: Server, path: str, body: dict, out: ExecResult) -> AsyncIterator[bytes]:
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
    """转发到真实推理实例。server.upstream.url 例如 http://127.0.0.1:8000。"""

    def __init__(self, timeout_s: float = 600.0, idle_timeout_s: float = 120.0):
        self.idle_timeout_s = idle_timeout_s  # 流式响应相邻数据片段的最大间隔
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0), trust_env=False)

    @staticmethod
    def validate(body: dict) -> None:
        return None

    async def models(self, nodes: list[Server]) -> tuple[int, dict]:
        """转发给第一个在线的真实服务器；没有时返回 503，上游失败返回 502。"""
        node = next((n for n in nodes if n.state == "on" and n.upstream), None)
        if node is None:
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
        url = node.upstream.url.rstrip("/") + path
        try:
            async with self.client.stream("POST", url, json=body) as r:
                out.status = r.status_code
                it = r.aiter_raw().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(it.__anext__(), self.idle_timeout_s)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        out.status = 502
                        msg = f"upstream {node.id} stalled: no data in {self.idle_timeout_s:g} s"
                        yield f"data: {json.dumps({'error': {'message': msg}})}\n\n".encode()
                        return
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


class SiteExecutor:
    """按服务器类型选择执行器：仿真服务器 → EmulatedExecutor，真实服务器 → OpenAIExecutor。"""

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
    """真实节点钩子。

    - 开关机：upstream 配置了 container 时通过 Docker Engine API 启停该容器；未配置时为逻辑开关
      （只停止向其派发请求，GPU 仍处于空载供电状态——测得的功率如实反映这一点）。
    - 功率封顶：显式开启（--enable-power-cap）时，通过管理该设备的传感器设置功率上限；未开启时只记录目标比例。
    - 就绪检查：GET {url}{ready_path}（默认 /health，vLLM 提供）返回 200。

    启动检查：配置了容器名时须能连接 Docker Engine 且容器存在；开启功率封顶时，每台真实服务器的加速卡都须由支持封顶的传感器管理。
    运行期操作失败时抛出异常，由站点代理记入服务器的 last_error 与失败计数。
    """

    def __init__(self, upstreams: list[UpstreamConfig], power=None, enable_power_cap: bool = False):
        self.power = power
        self.enable_power_cap = enable_power_cap
        self.client = httpx.AsyncClient(timeout=2.0, trust_env=False)
        self.docker = None
        containers = [u.container for u in upstreams if u.container]
        if containers:
            try:
                import docker  # type: ignore
            except ImportError as e:
                raise ConfigError(f"upstream 配置了容器名 {containers}，需要安装 docker SDK（pip install \"cpc-platform[docker]\"）") from e
            try:
                self.docker = docker.from_env()
                for c in containers:
                    self.docker.containers.get(c)
            except docker.errors.DockerException as e:
                raise ConfigError(f"无法通过 Docker Engine 访问容器 {containers}：{e}") from e
        if enable_power_cap:
            from .power import SensorPower

            if not isinstance(power, SensorPower):
                raise ConfigError("开启功率封顶需要传感器功率来源")
            bare = [u.id for u in upstreams if not u.device_names]
            if bare:
                raise ConfigError(f"服务器 {bare} 没有声明加速卡，无法进行硬件功率封顶")
            bad = [d for u in upstreams for d in u.device_names if not power.sensor_for(d).supports_power_limit]
            if bad:
                raise ConfigError(f"加速卡 {bad} 的传感器不支持功率封顶")

    async def power_on(self, node: Server) -> None:
        if self.docker and node.upstream and node.upstream.container:
            await asyncio.to_thread(lambda: self.docker.containers.get(node.upstream.container).start())
            log.info("已启动容器 %s", node.upstream.container)

    async def power_off(self, node: Server) -> None:
        if self.docker and node.upstream and node.upstream.container:
            await asyncio.to_thread(lambda: self.docker.containers.get(node.upstream.container).stop())
            log.info("已停止容器 %s", node.upstream.container)

    async def set_cap(self, node: Server, ratio: float) -> None:
        if not self.enable_power_cap:
            log.info("服务器 %s 功率封顶比例 %.2f（未开启硬件封顶，只记录目标比例）", node.id, ratio)
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
