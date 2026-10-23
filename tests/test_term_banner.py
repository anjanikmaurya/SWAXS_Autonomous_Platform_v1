"""The hub's terminal app list (src/term_banner.py): real icons as inline images
where the terminal supports them, a coloured mark in the app's accent elsewhere."""
import base64
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import term_banner  # noqa: E402
from src.term_banner import MARK, accent_colours, app_lines, image_protocol  # noqa: E402

APPS = yaml.safe_load((ROOT / "apps.yml").read_text())["apps"]


class _TTY:
    def isatty(self):
        return True


def test_every_app_has_its_own_colour_and_a_terminal_icon():
    acc = accent_colours()
    colours = [acc.get(a["accent"]) for a in APPS]
    assert all(colours) and len(set(colours)) == len(APPS)
    for a in APPS:
        png = ROOT / "assets" / "icons" / "terminal" / f"{a['icon_id']}.png"
        assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", png


def test_squares_use_the_accent_and_align():
    lines = app_lines(APPS, colour=True, protocol=None)
    assert all("\033[38;2;" in l and MARK in l for l in lines)
    assert len({l.index(":51") - l.index("\033[0m") for l in lines}) == 1


def test_plain_when_no_colour():
    lines = app_lines(APPS, colour=False, protocol=None)
    assert not any("\033" in l for l in lines)
    assert len({l.index(":51") for l in lines}) == 1


def test_iterm_lines_carry_the_real_icon():
    lines = app_lines(APPS, protocol="iterm")
    for a, l in zip(APPS, lines):
        assert "\033]1337;File=inline=1;" in l and "width=2;height=1" in l
        b64 = l.split("preserveAspectRatio=1:", 1)[1].split("\a", 1)[0]
        png = (ROOT / "assets/icons/terminal" / f"{a['icon_id']}.png").read_bytes()
        assert base64.b64decode(b64) == png
        assert l.endswith(f"{a['name']}".ljust(max(len(x['name']) for x in APPS)) + f"  :{a['port']}")


def test_kitty_lines_use_the_graphics_protocol():
    l = app_lines(APPS[:1], protocol="kitty")[0]
    assert l.count("\033_G") >= 1 and "a=T,f=100,c=2,r=1" in l and l.count("\033\\") >= 1


def test_protocol_detection(monkeypatch):
    monkeypatch.setattr(term_banner, "use_colour", lambda stream=None: True)
    assert image_protocol({"TERM_PROGRAM": "iTerm.app"}) == "iterm"
    assert image_protocol({"TERM_PROGRAM": "WezTerm"}) == "iterm"
    assert image_protocol({"TERM_PROGRAM": "vscode"}) == "iterm"
    assert image_protocol({"TERM": "xterm-kitty"}) == "kitty"
    assert image_protocol({"TERM_PROGRAM": "ghostty"}) == "kitty"
    assert image_protocol({"TERM_PROGRAM": "Apple_Terminal"}) is None
    assert image_protocol({"TERM_PROGRAM": "iTerm.app", "TMUX": "x"}) is None
    assert image_protocol({"TERM_PROGRAM": "iTerm.app", "SWAXS_TERM_ICONS": "squares"}) is None
    assert image_protocol({"TERM_PROGRAM": "Apple_Terminal", "SWAXS_TERM_ICONS": "images"}) == "iterm"


def test_no_images_or_colour_into_a_log(monkeypatch):
    import io
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert image_protocol(stream=io.StringIO()) is None
    monkeypatch.setenv("NO_COLOR", "1")
    assert term_banner.use_colour(_TTY()) is False
