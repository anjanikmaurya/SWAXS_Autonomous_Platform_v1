#!/usr/bin/env python3
"""
build_doc_icons.py: coloured copies of the app icons for README / QUICKSTART.

The source icons in assets/icons/swaxs-icons-svg/ draw with `currentColor`.
Inside an <img> on GitHub that resolves to black, which disappears in GitHub's
dark theme. This writes one copy per app into assets/icons/docs/ with the
app's accent colour baked in, switching to the dark-theme accent under
`prefers-color-scheme: dark`. Colours are read from assets/icons/swaxs-tokens.css,
so the docs always match the apps.

    python tools/build_doc_icons.py

Idempotent: run it again after editing an icon or an accent colour.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "icons" / "swaxs-icons-svg"
OUT = ROOT / "assets" / "icons" / "docs"
TOKENS = ROOT / "assets" / "icons" / "swaxs-tokens.css"

# icon file stem -> accent token (apps.yml icon_id / accent)
ICONS = {
    "swaxs-calibration": "swaxs-calibration",
    "swaxs-reduction": "swaxs-reduction",
    "swaxs-visualisation": "swaxs-visualisation",
    "swaxs-subtraction": "swaxs-subtraction",
    "swaxs-quality": "swaxs-quality",
    "swaxs-analysis": "swaxs-analysis",
    "swaxs-autofit": "swaxs-autofit",
    "swaxs-synthesis": "swaxs-synthesis",
    "swaxs-guinier-ai": "swaxs-guinier",
    "swaxs-watch": "swaxs-watch",
    "swaxs-ui-mark": None,  # platform mark: neutral
}
NEUTRAL = ("#1f2328", "#e6edf3")  # GitHub's own light / dark text colours


def _accents(css: str) -> dict[str, tuple[str, str]]:
    out = {}
    for m in re.finditer(r"--(swaxs-[a-z-]+?)-light:\s*(#[0-9a-fA-F]{6})", css):
        dark = re.search(rf"--{m.group(1)}-dark:\s*(#[0-9a-fA-F]{{6}})", css)
        if dark:
            out[m.group(1)] = (m.group(2), dark.group(1))
    return out


def build() -> list[Path]:
    accents = _accents(TOKENS.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    for stem, token in ICONS.items():
        svg = (SRC / f"{stem}.svg").read_text(encoding="utf-8")
        light, dark = accents[token] if token else NEUTRAL
        style = (f"<style>svg{{color:{light}}}"
                 f"@media (prefers-color-scheme: dark){{svg{{color:{dark}}}}}</style>")
        svg = re.sub(r"(<svg\b[^>]*>)", r"\1\n  " + style, svg, count=1)
        dest = OUT / f"{stem}.svg"
        dest.write_text(svg, encoding="utf-8")
        written.append(dest)
    return written


if __name__ == "__main__":
    for p in build():
        print(p.relative_to(ROOT))
    sys.exit(0)
