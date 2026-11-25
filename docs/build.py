"""Build the documentation: docs/<lang>/<name>/main.typ -> docs/pdf/<name>-<lang>.pdf.

Requires: pip install typst (Python bindings of the Typst compiler; no separate Typst installation).
Fonts: Noto Serif CJK SC / Noto Sans CJK SC for Chinese (Debian/Ubuntu package fonts-noto-cjk).
Figures are docs/figures/<lang>/*.pdf, exported by docs/figures/export.py from the draw.io sources in
docs/figures/src/<lang>/.

Usage: python docs/build.py [design] [guide] [--lang zh|en]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import typst

DOCS = Path(__file__).resolve().parent
OUT = DOCS / "pdf"
NAMES = ("design", "guide")
LANGS = ("zh", "en")


def build(name: str, lang: str) -> Path:
    OUT.mkdir(exist_ok=True)
    out = OUT / f"{name}-{lang}.pdf"
    t0 = time.time()
    fonts = [str(DOCS / "fonts")] if (DOCS / "fonts").exists() else []
    typst.compile(str(DOCS / lang / name / "main.typ"), output=str(out), root=str(DOCS), font_paths=fonts)
    print(f"{out}  ({out.stat().st_size / 1024:.0f} KB, {time.time() - t0:.1f}s)")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", metavar="{design,guide}")
    ap.add_argument("--lang", choices=LANGS, action="append", help="language (default: both)")
    args = ap.parse_args()
    if bad := set(args.names) - set(NAMES):
        ap.error(f"unknown documents {sorted(bad)}; choose from {', '.join(NAMES)}")
    for lang in args.lang or LANGS:
        for name in args.names or NAMES:
            build(name, lang)
