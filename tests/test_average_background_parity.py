"""
tests/test_average_background_parity.py

average (5103) and background (5104) do the same job — watch a folder, process
what lands in rolling batches, log it, let the operator start and stop that
from one control. Their auto-monitor controls had drifted into looking like
two different products:

    average                          background
    ─────────────────────────────    ──────────────────────────────
    btn-ok (GREEN) Start             btn-primary (CARDINAL) Start
    .btn-grp → content-width         .actions → flex:1, stretched
    pill font-size:14px              pill font-size:var(--fs-sm)
    .fnote count                     .hint count
    log max-height 260px             log max-height 300px
    log ui-monospace literal         log var(--mono)
    error lines var(--err)           error lines var(--accent)

That last one was not cosmetic. background defines --green/--yellow/--red but
NOT --ok/--warn/--err, so every var(--err) written there resolved to nothing
and the log painted ERROR lines in the brand accent — the only colour that did
resolve. Both now use the --*-text variants, which exist in both apps and in
both themes.

The two apps still differ underneath in a way this file does NOT try to fix:
average sizes text in px and inherits a 16px base; background sizes in --fs-*
tokens on an 18px base. The same token therefore renders ~11% bigger in
background. That is a platform-wide split (docs/DESIGN_SYSTEM.md §0 — five
apps each way) and converting either app wholesale is a separate job. The
shared block below sidesteps it by using absolute px, so it looks identical in
both regardless of the base.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_AVG = (_ROOT / "average" / "templates" / "index.html").read_text()
_BKG = (_ROOT / "background" / "templates" / "index.html").read_text()

#: The shared control block, matched from its opening comment to its last rule.
_BLOCK = re.compile(
    r"/\* ── Auto-monitor control — SHARED.*?\n\.auto-log \{.*?\n\}", re.S)


def _shared_block(html: str) -> str:
    m = _BLOCK.search(html)
    assert m, "the shared auto-monitor CSS block is missing"
    return m.group(0)


def test_the_shared_control_css_is_identical_in_both_apps():
    """Byte-identical, not merely similar. "Similar" is how it drifted."""
    a, b = _shared_block(_AVG), _shared_block(_BKG)
    assert a == b, "the shared auto-monitor block has diverged between the apps"


def test_the_shared_block_does_not_use_font_size_tokens():
    """The two apps run different base font sizes (16px vs 18px), so a --fs-*
    token renders ~11% larger in background. px is what makes this block look
    the same in both."""
    # Rules only. The comment above them necessarily NAMES the tokens it is
    # telling you not to use, and a scanner that cannot tell code from prose
    # reports the explanation as the defect (third time in this session).
    rules = re.sub(r"/\*.*?\*/", "", _shared_block(_AVG), flags=re.S)
    assert "--fs-" not in rules, (
        "the shared block uses a --fs-* token; it will render at two different "
        "sizes because the apps have different base font sizes")


@pytest.mark.parametrize("cls", ["auto-actions", "auto-state", "auto-count",
                                 "auto-log"])
def test_both_apps_use_the_shared_classes(cls):
    for name, html in (("average", _AVG), ("background", _BKG)):
        assert f'class="{cls}"' in html, f"{name} does not use .{cls}"


def test_both_start_buttons_are_the_same_colour():
    """A green Start in one app and a cardinal Start in the other, for the same
    action, is the difference you notice first."""
    for name, html, btn_id in (("average", _AVG, "aa-start"),
                               ("background", _BKG, "auto-start")):
        m = re.search(rf'<button[^>]*id="{btn_id}"[^>]*>', html) \
            or re.search(rf'<button[^>]*class="([^"]*)"[^>]*id="{btn_id}"', html)
        assert m, f"{name}: no start button found"
        tag = m.group(0)
        assert "btn-primary" in tag, (
            f"{name}'s start button is not btn-primary: {tag}")
        assert "btn-ok" not in tag


def test_neither_start_row_stretches_its_buttons():
    """`.actions` sets flex:1 on its buttons, so Start and Stop filled the row
    in background while the identical pair sat at content width in average."""
    for name, html in (("average", _AVG), ("background", _BKG)):
        row = re.search(r'<div class="auto-actions">(.*?)</div>', html, re.S)
        assert row, f"{name}: no .auto-actions row"
        assert 'class="actions"' not in row.group(0)


def test_the_log_colour_map_is_identical():
    maps = [re.search(r"const colors=\{[^}]*\}", h).group(0)
            for h in (_AVG, _BKG)]
    assert maps[0] == maps[1], f"log colour maps differ: {maps}"


def test_the_log_never_paints_an_error_in_the_brand_colour():
    """background used var(--accent) for errors because var(--err) did not
    resolve there — an error the same colour as every heading and button."""
    for name, html in (("average", _AVG), ("background", _BKG)):
        colours = re.search(r"const colors=\{[^}]*\}", html).group(0)
        assert "--accent" not in colours, \
            f"{name} paints a log level in the brand accent: {colours}"


# ── the token names both apps rely on now resolve in both ───────────────────
@pytest.mark.parametrize("token", ["--ok", "--warn", "--err",
                                   "--ok-text", "--warn-text", "--err-text",
                                   "--txt", "--txt-strong", "--mono",
                                   "--surface2", "--border", "--radius"])
def test_both_apps_define_every_token_the_shared_block_needs(token):
    for name, html in (("average", _AVG), ("background", _BKG)):
        assert re.search(rf"{re.escape(token)}\s*:", html), \
            f"{name} does not define {token}; declarations using it are dropped"


@pytest.mark.parametrize("token", ["--txt", "--txt-strong"])
def test_the_text_aliases_are_overridden_in_dark_mode_too(token):
    """Defining an alias in :root only is worse than not defining it: the
    near-black light value then applies on a #1f2126 dark surface."""
    for name, html in (("average", _AVG), ("background", _BKG)):
        dark = html.split('[data-theme="dark"]')[1].split("}")[0]
        assert f"{token}:" in dark, \
            f"{name}: {token} has no dark-mode value — invisible text in dark"


# ── the form spec (added after the operator asked average to follow
#    background: label-above, one shared 15px token, whole app) ─────────────
_FORM = re.compile(r"/\* ── Shared form spec.*?\n\.fnote, \.hint \{[^}]*\}", re.S)


def _form_block(html: str) -> str:
    m = _FORM.search(html)
    assert m, "the shared form spec is missing"
    return m.group(0)


def test_the_form_spec_is_identical_in_both_apps():
    assert _form_block(_AVG) == _form_block(_BKG), \
        "the shared form spec has diverged between the apps"


def test_average_now_stacks_its_labels_above_the_input():
    """average used .fg — a 185px label column with the label to the LEFT.
    Converted by turning that grid into a single column, so the existing
    <label><div.cell> pairs stack exactly like background's .field. No markup
    was rewritten; if this rule goes, all 17 containers revert at once."""
    block = _form_block(_AVG)
    assert ".field, .fg { display:flex; flex-direction:column" in block
    assert "text-align:left" in block, "labels are still right-aligned"


def test_both_apps_size_form_text_from_one_token():
    """--fs-form exists because the --fs-* scale jumps 14px -> 16px with
    nothing between, and 15px is what average's forms were already using."""
    block = _form_block(_AVG)
    assert "--fs-form:.9375rem" in block
    assert block.count("var(--fs-form)") >= 2, \
        "the token is declared but the labels/inputs do not use it"


def test_focusing_an_input_does_not_turn_it_white_in_dark_mode():
    """average had `input:focus{background:#fff}` with no dark override, so
    focusing any field in dark mode put near-white text on a white box —
    invisible while typing. Omitting the property would not have undone it;
    the shared rule has to restate the background."""
    block = _form_block(_AVG)
    focus = block[block.index("input:focus, select:focus {"):]
    assert "background:var(--surface2)" in focus, \
        "the focus rule no longer overrides the hard-coded white background"
    # and it must come after the old rule to win
    assert _AVG.index("Shared form spec") > _AVG.index("background:#fff")


def test_the_two_hint_classes_render_the_same():
    """average called it .fnote at 14px, background .hint at 13px."""
    block = _form_block(_AVG)
    assert ".fnote, .hint {" in block


# ── the Monitor panel ───────────────────────────────────────────────────────
def test_both_start_controls_live_in_a_titled_monitor_panel():
    """background's Start/Stop sat bare at the bottom of the auto pane while
    average's lived in a card headed "▶ Monitor", so the same control looked
    like two different things. Both are panelled now."""
    for name, html, hd in (("average", _AVG, 'class="card-hd">▶ Monitor<'),
                           ("background", _BKG, 'class="section-title">▶ Monitor<')):
        assert hd in html, f"{name} has no ▶ Monitor panel heading"


def test_the_monitor_panel_contains_the_controls_and_the_log():
    """A heading with the controls outside it would look right and group
    nothing."""
    for name, html, opener in (
            ("average", _AVG, '<div class="card">\n        <div class="card-hd">▶ Monitor</div>'),
            ("background", _BKG, '<div class="section">\n              <div class="section-title">▶ Monitor</div>')):
        i = html.index(opener)
        # the panel runs to the next panel opener or the pane's end
        tail = html[i:i + 1400]
        assert 'class="auto-actions"' in tail, f"{name}: controls are outside the panel"
        assert 'class="auto-log"' in tail, f"{name}: the log is outside the panel"


def test_scale_and_schedule_share_a_row_in_background():
    """Two small panels stacked full-width wasted a screen's worth of height
    between them."""
    seg = _BKG[_BKG.index('id="auto-setup"'):]
    row = seg.index('<div class="row">\n            <div class="section">\n'
                    '              <div class="section-title">⚙ Scale</div>')
    close = seg.index('</div><!-- /.row -->', row)
    block = seg[row:close]
    assert "⚙ Scale" in block and "⏱ Schedule" in block, \
        "Scale and Schedule are not inside the same .row"
    n_panels = block.count('<div class="section">')
    assert n_panels == 2, f"{n_panels} panels in the row, expected 2"
