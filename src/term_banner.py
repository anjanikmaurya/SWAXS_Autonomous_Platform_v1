"""src/term_banner.py: the hub's app list in the terminal.

Each app is shown with its real SWAXS icon where the terminal can display
inline images, and as a coloured mark in the app's accent colour elsewhere:

    iTerm2, WezTerm, VS Code*      iTerm2 inline-image protocol
    kitty, Ghostty                 kitty graphics protocol
    macOS Terminal, Windows, tmux,
    logs, NO_COLOR                 coloured ■ (or plain ■ without colour)

  * VS Code shows images only with "terminal.integrated.enableImages" on.

The icons are pre-rendered PNGs in assets/icons/terminal/ (tools/build_term_icons.py),
in the same accent colour as the app's hub card, read from swaxs-tokens.css.
SWAXS_TERM_ICONS=images|squares|plain forces a mode.
"""
from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TOKENS = _ROOT / "assets" / "icons" / "swaxs-tokens.css"
_PNG_DIR = _ROOT / "assets" / "icons" / "terminal"
MARK = "■"
CELLS = 2                     # every mark occupies two columns, so names align


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


def image_protocol(env=None, stream=None) -> str | None:
    """'iterm', 'kitty' or None for the terminal we are printing to."""
    env = os.environ if env is None else env
    forced = (env.get("SWAXS_TERM_ICONS") or "").lower()
    if forced in ("squares", "plain", "off", "none"):
        return None
    if forced not in ("images", "image", "on"):
        if not use_colour(stream) or env.get("TMUX") or env.get("STY"):
            return None                      # logs, NO_COLOR, tmux / screen
    prog = env.get("TERM_PROGRAM", "")
    if env.get("KITTY_WINDOW_ID") or env.get("TERM") == "xterm-kitty" \
            or prog.lower() == "ghostty" or env.get("TERM") == "xterm-ghostty":
        return "kitty"
    if prog in ("iTerm.app", "WezTerm", "vscode") or env.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    return "iterm" if forced in ("images", "image", "on") else None


def _iterm(png: bytes) -> str:
    b64 = base64.b64encode(png).decode()
    return (f"\033]1337;File=inline=1;size={len(png)};width={CELLS};height=1;"
            f"preserveAspectRatio=1:{b64}\a")


def _kitty(png: bytes) -> str:
    b64 = base64.b64encode(png).decode()
    chunks = [b64[i:i + 4096] for i in range(0, len(b64), 4096)] or [""]
    out = []
    for i, c in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        head = f"a=T,f=100,c={CELLS},r=1,q=2,m={more}" if i == 0 else f"m={more}"
        out.append(f"\033_G{head};{c}\033\\")
    return "".join(out)


def _rgb(hexv: str) -> tuple[int, int, int]:
    h = hexv.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def app_lines(apps: list[dict], colour: bool | None = None,
              protocol: str | None = "auto", indent: str = "      ") -> list[str]:
    """One aligned line per app: icon (or coloured mark), name, :port."""
    if protocol == "auto":
        protocol = image_protocol()
    colour = (use_colour() if colour is None else colour) or bool(protocol)
    acc = accent_colours() if colour else {}
    width = max((len(a.get("name", "")) for a in apps), default=0)
    lines = []
    for a in apps:
        mark = MARK + " " * (CELLS - 1)
        png_path = _PNG_DIR / f"{a.get('icon_id', '')}.png"
        hexv = acc.get(a.get("accent") or "")
        if protocol and png_path.is_file():
            png = png_path.read_bytes()
            mark = _kitty(png) if protocol == "kitty" else _iterm(png)
        elif colour and hexv:
            r, g, b = _rgb(hexv)
            mark = f"\033[38;2;{r};{g};{b}m{MARK}\033[0m" + " " * (CELLS - 1)
        lines.append(f"{indent}{mark} {a.get('name', ''):<{width}}  :{a.get('port')}")
    return lines
