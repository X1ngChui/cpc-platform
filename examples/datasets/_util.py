"""转换范例的公共函数。范例代码不属于平台核心，演示如何把原始公开数据整理为符合平台规范的数据集。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

from cpc.datasets import data_dir


def download(url: str, dest: Path, token_env: str | None = None) -> Path:
    """下载原始文件到 dest（已存在时直接使用）。外部下载使用系统代理设置。"""
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {}
    if token_env:
        tok = os.environ.get(token_env)
        if not tok:
            sys.exit(f"下载 {url} 需要访问令牌：请设置环境变量 {token_env}")
        headers["Authorization"] = f"Bearer {tok}"
    part = dest.with_name(dest.name + ".part")
    print(f"下载 {url}", file=sys.stderr)
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    part.replace(dest)
    return dest


def raw_file(raw: str | None, group: str, url: str, token_env: str | None = None) -> Path:
    """原始文件：给出 --raw 时使用该文件，否则下载到 ${CPC_DATA_DIR}/raw/<group>/。"""
    if raw:
        return Path(raw)
    return download(url, data_dir() / "raw" / group / url.rsplit("/", 1)[-1], token_env)


def out_dir(out: str | None, name: str) -> Path:
    """输出目录：给出 --out 时使用该目录，否则为 ${CPC_DATA_DIR}/<数据集名>。"""
    return Path(out) if out else data_dir() / name


WORKLOAD_CORE = [
    {"name": "arrival_s", "type": "number", "unit": "s", "description": "到达时间（相对 cpc.origin 的秒数）",
     "constraints": {"required": True, "minimum": 0}},
    {"name": "input_tokens", "type": "integer", "description": "输入 token 数", "constraints": {"required": True, "minimum": 0}},
    {"name": "output_tokens", "type": "integer", "description": "输出 token 数", "constraints": {"required": True, "minimum": 1}},
]

SIGNAL_CORE = [
    {"name": "region", "type": "string", "constraints": {"required": True}},
    {"name": "start", "type": "datetime", "constraints": {"required": True}},
    {"name": "end", "type": "datetime", "constraints": {"required": True}},
    {"name": "signal", "type": "string", "constraints": {"required": True}},
    {"name": "value", "type": "number", "constraints": {"required": True}},
]


def zero_output(n_zero: int, mode: str, what: str) -> None:
    """输出 token 数为 0 的请求（平台规范要求 ≥ 1）须由转换参数显式处理。"""
    if n_zero and mode == "fail":
        sys.exit(f"{what} 中有 {n_zero} 个请求的输出 token 数为 0；请用 --zero-output drop 显式剔除")
