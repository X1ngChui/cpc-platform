"""Helpers of the conversion examples. The examples are not part of the platform; they show how public raw
data become datasets in the platform format."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

from cpc.datasets import data_dir


def download(url: str, dest: Path, token_env: str | None = None) -> Path:
    """Download a raw file to dest (reused if present). External downloads use the system proxy settings."""
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {}
    if token_env:
        tok = os.environ.get(token_env)
        if not tok:
            sys.exit(f"downloading {url} needs an access token: set the environment variable {token_env}")
        headers["Authorization"] = f"Bearer {tok}"
    part = dest.with_name(dest.name + ".part")
    print(f"downloading {url}", file=sys.stderr)
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    part.replace(dest)
    return dest


def raw_file(raw: str | None, group: str, url: str, token_env: str | None = None) -> Path:
    """Raw file: --raw if given, otherwise downloaded to ${CPC_DATA_DIR}/raw/<group>/."""
    if raw:
        return Path(raw)
    return download(url, data_dir() / "raw" / group / url.rsplit("/", 1)[-1], token_env)


def out_dir(out: str | None, name: str) -> Path:
    """Output directory: --out if given, otherwise ${CPC_DATA_DIR}/<dataset name>."""
    return Path(out) if out else data_dir() / name


WORKLOAD_CORE = [
    {"name": "arrival_s", "type": "number", "unit": "s", "description": "arrival time (s from cpc.origin)",
     "constraints": {"required": True, "minimum": 0}},
    {"name": "input_tokens", "type": "integer", "description": "input tokens", "constraints": {"required": True, "minimum": 0}},
    {"name": "output_tokens", "type": "integer", "description": "output tokens", "constraints": {"required": True, "minimum": 1}},
]

SIGNAL_CORE = [
    {"name": "region", "type": "string", "constraints": {"required": True}},
    {"name": "start", "type": "datetime", "constraints": {"required": True}},
    {"name": "end", "type": "datetime", "constraints": {"required": True}},
    {"name": "signal", "type": "string", "constraints": {"required": True}},
    {"name": "value", "type": "number", "constraints": {"required": True}},
]


def zero_output(n_zero: int, mode: str, what: str) -> None:
    """Requests with 0 output tokens (the specification requires >= 1) need an explicit conversion option."""
    if n_zero and mode == "fail":
        sys.exit(f"{what} has {n_zero} requests with 0 output tokens; drop them explicitly with --zero-output drop")
