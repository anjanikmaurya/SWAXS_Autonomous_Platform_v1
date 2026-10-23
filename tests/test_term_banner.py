"""The hub's terminal app list: a coloured mark per app accent (src/term_banner.py)."""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.term_banner import MARK, accent_colours, app_lines  # noqa: E402

APPS = yaml.safe_load((ROOT / "apps.yml").read_text())["apps"]


def test_every_app_has_its_own_colour():
    acc = accent_colours()
    colours = [acc.get(a["accent"]) for a in APPS]
    assert all(colours), [a["id"] for a, c in zip(APPS, colours) if not c]
    assert len(set(colours)) == len(APPS)


def test_coloured_lines_use_the_accent_and_align():
    lines = app_lines(APPS, colour=True)
    assert len(lines) == len(APPS)
    assert all("\033[38;2;" in l and MARK in l for l in lines)
    cols = {l.index(":51") - l.index("\033[0m") for l in lines}
    assert len(cols) == 1                                   # ports in one column


def test_plain_when_no_colour_and_no_emoji():
    lines = app_lines(APPS, colour=False)
    assert not any("\033" in l for l in lines)
    assert all(ord(ch) < 0x1F000 for l in lines for ch in l)
    assert len({l.index(":51") for l in lines}) == 1


def test_no_color_env_and_redirected_output_turn_colour_off(monkeypatch):
    import io
    from src import term_banner
    monkeypatch.setenv("NO_COLOR", "1")
    assert term_banner.use_colour() is False
    monkeypatch.delenv("NO_COLOR")
    assert term_banner.use_colour(io.StringIO()) is False
