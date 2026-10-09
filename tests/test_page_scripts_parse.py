"""Every app's page JavaScript must at least PARSE.

One unbalanced parenthesis in an inline <script> kills the whole block: the page
renders but nothing on it works (no live updates, dead buttons). The Python tests
never execute page JS, so this slipped through once (reactor, October 2026) and
was only caught by a browser smoke test. This renders each app through Flask,
exactly as served, and runs `node --check` on every inline script block.

Skipped when node is not installed, so it never blocks a beamline install.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

NODE = shutil.which("node")
APPS = ["hub", "calibration", "reduction", "average", "background", "quality",
        "analysis", "analyzer", "reactor", "assistant", "watchdog"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("app_id", APPS)
def test_every_inline_script_parses(app_id, monkeypatch):
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    monkeypatch.setenv("SWAXS_SLACK_WEBHOOK_URL", "")     # never notify from a test
    mod = importlib.import_module(f"{app_id}.app")
    mod.app.config["TESTING"] = True
    html = mod.app.test_client().get("/", follow_redirects=True).get_data(as_text=True)
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert blocks, f"{app_id}: no inline scripts found"
    bad = []
    with tempfile.TemporaryDirectory() as td:
        for i, js in enumerate(blocks):
            f = Path(td) / f"b{i}.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
            if r.returncode:
                bad.append(f"block {i}: " + " ".join(r.stderr.strip().splitlines()[:4]))
    assert not bad, f"{app_id}: page script does not parse:\n" + "\n".join(bad)
