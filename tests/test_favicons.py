"""
tests/test_favicons.py

Ten apps on ten ports gave ten near-identical browser tabs — only the
assistant had a favicon, so picking a tab meant reading truncated titles.
Every app now serves its own registry icon at /app-icon (and /favicon.ico,
which browsers request unprompted) and links it from its template.

The icon comes from apps.yml, the same registry the hub cards read, so a tab
and its card cannot disagree. These tests pin that wiring: it is the kind of
thing that silently rots when an app is added.
"""
from __future__ import annotations

import importlib.util as u
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")
os.environ.setdefault("SWAXS_NO_RESUME", "1")

from src.favicon import app_meta, favicon_svg, register_favicon   # noqa: E402

#: Every app with a browser tab. The hub has no apps.yml entry of its own and
#: falls back to src/favicon.py's defaults — included deliberately.
APPS = ["hub", "calibration", "reduction", "average", "background", "quality",
        "analysis", "analyzer", "reactor", "assistant", "watchdog"]

LINK = 'href="/app-icon"'


# ── the SVG generator ───────────────────────────────────────────────────────
def test_the_generated_svg_carries_the_emoji_and_the_app_colour():
    svg = favicon_svg("🎯", "#AD1457")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert 'viewBox="0 0 64 64"' in svg, "a tab icon is drawn at 16-32 px"
    assert "#AD1457" in svg and "🎯" in svg


def test_the_generated_svg_escapes_markup_in_the_icon_field():
    """apps.yml is hand-edited; an `&` or `<` in the icon field must not
    produce invalid SVG."""
    svg = favicon_svg("<&>", "#000000")
    assert "&lt;&amp;&gt;" in svg
    assert "<&>" not in svg.replace("&lt;&amp;&gt;", "")


# ── every app serves and links one ──────────────────────────────────────────
@pytest.fixture(scope="module")
def modules():
    loaded = {}
    for app_id in APPS:
        spec = u.spec_from_file_location(f"favtest_{app_id}", f"{app_id}/app.py")
        mod = u.module_from_spec(spec)
        sys.modules[f"favtest_{app_id}"] = mod
        spec.loader.exec_module(mod)
        loaded[app_id] = mod
    return loaded


@pytest.mark.parametrize("app_id", APPS)
def test_every_app_serves_a_tab_icon(app_id, modules):
    client = modules[app_id].app.test_client()
    for path in ("/app-icon", "/favicon.ico"):
        r = client.get(path)
        assert r.status_code == 200, f"{app_id} {path} -> {r.status_code}"
        body = r.get_data()
        assert body, f"{app_id} {path} served nothing"
        is_png = body[:4] == b"\x89PNG"
        is_svg = b"<svg" in body[:120]
        assert is_png or is_svg, f"{app_id} {path} is neither PNG nor SVG"
        ctype = r.headers.get("Content-Type", "")
        assert ("image/png" if is_png else "image/svg+xml") in ctype, \
            f"{app_id} {path} content-type {ctype!r} does not match its bytes"


@pytest.mark.parametrize("app_id", APPS)
def test_every_template_links_the_icon(app_id, modules):
    """/favicon.ico alone is a browser courtesy, not a contract — the <link>
    is what guarantees the tab shows it."""
    page = modules[app_id].app.test_client().get("/").get_data(as_text=True)
    assert LINK in page, f"{app_id}/templates/index.html has no {LINK}"
    assert 'rel="icon"' in page


@pytest.mark.parametrize("app_id", APPS)
def test_the_icon_matches_the_registry(app_id, modules):
    """The tab and the hub card read the SAME apps.yml entry, so they cannot
    drift apart. An app with a custom icon_image serves that PNG; everything
    else serves its emoji on its colour."""
    meta = app_meta(app_id)
    body = modules[app_id].app.test_client().get("/app-icon").get_data()
    rel = str(meta.get("icon_image") or "")
    if rel:
        assert body[:4] == b"\x89PNG", \
            f"{app_id} declares icon_image {rel} but serves an emoji tile"
        return
    text = body.decode("utf-8")
    if meta:                            # the hub has no registry entry
        assert str(meta["icon"]) in text, f"{app_id} tile has the wrong emoji"
        assert str(meta["color"]).upper() in text.upper(), \
            f"{app_id} tile has the wrong colour"


def test_no_two_apps_share_an_icon(modules):
    """The whole point is telling tabs apart at a glance."""
    seen: dict = {}
    for app_id in APPS:
        body = modules[app_id].app.test_client().get("/app-icon").get_data()
        seen.setdefault(body, []).append(app_id)
    clashes = {tuple(v) for v in seen.values() if len(v) > 1}
    assert not clashes, f"these apps serve an identical tab icon: {clashes}"


# ── degradation ─────────────────────────────────────────────────────────────
def test_an_unknown_app_id_still_gets_an_icon():
    """A new app wired before its apps.yml entry exists must not 404 its own
    favicon, and must certainly not fail to boot over one."""
    from flask import Flask
    app = Flask("nope")
    register_favicon(app, "not_in_the_registry")
    r = app.test_client().get("/app-icon")
    assert r.status_code == 200 and b"<svg" in r.get_data()


def test_a_declared_but_missing_png_falls_back_to_the_emoji_tile(tmp_path,
                                                                  monkeypatch):
    from flask import Flask
    import src.favicon as fav
    monkeypatch.setattr(fav, "_registry", {
        "ghost": {"id": "ghost", "icon": "👻", "color": "#123456",
                  "icon_image": "/static/does_not_exist.png"}})
    app = Flask("ghost", root_path=str(tmp_path))
    fav.register_favicon(app, "ghost")
    body = app.test_client().get("/app-icon").get_data()
    assert b"<svg" in body and "👻".encode() in body


def test_a_broken_registry_does_not_stop_an_app_serving_an_icon(monkeypatch):
    from flask import Flask
    import src.favicon as fav
    monkeypatch.setattr(fav, "_registry", {})
    app = Flask("broken")
    fav.register_favicon(app, "reduction")
    r = app.test_client().get("/app-icon")
    assert r.status_code == 200, "an app must never fail to boot over a favicon"
