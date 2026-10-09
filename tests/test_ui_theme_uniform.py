"""One light/dark system for the whole platform (October 2026).

* one palette, in assets/icons/swaxs-tokens.css, that outranks any leftover app
  palette — change a colour there and every app follows;
* one starting rule, run in <head> before first paint by _theme_boot.html:
  the saved choice (shared key `swaxs-theme`) wins, else the OS preference;
* a theme toggle in every app, the hub included, all on that one key;
* readable in both themes: the contrast floors below are WCAG AA.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKENS = (ROOT / "assets" / "icons" / "swaxs-tokens.css").read_text(encoding="utf-8")
TEMPLATES = sorted(ROOT.glob("*/templates/index.html"))


def _palette(selector: str) -> dict:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", TOKENS)
    assert m, f"shared palette block missing: {selector}"
    return dict(re.findall(r"--([\w-]+)\s*:\s*([^;]+);", m.group(1)))


LIGHT = _palette('html:root:not([data-theme="dark"])')
DARK = _palette('html:root[data-theme="dark"]')


def _lum(h: str) -> float:
    h = h.strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_both_themes_define_the_same_tokens():
    assert set(LIGHT) == set(DARK), set(LIGHT) ^ set(DARK)


TEXT_PAIRS = [("txt", "bg"), ("txt", "surface"), ("txt", "surface2"),
              ("muted", "surface"), ("muted", "surface2"), ("faint", "surface"),
              ("accent-text", "surface"), ("accent-text", "surface2"),
              ("ok-text", "ok-lt"), ("warn-text", "warn-lt"), ("err-text", "err-lt"),
              ("info-text", "info-lt"), ("violet-text", "violet-lt"),
              ("ok-text", "surface"), ("warn-text", "surface"), ("err-text", "surface"),
              ("info-text", "surface")]


@pytest.mark.parametrize("theme,pal", [("light", LIGHT), ("dark", DARK)])
@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_is_readable_in_both_themes(theme, pal, fg, bg):
    r = _ratio(pal[fg], pal[bg])
    assert r >= 4.5, f"{theme}: --{fg} on --{bg} is {r:.2f}:1 (AA text needs 4.5)"


@pytest.mark.parametrize("theme,pal", [("light", LIGHT), ("dark", DARK)])
def test_white_labels_on_accent_buttons_are_readable(theme, pal):
    assert _ratio("#ffffff", pal["accent"]) >= 4.5


@pytest.mark.parametrize("tpl", TEMPLATES, ids=lambda p: p.parent.parent.name)
def test_every_page_boots_its_theme_in_the_head_and_has_a_toggle(tpl):
    app = tpl.parent.parent.name
    s = tpl.read_text(encoding="utf-8")
    head = s[: s.find("</head>")]
    assert '{% include "_theme_boot.html" %}' in head, f"{app}: theme boot not in <head>"
    assert not re.search(r"<html[^>]*data-theme=", s), f"{app}: hardcoded theme on <html>"
    assert 'class="theme-toggle"' in s, f"{app}: no theme toggle"
    body = re.sub(r"<!--.*?-->|//[^\n]*", "", s, flags=re.S)
    keys = set(re.findall(r"localStorage\.\w+\(\s*['\"]([\w-]*theme[\w-]*)['\"]", body))
    assert keys <= {"swaxs-theme"}, f"{app}: theme stored under {keys}, not the shared key"


def test_the_boot_rule_is_saved_choice_then_os():
    boot = (ROOT / "hub" / "templates" / "_theme_boot.html").read_text()
    assert "localStorage.getItem('swaxs-theme')" in boot
    assert "prefers-color-scheme: dark" in boot
