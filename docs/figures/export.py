"""Export docs/figures/src/<lang>/*.drawio to docs/figures/<lang>/*.pdf (vector figures of the documents).

Requires the command-line export of draw.io desktop (https://github.com/jgraph/drawio-desktop). The
executable is looked up in the environment variable DRAWIO, then drawio / draw.io on PATH, then the default
Windows installation. Figure text uses the Noto Sans SC font (embedded in the PDF).

Usage: python docs/figures/export.py [--lang zh|en] [name ...]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANGS = ("zh", "en")


def drawio() -> str:
    cands = [os.environ.get("DRAWIO"), shutil.which("drawio"), shutil.which("draw.io")]
    if os.environ.get("LOCALAPPDATA"):
        cands.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "draw.io" / "draw.io.exe"))
    if exe := next((c for c in cands if c and Path(c).exists()), None):
        return exe
    raise SystemExit("draw.io executable not found: install draw.io desktop or set DRAWIO to its path")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--lang", choices=LANGS, action="append", help="language (default: both)")
    args = ap.parse_args()
    exe = drawio()
    for lang in args.lang or LANGS:
        (HERE / lang).mkdir(exist_ok=True)
        for n in args.names or sorted(p.stem for p in (HERE / "src" / lang).glob("*.drawio")):
            src, out = HERE / "src" / lang / f"{n}.drawio", HERE / lang / f"{n}.pdf"
            subprocess.run([exe, "-x", "-f", "pdf", "--crop", "-o", str(out), str(src)], check=True, capture_output=True)
            print(f"{lang}/{src.name} -> {lang}/{out.name}")


if __name__ == "__main__":
    main()
