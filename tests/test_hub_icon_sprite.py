"""
tests/test_hub_icon_sprite.py

The hub's emoji were replaced with the SVG set in assets/icons/.

Three things here are easy to get wrong in a way that looks fine until you
open the page, so they are pinned:

  * **an unresolved reference renders NOTHING.** `<use href="#typo">` is not an
    error — the browser draws empty space. A card would simply lose its mark.
    Every reference on the page is checked against the symbols actually in the
    sprite.

  * **the sprite is generated, and generated files go stale.** It is built from
    assets/icons/swaxs-icons-svg/ into two places; if someone edits an icon and
    forgets to re-run the script, the two copies drift and the page shows the
    old art.

  * **colour must come from a token.** The icons are drawn with
    `stroke="currentColor"`, so a hex at the call site would pin one colour
    across both themes and break the one property that makes the set themeable.

Also checked: an app card's mark and its top stripe take the SAME token, so
they cannot drift into two different colours for one app.
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

_APPS = yaml.safe_load((_ROOT / "apps.yml").read_text())["apps"]
_SPRITE = _ROOT / "assets" / "icons" / "swaxs-icons-sprite.svg"
_PARTIAL = _ROOT / "hub" / "templates" / "_icon_sprite.svg"
_TOKENS = _ROOT / "assets" / "icons" / "swaxs-tokens.css"


@pytest.fixture(scope="module")
def page():
    spec = u.spec_from_file_location("hub_icons", _ROOT / "hub" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["hub_icons"] = mod
    spec.loader.exec_module(mod)
    html = mod.app.test_client().get("/").get_data(as_text=True)
    return html


def _body(html: str) -> str:
    return html[html.index("<body>"):]


def _symbols(html: str) -> set[str]:
    return set(re.findall(r'<symbol id="([^"]+)"', html))


# ── the sprite ──────────────────────────────────────────────────────────────
def test_the_sprite_is_in_sync_with_its_source_icons():
    """Re-run tools/build_icon_sprite.py if this fails — do not hand-edit."""
    from tools.build_icon_sprite import build
    fresh, _ids = build(_ROOT / "assets" / "icons" / "swaxs-icons-svg")
    assert _SPRITE.read_text() == fresh, \
        "the sprite is stale; run python tools/build_icon_sprite.py"


def test_the_sprite_holds_every_source_icon_except_the_skipped_variants():
    """DERIVED, not a literal.

    A hardcoded number is the kind of assertion that keeps passing for the
    wrong reason: it went from 27 to 33 when eight glyphs were added and two
    alt variants were excluded, and a stale 27 would simply have failed while a
    stale 35 would have been wrong in the other direction. Counting the files
    and subtracting the documented skips cannot drift.

    Note the arithmetic: there are 35 .svg files but 33 symbols, because
    swaxs-reduction-alt-b and -alt-c are deliberately left out.
    """
    from tools.build_icon_sprite import _SKIP
    files = {p.stem for p in (_ROOT / "assets" / "icons" / "swaxs-icons-svg").glob("*.svg")}
    expected = files - set(_SKIP)
    got = set(re.findall(r'<symbol id="([^"]+)"', _SPRITE.read_text()))
    assert got == expected, (
        f"missing from sprite: {sorted(expected - got)}; "
        f"unexpected in sprite: {sorted(got - expected)}")
    assert set(_SKIP) & files == set(_SKIP), \
        "the skip list names files that no longer exist — stale exclusion"


@pytest.mark.parametrize("icon,token", [
    ("swaxs-ui-warning",    "--swaxs-status-warning"),
    ("swaxs-ui-pass",       "--swaxs-status-running"),
    ("swaxs-ui-flag",       "--swaxs-status-warning"),
    ("swaxs-ui-close",      "--swaxs-chrome"),
    ("swaxs-ui-parent-dir", "--swaxs-chrome"),
    ("swaxs-ui-drive",      "--swaxs-chrome"),
])
def test_each_new_glyph_replaced_its_emoji_with_the_right_token(icon, token, page):
    """The six that had no counterpart until this icon drop."""
    src = (_ROOT / "hub" / "templates" / "index.html").read_text()
    assert f'#{icon}"' in src, f"{icon} is never referenced"
    for m in re.finditer(rf'style="([^"]*)"[^>]*>\s*<use href="#{icon}"', src):
        assert token in m.group(1), f"{icon} coloured with {m.group(1)}, want {token}"


def test_no_emoji_remains_where_an_icon_exists():
    """Only two non-ASCII marks should survive in the hub: the arrow inside an
    app DESCRIPTION string (prose, "2D → 1D"), and the box-drawing rules in
    JS section comments. Neither is UI chrome."""
    src = (_ROOT / "hub" / "templates" / "index.html").read_text()
    body = src[src.index("<body>"):]
    gone = "⚠✅⚑✕⬆💾🔌📁🎯🌀🔁🧭▶■↗"
    found = sorted({c for c in gone if c in body})
    assert not found, f"emoji still in the hub body: {found}"


def test_both_copies_of_the_sprite_match():
    assert _SPRITE.read_text() == _PARTIAL.read_text(), \
        "assets/ and hub/templates/ hold different sprites"


def test_symbols_carry_no_fixed_size():
    """A width/height on a <symbol> overrides the <use>, so every instance
    would render at 24px however the CSS sizes it."""
    for m in re.finditer(r"<symbol ([^>]*)>", _SPRITE.read_text()):
        attrs = m.group(1)
        assert " width=" not in attrs and " height=" not in attrs, attrs[:80]


def test_symbols_keep_the_stroke_attributes_they_need():
    """The source files set stroke/fill on the root <svg> and the children
    inherit. Dropped, every icon renders as a black silhouette."""
    sprite = _SPRITE.read_text()
    red = re.search(r'<symbol id="swaxs-reduction"([^>]*)>', sprite).group(1)
    assert 'stroke="currentColor"' in red
    assert "stroke-width" in red and 'fill="none"' in red


def test_the_sprite_is_hidden_and_inlined_once(page):
    assert page.count("<symbol id=") == len(_symbols(page)), "sprite inlined twice?"
    assert 'style="display:none"' in page


# ── every reference resolves ────────────────────────────────────────────────
def test_no_reference_points_at_a_missing_symbol(page):
    """The failure mode this guards: an unresolved reference is silent — the
    browser draws nothing and the icon is simply absent."""
    syms = _symbols(page)
    used = set(re.findall(r'<use href="#([^"]+)"', _body(page)))
    assert used, "the page uses no icons at all"
    assert not (used - syms), f"unresolved: {sorted(used - syms)}"


# ── colour comes from tokens, never a hex ───────────────────────────────────
def test_no_icon_is_coloured_with_a_hex(page):
    for m in re.finditer(r'<svg class="ico"[^>]*style="([^"]*)"', _body(page)):
        style = m.group(1)
        assert "#" not in style, f"hex on an icon: {style}"
        assert "var(--swaxs-" in style or "color:inherit" in style, style


def test_every_token_the_page_uses_is_defined(page):
    css = _TOKENS.read_text()
    used = set(re.findall(r"var\((--swaxs-[\w-]+)\)", _body(page)))
    missing = sorted(t for t in used
                     if not re.search(rf"^\s*{re.escape(t)}\s*:", css, re.M))
    assert not missing, f"used but undefined in swaxs-tokens.css: {missing}"


def test_the_chrome_and_status_tokens_are_inside_a_rule():
    """They were declared at the stylesheet's top level — invalid CSS, silently
    discarded, so every var(--swaxs-chrome) resolved to nothing."""
    css = re.sub(r"/\*.*?\*/", "", _TOKENS.read_text(), flags=re.S)
    depth = 0
    for line in css.splitlines():
        for ch in line:
            depth += ch == "{"
            depth -= ch == "}"
        if re.match(r"\s*--[\w-]+\s*:", line):
            assert depth > 0, f"declaration outside any rule: {line.strip()}"


# ── card mark and stripe cannot disagree ────────────────────────────────────
@pytest.mark.parametrize("app_id", [a["id"] for a in _APPS])
def test_each_card_mark_and_stripe_share_one_token(app_id, page):
    a = next(x for x in _APPS if x["id"] == app_id)
    card = re.search(rf'<div class="app-card" id="card-{app_id}".*?</div>\s*</div>',
                     _body(page), re.S).group(0)
    tok = f"--{a['accent']}"
    assert f"--card-color:var({tok})" in card, f"{app_id} stripe is not {tok}"
    assert f"color:var({tok})" in card, f"{app_id} mark is not {tok}"
    assert f'<use href="#{a["icon_id"]}"/>' in card


def test_guinier_uses_different_ids_for_its_symbol_and_its_token():
    """The one app where they differ: symbol swaxs-guinier-ai, token
    --swaxs-guinier. Easy to "tidy" into a mismatch."""
    a = next(x for x in _APPS if x["id"] == "assistant")
    assert a["icon_id"] == "swaxs-guinier-ai"
    assert a["accent"] == "swaxs-guinier"


# ── the buttons take their button's colour ──────────────────────────────────
@pytest.mark.parametrize("icon", ["swaxs-ui-start", "swaxs-ui-stop", "swaxs-ui-open"])
def test_card_buttons_inherit_rather_than_pick_a_token(icon, page):
    """They sit inside coloured buttons, so an accent would fight the button."""
    for m in re.finditer(rf'<svg class="ico"[^>]*style="([^"]*)"[^>]*>'
                         rf'<use href="#{icon}"/>', _body(page)):
        assert "color:inherit" in m.group(1), m.group(1)


def test_the_status_indicator_keeps_its_text(page):
    """Icons replace the coloured dots; the label beside them is unchanged."""
    assert 'id="lbl-reduction"' in page
    assert "status-dot" in page, "the dot element was removed, not repurposed"
    assert '<use href="#swaxs-ui-status-stopped"/>' in page
