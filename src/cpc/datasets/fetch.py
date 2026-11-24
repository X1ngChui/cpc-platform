"""下载描述文件中以 URL 给出的资源到描述文件所在目录（cpc data fetch）。

外部下载使用系统代理设置（trust_env=True）；需要认证的资源可在描述文件的资源属性 cpc_auth 中声明认证方式，
目前支持 hf（从环境变量 HF_TOKEN 读取 Hugging Face 访问令牌）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .descriptor import DatasetError, Descriptor


def fetch(d: Descriptor, force: bool = False, progress: Callable[[str, int, int | None], None] | None = None) -> list[Path]:
    import httpx

    out = []
    for r in d.resources:
        if not r.is_remote:
            continue
        p = d.local_path(r)
        if p.is_file() and not force:
            out.append(p)
            continue
        headers = {}
        if r.extra.get("cpc_auth") == "hf":
            tok = os.environ.get("HF_TOKEN")
            if not tok:
                raise DatasetError(f"资源 {r.name} 需要 Hugging Face 访问令牌：请设置环境变量 HF_TOKEN")
            headers["Authorization"] = f"Bearer {tok}"
        part = p.with_name(p.name + ".part")
        try:
            with httpx.stream("GET", r.path, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as resp:
                resp.raise_for_status()
                done = 0
                with open(part, "wb") as w:
                    for chunk in resp.iter_bytes(1 << 20):
                        w.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(r.name, done, r.bytes)
        except httpx.HTTPError as e:
            raise DatasetError(f"下载 {r.path} 失败：{e}") from e
        part.replace(p)
        out.append(p)
    return out
