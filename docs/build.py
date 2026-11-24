"""编译文档：docs/<name>/main.typ → docs/pdf/<name>.pdf。

依赖：pip install typst（Typst 编译器的 Python 封装，无需单独安装 Typst）。
字体：Noto Serif CJK SC / Noto Sans CJK SC（Debian/Ubuntu 软件包 fonts-noto-cjk）。
插图为 docs/figures/*.pdf，由 docs/figures/export.py 从 draw.io 源文件（docs/figures/src/）导出。

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
        font_paths=[str(DOCS / "fonts")] if (DOCS / "fonts").exists() else [],
    )
    print(f"{out}  ({out.stat().st_size / 1024:.0f} KB, {time.time() - t0:.1f}s)")
    return out


if __name__ == "__main__":
    for n in sys.argv[1:] or NAMES:
        build(n)
