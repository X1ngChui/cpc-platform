"""把 docs/figures/src/*.drawio 导出为 docs/figures/*.pdf（文档中以矢量图引用）。

依赖：draw.io 桌面版（https://github.com/jgraph/drawio-desktop）的命令行导出功能。
可执行文件按以下顺序查找：环境变量 DRAWIO、PATH 中的 drawio / draw.io、Windows 默认安装位置。
图中文字使用 Noto Sans SC 字体（导出时嵌入 PDF）。

用法：python docs/figures/export.py [名称 ...]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def drawio() -> str:
    cands = [os.environ.get("DRAWIO"), shutil.which("drawio"), shutil.which("draw.io")]
    if os.environ.get("LOCALAPPDATA"):
        cands.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "draw.io" / "draw.io.exe"))
    for c in cands:
        if c and Path(c).exists():
            return c
    raise SystemExit("未找到 draw.io 可执行文件：请安装 draw.io 桌面版，或用环境变量 DRAWIO 指定路径")


def main() -> None:
    exe = drawio()
    names = sys.argv[1:] or sorted(p.stem for p in (HERE / "src").glob("*.drawio"))
    for n in names:
        src, out = HERE / "src" / f"{n}.drawio", HERE / f"{n}.pdf"
        subprocess.run([exe, "-x", "-f", "pdf", "--crop", "-o", str(out), str(src)], check=True, capture_output=True)
        print(f"{src.name} -> {out.name}")


if __name__ == "__main__":
    main()
