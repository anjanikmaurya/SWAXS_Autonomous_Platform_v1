"""
tests/test_shared_layout_spec.py

reduction, average and background are the same kind of page — a column of
panels, each a heading over a form — and had three implementations of it.

reduction and average already agreed: `.card` / `.card-hd`, byte for byte.
background was the outlier in three ways, two of which were bugs rather than
taste:

  * **an 18px base.** The --fs-* scale is defined against 16px
    (--fs-sm:.8125rem is meant to be 13px), so every token rendered ~11%
    larger in background than the identical token in reduction. A component
    moved between the apps silently changed size.

  * **`.row` was never defined.** background's markup used it in four places
    to put two fields side by side. With no rule they stacked. Nothing in the
    source looked wrong; the page was just longer than it needed to be.

  * **`.section` / `.section-title`** — 14px padding and a small grey heading
    against reduction's 20px and a 15px cardinal one.

One block now, identical in all three, plus a panel that is ~14px shorter than
reduction's original so a whole form fits on one screen without shrinking any
text.

What this file does NOT assert: that the three apps agree on everything.
average sizes text in px literals, background in --fs-* tokens; average's
forms are label-left (.fg), background's label-above (.field). Those are real
differences that need markup changes, not a CSS block, and claiming otherwise
with a test nobody can satisfy is worse than saying so here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_APPS = ("reduction", "average", "background")

_BLOCK = re.compile(
    r"/\* ── Shared layout spec.*?\n\.two-col, \.row \{.*?\n\}", re.S)


def _tpl(app: str) -> str:
    return (_ROOT / app / "templates" / "index.html").read_text()


def _block(app: str) -> str:
    m = _BLOCK.search(_tpl(app))
    assert m, f"{app} has no shared layout block"
    return m.group(0)


def test_all_three_carry_the_identical_block():
    blocks = {a: _block(a) for a in _APPS}
    first = blocks["reduction"]
    for app, b in blocks.items():
        assert b == first, f"{app}'s copy of the shared layout block has drifted"


@pytest.mark.parametrize("app", _APPS)
def test_the_block_is_the_last_word_on_those_selectors(app):
    """It is appended at the end of <style> so it overrides each app's older
    rules. A later rule on the same selector would silently undo it."""
    css = re.search(r"<style>(.*?)</style>", _tpl(app), re.S).group(1)
    block = _block(app)
    # Everything strictly AFTER the block ends — its own rules are not
    # "overrides of itself", and counting them was how this test first
    # miscounted.
    after = css[css.index(block) + len(block):]
    later = re.findall(
        r"(?m)^\s*\.(?:card|card-hd|section|section-title|two-col|row)\b[^{]*\{",
        after)
    assert not later, (
        f"{app}: {later} appear after the shared block and will override it")


@pytest.mark.parametrize("app", _APPS)
def test_every_app_uses_the_same_base_font_size(app):
    """The one setting that makes every other token lie. background ran 18px,
    so --fs-sm was 14.6px there and 13px everywhere else."""
    css = _tpl(app)
    m = re.search(r"html[^{]*\{[^}]*font-size:\s*(\d+)px", css)
    base = int(m.group(1)) if m else 16          # unset means the browser's 16
    assert base == 16, (
        f"{app} sets a {base}px base; the --fs-* scale is defined against 16px, "
        f"so every token renders {base / 16:.0%} of its documented size")


def test_row_actually_has_a_rule_now():
    """The regression that cost the most scrolling and was invisible in the
    markup: background used class="row" four times with no CSS behind it."""
    html = _tpl("background")
    assert 'class="row"' in html, "the .row wrappers vanished"
    assert re.search(r"(?m)^\.two-col, \.row \{", _block("background")), \
        ".row still has no rule — its fields will stack"


@pytest.mark.parametrize("app", _APPS)
def test_panels_are_tighter_than_the_original(app):
    """The operator asked to fill in a form without scrolling. This is the part
    that is free: less padding, no smaller text."""
    block = _block(app)
    pad = re.search(r"padding:(\d+)px (\d+)px", block)
    assert pad and int(pad.group(1)) <= 16, \
        f"{app}: panel padding grew back to {pad.group(1) if pad else '?'}px"


@pytest.mark.parametrize("app", _APPS)
def test_no_text_was_shrunk_to_buy_the_space(app):
    """Density from padding, not from making anything harder to read. The
    heading stays at reduction's 15px."""
    assert "font-size:15px" in _block(app), \
        f"{app}: the panel heading is no longer 15px"


def test_the_two_column_grid_collapses_rather_than_squeezing():
    """A 240px minimum means a narrow window gets one usable column instead of
    two unusable ones."""
    block = _block("average")
    assert "auto-fit" in block and "minmax(240px" in block
