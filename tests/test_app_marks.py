"""
tests/test_app_marks.py

Open any app and its own icon should be the one on its hub card and in its
browser tab. Three surfaces, one artwork:

    hub card   <use href="#swaxs-reduction">   from the inlined sprite
    tab icon   /app-icon                       inlined by src/favicon.py
    the app    {% include "_app_mark.svg" %}   its own generated partial

All three come from assets/icons/swaxs-icons-svg/<icon_id>.svg via
tools/build_icon_sprite.py, so they cannot show different art — but only if
the partials are regenerated when an icon changes, which is what this checks.

Why the app gets a partial rather than the sprite: the sprite is inlined into
the HUB's page on port 5100. A `<use href="#id">` on port 5103 would point at
a symbol in a document the browser never loaded, resolve to nothing, and draw
empty space. Same trap as the favicon.

The comparison against the sprite is by GEOMETRY, not bytes: the sprite is
serialised by ElementTree (`<circle … />`) while the partial is sliced from
the source file (`<circle …/>`). Identical drawings, one space apart — a
byte comparison reports all ten as different and teaches you to ignore it.
"""
from __future__ import annotations

import importlib.util as u
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")

_APPS = [a for a in yaml.safe_load((_ROOT / "apps.yml").read_text())["apps"]
         if a.get("icon_id")]
_IDS = [a["id"] for a in _APPS]
_SPRITE = (_ROOT / "assets" / "icons" / "swaxs-icons-sprite.svg").read_text()


def _meta(app_id):
    return next(a for a in _APPS if a["id"] == app_id)


def _geometry(svg: str):
    """Every shape attribute, order-independent — the drawing itself."""
    return sorted(re.findall(
        r'\b(d|cx|cy|r|x1|y1|x2|y2|points|rx|ry|width|height)="([^"]*)"', svg))


def _partial(app_id: str) -> str:
    return (_ROOT / app_id / "templates" / "_app_mark.svg").read_text()


@pytest.fixture(scope="module")
def pages():
    out = {}
    for app_id in _IDS:
        spec = u.spec_from_file_location(f"mark_{app_id}", f"{app_id}/app.py")
        mod = u.module_from_spec(spec)
        sys.modules[f"mark_{app_id}"] = mod
        spec.loader.exec_module(mod)
        c = mod.app.test_client()
        out[app_id] = (c.get("/").get_data(as_text=True), c)
    return out


# ── one artwork across all three surfaces ───────────────────────────────────
@pytest.mark.parametrize("app_id", _IDS)
def test_the_apps_mark_is_the_same_drawing_as_its_hub_card(app_id):
    icon_id = _meta(app_id)["icon_id"]
    sym = re.search(rf'<symbol id="{icon_id}"[^>]*>(.*?)</symbol>',
                    _SPRITE, re.S)
    assert sym, f"{icon_id} is not in the sprite"
    body = re.search(r"<svg[^>]*>(.*)</svg>", _partial(app_id), re.S).group(1)
    assert _geometry(body) == _geometry(sym.group(1)), (
        f"{app_id}'s mark has drifted from the sprite — "
        f"run python tools/build_icon_sprite.py")


@pytest.mark.parametrize("app_id", _IDS)
def test_the_partial_is_generated_from_the_declared_icon(app_id):
    """The header comment names its source. A partial pointing at a different
    icon than apps.yml declares is how the card and the page disagree."""
    assert f"{_meta(app_id)['icon_id']}.svg" in _partial(app_id)


def test_every_app_shows_a_different_mark():
    seen = {}
    for app_id in _IDS:
        body = re.search(r"<svg[^>]*>(.*)</svg>", _partial(app_id), re.S).group(1)
        seen.setdefault(str(_geometry(body)), []).append(app_id)
    clashes = [v for v in seen.values() if len(v) > 1]
    assert not clashes, f"apps sharing one mark: {clashes}"


# ── it renders, and it is coloured by a token ───────────────────────────────
@pytest.mark.parametrize("app_id", _IDS)
def test_the_mark_reaches_the_rendered_page(app_id, pages):
    html, _c = pages[app_id]
    body = html[html.index("<body>"):]
    assert 'class="app-mark"' in body, f"{app_id} renders no mark"
    # CONTRACT CHANGED (September 2026). Apps used to be forbidden from
    # <use>-ing sprite symbols, because only the hub inlined the sprite and a
    # cross-document <use> resolves to nothing. Now each app inlines its OWN
    # copy (templates/_icon_sprite.svg) so its UI can use the shared glyphs on
    # folder/stop/clear buttons and card headers. So <use> is allowed — but
    # ONLY when the sprite that defines those symbols is inlined on the same
    # page, or every reference is a blank square.
    if "<use" in body:
        assert "_icon_sprite" in html or "<symbol" in body, (
            f"{app_id} references a sprite symbol with <use> but does not inline "
            f"the sprite — those glyphs will render as nothing")
        # and every referenced id must actually be a symbol in that sprite
        import re as _re
        defined = set(_re.findall(r'<symbol[^>]*\bid="([^"]+)"', body))
        for ref in _re.findall(r'<use href="#([^"]+)"', body):
            assert ref in defined, f"{app_id}: <use href=#{ref}> has no matching symbol"


@pytest.mark.parametrize("app_id", _IDS)
def test_the_mark_is_painted_by_its_accent_token(app_id, pages):
    html, _c = pages[app_id]
    accent = _meta(app_id)["accent"]
    assert f"var(--{accent})" in html, f"{app_id} does not use --{accent}"
    assert "swaxs-tokens.css" in html, (
        f"{app_id} uses the token but never links the sheet that defines it — "
        f"the declaration is dropped and the mark takes the inherited colour")


@pytest.mark.parametrize("app_id", _IDS)
def test_the_token_sheet_is_served_by_the_app_itself(app_id):
    """Each Flask app serves its OWN static/. A <link> to the hub's copy would
    404 on port 5103."""
    assert (_ROOT / app_id / "static" / "swaxs-tokens.css").is_file(), \
        f"{app_id}/static/swaxs-tokens.css missing — run the build"


@pytest.mark.parametrize("app_id", _IDS)
def test_the_tab_icon_agrees_with_the_page(app_id, pages):
    _html, c = pages[app_id]
    r = c.get("/app-icon")
    assert r.status_code == 200
    svg = r.get_data(as_text=True)
    body = re.search(r"<svg[^>]*>(.*)</svg>", _partial(app_id), re.S).group(1)
    inner = _geometry(body)
    assert inner and all(kv in _geometry(svg) for kv in inner), \
        f"{app_id}'s tab icon draws something other than its page mark"
