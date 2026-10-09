"""Icons come from the platform sprite only — never emoji.

Emoji render differently on every OS (and sometimes as empty boxes on a beamline
PC), ignore the light/dark theme, and do not match the platform's drawn icon set.
Every in-UI icon is <svg class="ico"><use href="#swaxs-ui-…"/></svg>, or built
with the shared icoSvg()/setIco() helpers. New icons go in
assets/icons/swaxs-icons-svg/ and `python tools/build_icon_sprite.py` ships them
to every app.

Plain text marks (✓ ✗ ⚠ arrows, ▲▼ sort triangles, ▾ disclosure) are typography,
not pictographs, and are allowed. HTML comments are ignored.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(_ROOT.glob("*/templates/index.html"))

# Pictographic emoji blocks + the specific symbol-emoji that were used as icons.
_PICTO = re.compile(
    "[\U0001F300-\U0001FAFF\U0001F000-\U0001F2FF"
    "⏩-⏺⬆⬇♻⚡⚙✂✏✨ℹ⛔⛽]️?")


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_no_emoji_icons_in_the_ui(tpl):
    body = re.sub(r"<!--.*?-->", "", tpl.read_text(encoding="utf-8"), flags=re.S)
    hits = []
    for m in _PICTO.finditer(body):
        line = body.count("\n", 0, m.start()) + 1
        hits.append(f"line ~{line}: {m.group(0)!r}")
    assert not hits, (f"{tpl.parent.parent.name} uses emoji as icons — use the sprite "
                      f"(icoSvg / <svg class=\"ico\">) instead:\n  " + "\n  ".join(hits[:15]))


def test_every_app_inlines_the_sprite_and_the_helpers():
    for tpl in TEMPLATES:
        s = tpl.read_text(encoding="utf-8")
        app = tpl.parent.parent.name
        assert "_icon_sprite.svg" in s, f"{app} does not inline the icon sprite"
        assert "_icon_helpers.html" in s, f"{app} does not include the icon helpers"


# ── page layout (operator's choice, October 2026) ────────────────────────────
APP_TEMPLATES = [t for t in TEMPLATES if t.parent.parent.name != "hub"]

#: EVERY app has the same full-width top bar (logo + name, status, project path,
#: theme switch, Port · ← Hub). Multi-section apps add a LEFT COLUMN of sections
#: under it, with each section's task tabs inside the page.
LEFT_COLUMN = {"reduction", "calibration", "watchdog", "average", "analysis", "background", "reactor"}
TOP_BAR = {"analyzer", "quality", "assistant"}   # reactor moved to the left column, Oct 2026


def test_every_app_has_exactly_one_layout():
    apps = {t.parent.parent.name for t in APP_TEMPLATES}
    assert apps == LEFT_COLUMN | TOP_BAR, f"unclassified apps: {apps ^ (LEFT_COLUMN | TOP_BAR)}"


@pytest.mark.parametrize("tpl", APP_TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_every_app_has_the_same_top_bar(tpl):
    app = tpl.parent.parent.name
    s = re.sub(r"<!--.*?-->", "", tpl.read_text(encoding="utf-8"), flags=re.S)
    m = re.search(r'<(header|div)[^>]*class="[^"]*\btopbar\b[^"]*"[^>]*>', s)
    assert m, f"{app}: no top bar"
    tag, depth, end = m.group(1), 0, None
    for t in re.finditer(rf"<{tag}\b|</{tag}>", s[m.start():]):   # matching close, not the first inner one
        depth += 1 if not t.group(0).startswith("</") else -1
        if depth == 0:
            end = m.start() + t.end(); break
    bar = s[m.start(): end]
    assert "_app_mark.svg" in bar and "<h1" in bar, f"{app}: logo/name not in the top bar"
    assert 'class="theme-toggle"' in bar, f"{app}: theme switch not in the top bar"
    assert "localhost:5100" in bar and "← Hub" in bar, f"{app}: Port · Hub not in the top bar"


@pytest.mark.parametrize("tpl", APP_TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_the_left_column_holds_only_sections(tpl):
    app = tpl.parent.parent.name
    s = re.sub(r"<!--.*?-->", "", tpl.read_text(encoding="utf-8"), flags=re.S)
    if app not in LEFT_COLUMN:
        assert 'id="sidebar"' not in s, f"{app}: unexpected left column"
        return
    i = s.find('<nav id="sidebar">')
    assert i >= 0, f"{app}: left column missing"
    side = s[i: s.find("</nav>", i)]
    assert 'class="nav-btn' in side, f"{app}: no sections in the left column"
    for dup in ('id="logo"', 'id="sidebar-foot"', "← Hub"):
        assert dup not in side, f"{app}: {dup} belongs in the top bar, not the column"
    assert 'id="topbar"' not in s, f"{app}: old slim bar still present"
    assert '<body class="swaxs-shell">' in s, f"{app}: page not marked to stack bar over column"


# ── one top-row ORDER everywhere, extras in a fixed slot ─────────────────────
def _bar_children(s: str) -> list[str]:
    m = re.search(r'<(header|div)[^>]*class="[^"]*\btopbar\b[^"]*"[^>]*>', s)
    tag, depth, end = m.group(1), 0, len(s)
    for t in re.finditer(rf"<{tag}\b|</{tag}>", s[m.start():]):
        depth += 1 if not t.group(0).startswith("</") else -1
        if depth == 0:
            end = m.start() + t.start(); break
    inner = s[m.end(): end]
    kids, pos = [], 0
    for c in re.finditer(r"<(\w+)\b([^>]*)>", inner):   # top-level children only
        if c.start() < pos:
            continue
        name, attrs = c.group(1), c.group(2)
        d, j = 0, c.start()
        for t in re.finditer(rf"<{name}\b[^>]*?(/?)>|</{name}>", inner[c.start():]):
            if t.group(0).startswith("</"): d -= 1
            elif not t.group(0).endswith("/>"): d += 1
            if d == 0:
                j = c.start() + t.end(); break
        pos = j
        cls = re.search(r'class="([^"]*)"', attrs)
        kids.append((cls.group(1) if cls else "") + "#" + (re.search(r'id="([^"]*)"', attrs) or [None, ""])[1])
    return kids


@pytest.mark.parametrize("tpl", APP_TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_every_top_row_has_the_same_order(tpl):
    """logo+name │ status │ [app extras]  ……  project folder │ theme │ Port · ← Hub
    (the folder sits on the right, just before the theme switch — operator, Oct 2026)"""
    app = tpl.parent.parent.name
    s = re.sub(r"<!--.*?-->", "", tpl.read_text(encoding="utf-8"), flags=re.S)
    k = _bar_children(s)
    assert k[0].startswith("app-lockup"), f"{app}: {k}"
    tail = k[-3:]
    assert (tail[0].startswith("proj") or "#topbar-path" in tail[0]), f"{app}: folder not right of the row: {k}"
    assert "theme-toggle" in tail[1] and tail[2].startswith("topbar-sub"), f"{app}: right edge: {k}"
    middle = k[2:-3]
    assert len(middle) <= 1 and all(m.startswith("bar-extras") for m in middle), \
        f"{app}: extras must sit in ONE slot after the status: {k}"


def test_operator_field_is_only_in_the_hub():
    for tpl in TEMPLATES:
        app = tpl.parent.parent.name
        s = tpl.read_text(encoding="utf-8")
        has = 'id="hubOperator"' in s or 'id="cfg-operator"' in s or re.search(r'placeholder="name / initials"', s)
        assert bool(has) == (app == "hub"), f"{app}: operator field {'missing' if app == 'hub' else 'must live only in the hub'}"


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_headings_with_an_icon_keep_the_title_beside_it(tpl):
    """A heading that starts with a sprite icon must not use space-between: the
    icon is its own flex item, so the title drifts to the centre (or, when the
    trailing control is hidden, to the far right). Keep the title beside the
    icon and push only the trailing control right with margin-left:auto.
    (Reactor: Flow rate centred, Beamline right-aligned, Oct 2026.)"""
    s = re.sub(r"<!--.*?-->", "", tpl.read_text(encoding="utf-8"), flags=re.S)
    bad = re.findall(r'style="[^"]*justify-content:\s*space-(?:between|around|evenly)[^"]*"[^>]*>\s*<svg class="ico"', s)
    assert not bad, f"{tpl.parent.parent.name}: {len(bad)} icon heading(s) spread with space-between"
