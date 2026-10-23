"""src/term_banner.py: the hub's app list in the terminal.

Terminals cannot draw the SVG app icons, so each app gets a coloured mark in
its own accent colour (the same colour as its hub card and icon), read from
assets/icons/swaxs-tokens.css via the app's `accent` in apps.yml. Names and
ports are aligned in columns.

Colour is used only when the output is a terminal that can show it; NO_COLOR
(https://no-color.org) or a redirected log gives plain text.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_TOKENS = Path(__file__).resolve().parents[1] / "assets" / "icons" / "swaxs-tokens.css"
MARK = "■"


def accent_colours(css_path: Path = _TOKENS) -> dict[str, str]:
    """{'swaxs-reduction': '#2374ee', ...}: the base accent of every app."""
    try:
        css = css_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for name, hexv in re.findall(r"--(swaxs-[a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;", css):
        if not name.endswith(("-light", "-dark")):
            out.setdefault(name, hexv)        # first = the base (theme-neutral) value
    return out


def use_colour(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name == "nt":                      # turn on ANSI in the Windows console
        try:
            os.system("")
        except Exception:                    # noqa: BLE001
            return False
    return True


def _rgb(hexv: str) -> tuple[int, int, int]:
    h = hexv.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def app_lines(apps: list[dict], colour: bool | None = None,
              indent: str = "      ") -> list[str]:
    """One aligned line per app: coloured mark, name, :port."""
    colour = use_colour() if colour is None else colour
    acc = accent_colours() if colour else {}
    width = max((len(a.get("name", "")) for a in apps), default=0)
    lines = []
    for a in apps:
        mark = MARK
        hexv = acc.get(a.get("accent") or "")
        if colour and hexv:
            r, g, b = _rgb(hexv)
            mark = f"\033[38;2;{r};{g};{b}m{MARK}\033[0m"
        lines.append(f"{indent}{mark}  {a.get('name', ''):<{width}}  :{a.get('port')}")
    return lines
