"""
src/favicon.py — per-app browser-tab icon, from apps.yml.

Ten apps on ten ports means ten near-identical browser tabs. Only the
assistant had a favicon, so picking the right tab meant reading truncated
titles. Every app now shows its own registry icon in the tab and in the
window switcher.

The icon comes from `apps.yml` — the same registry the hub cards read, so a
tab and its card can never disagree:

  * `icon_id` — the app's mark from the shared set (assets/icons/), drawn on a
    rounded tile in its `accent` token. First choice, so the tab, the hub card
    and the app's own chrome are one artwork;
  * `icon_image` when the app has one and no icon_id (the older 400-512 px
    PNGs);
  * otherwise the app's `icon` emoji on a rounded tile in its `color`.

The mark is inlined into the favicon rather than referenced from the sprite:
a favicon document is fetched on its own, so a `use href="#id"` pointing at a
symbol defined in some other document resolves to nothing and the tab goes
blank. currentColor is resolved to the accent's literal value for the same
reason — there is no page around it to inherit from.

Usage — one line per app, after `app = Flask(__name__)`:

    from src.favicon import register_favicon
    register_favicon(app, "reduction")

and in its template's <head>:

    <link rel="icon" href="/app-icon">

The path is extension-free on purpose: the same URL returns SVG for most apps
and PNG for the four with a custom icon, and calling it ".svg" while returning
PNG bytes would mislead the next reader. Browsers dispatch on Content-Type,
not on the extension. `register_favicon` also answers /favicon.ico, which
browsers request on their own initiative, so a tab still gets an icon if the
<link> is ever dropped.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APPS_YML = _ROOT / "apps.yml"

#: Parsed apps.yml, read once. Small file, read at import; a KeyError here is
#: a registry mistake and should be loud, not silently iconless.
_registry: dict | None = None


def _load_registry() -> dict:
    global _registry
    if _registry is None:
        try:
            import yaml
            data = yaml.safe_load(_APPS_YML.read_text(encoding="utf-8")) or {}
            _registry = {a["id"]: a for a in (data.get("apps") or []) if a.get("id")}
        except Exception:
            _registry = {}
    return _registry


def app_meta(app_id: str) -> dict:
    """The registry entry for one app id, or {} when unknown (e.g. the hub,
    which has no `apps.yml` entry of its own)."""
    return dict(_load_registry().get(app_id) or {})


# Fallbacks for an app with no registry entry — the hub is the real case.
_DEFAULTS = {
    "hub": {"icon": "🧭", "color": "#B1040E", "name": "SWAXS Hub"},
}


def _icon_and_colour(app_id: str) -> tuple[str, str]:
    meta = app_meta(app_id) or _DEFAULTS.get(app_id, {})
    return (str(meta.get("icon") or "●"), str(meta.get("color") or "#B1040E"))


_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"


def _sprite_mark(icon_id: str) -> str | None:
    """The inner markup of one source icon, or None. No sprite, no <use>."""
    try:
        raw = (_ICON_DIR / "swaxs-icons-svg" / f"{icon_id}.svg").read_text()
    except OSError:
        return None
    m = re.search(r"<svg\b[^>]*>(.*)</svg>", raw, re.S)
    if not m:
        return None
    root = re.match(r"<svg\b([^>]*)>", raw, re.S)
    keep = " ".join(
        f'{a}="{v}"' for a, v in re.findall(r'([\w-]+)="([^"]*)"', root.group(1))
        if a in ("fill", "stroke", "stroke-width", "stroke-linecap",
                 "stroke-linejoin"))
    return f'<g {keep}>{m.group(1).strip()}</g>'


def _token_value(token: str) -> str | None:
    """The DARK value of an accent token from swaxs-tokens.css.

    Dark, because the tile behind the mark is the app's dark surface. Read from
    the stylesheet rather than duplicated here, so a colour change upstream
    reaches the tab without anyone remembering this file.
    """
    try:
        css = (_ICON_DIR / "swaxs-tokens.css").read_text()
    except OSError:
        return None
    m = re.search(rf"^\s*{re.escape(token)}-dark\s*:\s*(#[0-9a-fA-F]{{3,8}})",
                  css, re.M)
    return m.group(1) if m else None


def mark_favicon_svg(icon_id: str, accent_token: str) -> str | None:
    """A tab icon carrying the app's own mark. None if anything is missing."""
    inner = _sprite_mark(icon_id)
    colour = _token_value(f"--{accent_token}")
    if not inner or not colour:
        return None
    # The mark is drawn on 24x24; the tile is 64x64 with the art inset ~10px so
    # it does not touch the rounded corners at 16 px.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="14" fill="#12161c"/>'
        f'<g transform="translate(11 11) scale(1.75)" color="{colour}" '
        f'stroke="{colour}">{inner}</g></svg>'
    )


def favicon_svg(emoji: str, color: str) -> str:
    """A rounded tile in `color` with `emoji` centred, sized for a tab.

    viewBox 0 0 64 64 with the emoji at 38 px: a favicon is drawn at 16-32 px,
    so the glyph has to fill most of the tile or it vanishes. `dominant-
    baseline` is deliberately not used — support for it on <text> is uneven,
    and a hand-placed `y` is predictable everywhere.
    """
    safe = (emoji.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{color}"/>'
        '<text x="32" y="46" font-size="38" text-anchor="middle" '
        'font-family="Apple Color Emoji,Segoe UI Emoji,Noto Color Emoji,sans-serif">'
        f'{safe}</text></svg>'
    )


def register_favicon(flask_app, app_id: str) -> None:
    """Serve this app's tab icon at /app-icon and /favicon.ico.

    Never raises and never blocks startup: a missing registry entry or a
    missing PNG degrades to the emoji tile, and a broken apps.yml degrades to
    a plain coloured tile. An app failing to boot over a favicon would be a
    poor trade.
    """
    from flask import Response, send_from_directory

    meta = app_meta(app_id)
    emoji, color = _icon_and_colour(app_id)

    # The shared mark wins when the registry names one. Built once here, not
    # per request — it reads two files.
    mark = None
    if meta.get("icon_id") and meta.get("accent"):
        mark = mark_favicon_svg(str(meta["icon_id"]), str(meta["accent"]))

    # The custom PNGs live in the owning app's own static/ folder, which Flask
    # already serves — resolve it once at registration rather than per request.
    png_dir = png_name = None
    rel = str(meta.get("icon_image") or "")
    if rel.startswith("/static/"):
        candidate = Path(flask_app.root_path) / "static" / rel[len("/static/"):]
        if candidate.is_file():
            png_dir, png_name = str(candidate.parent), candidate.name

    # No day-long cache. A favicon cached for 86400 s is why a changed icon
    # kept showing the OLD art for up to a day after a restart — the browser
    # never re-fetched. `no-cache` makes it revalidate on every load, which for
    # a ~1 KB SVG is free, so a rebuilt icon appears the next time the tab loads
    # instead of tomorrow.
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    def _send():
        if mark:
            return Response(mark, mimetype="image/svg+xml", headers=_NO_CACHE)
        if png_name:
            return send_from_directory(png_dir, png_name,
                                       mimetype="image/png", max_age=0)
        return Response(favicon_svg(emoji, color), mimetype="image/svg+xml",
                        headers=_NO_CACHE)

    # Distinct endpoint names: several apps are imported into one process by
    # the test suite, and Flask rejects a duplicate endpoint on the same app.
    flask_app.add_url_rule("/app-icon", f"app_icon_{app_id}", _send)
    flask_app.add_url_rule("/favicon.ico", f"favicon_ico_{app_id}", _send)
