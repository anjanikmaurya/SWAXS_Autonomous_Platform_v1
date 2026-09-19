"""
tests/test_theme_and_naming.py

Cross-app consistency: does the platform look and read like ONE product?

Three things this caught, all invisible until you put the apps side by side:

  * **The theme key had forked.** Eight apps store the light/dark choice under
    `swaxs-theme`; Auto Watch used `swaxs_theme` with an UNDERSCORE. So its
    theme lived in a different localStorage entry — switching to light in
    Reduction left Auto Watch dark, and switching there changed nothing
    anywhere else. Its own comment claimed the toggles agreed. They could not.

  * **A tab title that named a different app.** reduction's <title> was "SWAXS
    Pipeline — SLAC/SSRL" while the hub card called it "Reduction &
    Correction". With ten apps open, the tab strip is how you find one.

  * **Five different title formats** across ten apps ("X — SLAC/SSRL",
    "SWAXS · X", "X — SWAXS", "SWAXS X", bare "X"). Tabs truncate from the
    right, so the app's own name has to come first.

Deliberately NOT asserted: that every app uses the same type scale. They do
not — docs/DESIGN_SYSTEM.md section 0 records exactly how far each has
diverged, and pretending otherwise with a failing test nobody can fix is
worse than the honest table.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_APPS = yaml.safe_load((_ROOT / "apps.yml").read_text())["apps"]

#: The key every themed app must use. Hyphen, not underscore.
_THEME_KEY = "swaxs-theme"


def _templates():
    for a in _APPS:
        p = _ROOT / a["id"] / "templates" / "index.html"
        if p.is_file():
            yield a, p.read_text()


def _title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _unescape(s: str) -> str:
    return s.replace("&amp;", "&").replace("&nbsp;", " ")


_BLOCK = re.compile(r"/\*.*?\*/|<!--.*?-->", re.S)


def _strip_comments(text: str) -> str:
    """Comments out, before scanning for keys.

    The comment explaining why the old key was WRONG naturally quotes it, so a
    scanner that cannot tell code from prose reports the explanation as the
    defect and pushes the next person to delete the explanation. Only
    line-leading `//` is stripped, because the templates are full of http://.
    """
    out = _BLOCK.sub("", text)
    return "\n".join(l for l in out.splitlines()
                      if not l.strip().startswith(("//", "#:")))


# ── the tab strip ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("app_id", [a["id"] for a in _APPS])
def test_the_tab_title_names_the_app_the_hub_card_names(app_id):
    a = next(x for x in _APPS if x["id"] == app_id)
    p = _ROOT / app_id / "templates" / "index.html"
    if not p.is_file():
        pytest.skip(f"{app_id} has no template")
    title = _unescape(_title(p.read_text()))
    assert title, f"{app_id} has no <title>"
    assert _unescape(a["name"]) in title, (
        f"{app_id}: the tab says {title!r} but the hub card says "
        f"{a['name']!r} — with ten apps open, the tab strip is how you find one")


@pytest.mark.parametrize("app_id", [a["id"] for a in _APPS])
def test_the_app_name_comes_first_in_the_title(app_id):
    """Browser tabs truncate from the right. A title starting with the shared
    "SWAXS" prefix truncates to ten identical tabs reading "SWAXS…"."""
    a = next(x for x in _APPS if x["id"] == app_id)
    p = _ROOT / app_id / "templates" / "index.html"
    if not p.is_file():
        pytest.skip(f"{app_id} has no template")
    title = _unescape(_title(p.read_text()))
    assert title.startswith(_unescape(a["name"])), \
        f"{app_id}: {title!r} does not lead with the app name"


def test_every_title_uses_one_format():
    formats = {_unescape(_title(html)).split(" — ")[-1]
               for _a, html in _templates() if _title(html)}
    assert formats == {"SWAXS"}, (
        f"titles end with {sorted(formats)} — five different patterns is how "
        f"a ten-app platform stops looking like one product")


# ── the theme carries across apps ───────────────────────────────────────────
def test_every_themed_app_shares_one_localstorage_key():
    wrong = []
    for a, html in _templates():
        keys = set(re.findall(r"swaxs[-_]theme", _strip_comments(html)))
        if not keys:
            continue                 # calibration/hub are single-theme by design
        if keys != {_THEME_KEY}:
            wrong.append(f"{a['id']}: {sorted(keys)}")
    assert not wrong, (
        f"these apps store the theme under a different key, so the toggle does "
        f"not carry across the platform: {wrong}")


def test_the_apps_that_do_not_theme_are_the_known_two():
    """If a new app ships without a toggle, that is a decision worth noticing
    rather than discovering when a user opens it next to nine dark panels."""
    untoggled = {a["id"] for a, html in _templates()
                 if "swaxs-theme" not in _strip_comments(html)}
    # The hub has no apps.yml entry of its own (it is the launcher), so it is
    # never yielded here; calibration is the one registered single-theme app.
    assert untoggled == {"calibration"}, (
        f"apps with no theme toggle: {sorted(untoggled)} — "
        f"docs/DESIGN_SYSTEM.md lists only hub and calibration")


# ── icons ───────────────────────────────────────────────────────────────────
def test_every_app_has_an_icon_and_a_colour():
    missing = [a["id"] for a in _APPS
               if not (a.get("icon") or a.get("icon_image")) or not a.get("color")]
    assert not missing, f"apps.yml entries with no icon or colour: {missing}"


def test_the_design_system_table_covers_every_app():
    """The conformance table is the only record of how far each app has
    diverged. An app missing from it is an app nobody has checked — Auto Watch
    was absent from it for its whole life."""
    doc = (_ROOT / "docs" / "DESIGN_SYSTEM.md").read_text()
    table = doc.split("## 0. Per-app conformance")[1].split("\n## ")[0]
    missing = [a["id"] for a in _APPS if f"| {a['id']} |" not in table]
    assert not missing, f"not in the conformance table: {missing}"
