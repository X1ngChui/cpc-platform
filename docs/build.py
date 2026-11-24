"""编译文档：docs/<name>/main.typ → docs/pdf/<name>.pdf。

依赖：pip install typst（Typst 编译器的 Python 封装，跨平台，无需单独安装 Typst）。
字体：优先使用系统中的 Times New Roman / 宋体 / 黑体；Linux 可安装 fonts-noto-cjk。
Typst 包（fletcher 等）缓存在 docs/.typst-packages，已随仓库提供，可离线编译。

用法：python docs/build.py [design] [guide]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import typst

DOCS = Path(__file__).resolve().parent
OUT = DOCS / "pdf"
NAMES = ["design", "guide"]


def build(name: str) -> Path:
    OUT.mkdir(exist_ok=True)
    out = OUT / f"{name}.pdf"
    t0 = time.time()
    typst.compile(
        str(DOCS / name / "main.typ"),
        output=str(out),
        root=str(DOCS),
        package_cache_path=str(DOCS / ".typst-packages"),
        font_paths=[str(DOCS / "fonts")] if (DOCS / "fonts").exists() else [],
    )
    print(f"{out}  ({out.stat().st_size / 1024:.0f} KB, {time.time() - t0:.1f}s)")
    return out


if __name__ == "__main__":
    for n in sys.argv[1:] or NAMES:
        build(n)
