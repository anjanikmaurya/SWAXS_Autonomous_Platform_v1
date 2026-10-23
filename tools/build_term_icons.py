#!/usr/bin/env python3
"""build_term_icons.py: small PNG copies of the app icons for the terminal.

The hub prints its app list with the real SWAXS icons in terminals that can show
inline images (src/term_banner.py). Terminals take PNG, not SVG, so this
renders each app icon in its accent colour (assets/icons/swaxs-tokens.css) to
assets/icons/terminal/<icon_id>.png, which are committed. Needs cairosvg only
here, at build time; the hub itself needs nothing extra.

    python tools/build_term_icons.py

Re-run after editing an icon or an accent colour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.term_banner import accent_colours  # noqa: E402

SRC = ROOT / "assets" / "icons" / "swaxs-icons-svg"
OUT = ROOT / "assets" / "icons" / "terminal"
SIZE = 64          # px; the terminal scales it into 2 cells


def build() -> list[Path]:
    import cairosvg
    acc = accent_colours()
    apps = yaml.safe_load((ROOT / "apps.yml").read_text(encoding="utf-8"))["apps"]
    OUT.mkdir(parents=True, exist_ok=True)
    out = []
    for a in apps:
        icon, accent = a.get("icon_id"), a.get("accent")
        if not icon or accent not in acc:
            continue
        svg = (SRC / f"{icon}.svg").read_text(encoding="utf-8")
        svg = svg.replace("currentColor", acc[accent])
        dest = OUT / f"{icon}.png"
        cairosvg.svg2png(bytestring=svg.encode(), write_to=str(dest),
                         output_width=SIZE, output_height=SIZE)
        out.append(dest)
    return out


if __name__ == "__main__":
    for p in build():
        print(p.relative_to(ROOT), p.stat().st_size, "bytes")
