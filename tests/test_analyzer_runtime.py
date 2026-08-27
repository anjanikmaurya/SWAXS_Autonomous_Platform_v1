"""
tests/test_analyzer_runtime.py — do the pages actually run?

Every other UI test in this repo greps the template. That catches a missing
button; it cannot catch `psView` being called before it is defined, a null
element access, or a typo'd variable — the failures that leave an operator
staring at a page that renders and then does nothing.

This shells out to tests/ui/analyzer_ui_check.js, which loads the real template
in jsdom with the endpoints and the SSE stream stubbed and drives the UI. Skipped
unless node + jsdom are available, so it never blocks a beamline install:

    cd tests/ui && npm install jsdom
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "ui" / "analyzer_ui_check.js"
HUB_HARNESS = ROOT / "tests" / "ui" / "hub_ui_check.js"


def _node():
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed")
    return exe


def _jsdom_path(node) -> str | None:
    """Where jsdom lives. `require` resolves from the harness FILE, not the cwd, so
    a checkout without tests/ui/node_modules needs NODE_PATH pointing at it."""
    import os
    for base in (HARNESS.parent, ROOT, Path(os.environ.get("SWAXS_JSDOM", "")),
                 Path("/tmp/uicheck")):
        if not base or not str(base) or not (base / "node_modules").is_dir():
            continue
        r = subprocess.run([node, "-e", "require.resolve('jsdom')"],
                           cwd=base, capture_output=True,
                           env={**os.environ, "NODE_PATH": str(base / "node_modules")})
        if r.returncode == 0:
            return str(base / "node_modules")
    return None


def test_the_page_runs_without_errors():
    import os
    node = _node()
    np_ = _jsdom_path(node)
    if not np_:
        pytest.skip("jsdom not installed — run: cd tests/ui && npm install jsdom")
    r = subprocess.run([node, str(HARNESS)], cwd=str(HARNESS.parent),
                       env={**os.environ, "NODE_PATH": np_},
                       capture_output=True, text=True, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, "the analyzer page failed at runtime:\n" + out
    assert "no runtime errors" in out, out
    # the harness prints "N checks passed"; a silent pass would mean it did nothing
    n = int(out.split(" checks passed")[0].strip().split()[-1])
    assert n >= 45, f"only {n} UI assertions ran — the harness was gutted:\n{out}"


def test_the_hub_page_reports_crashes_correctly():
    """The hub template is rendered by Jinja, so jsdom cannot read it directly —
    render it through the app first, then drive the real page.

    This is the DOM half of the "⚠ CRASHED (exit null)" fix: the server no longer
    calls a Stop a crash, and the card no longer prints a missing exit code as the
    word "null".
    """
    import importlib.util as iu
    import tempfile

    node = _node()
    np_ = _jsdom_path(node)
    if not np_:
        pytest.skip("jsdom not installed — run: cd tests/ui && npm install jsdom")

    spec = iu.spec_from_file_location("hub_render", str(ROOT / "hub" / "app.py"))
    m = iu.module_from_spec(spec)
    sys.modules["hub_render"] = m
    spec.loader.exec_module(m)
    html = m.app.test_client().get("/").get_data(as_text=True)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(html)
        rendered = fh.name

    r = subprocess.run([node, str(HUB_HARNESS)], cwd=str(HUB_HARNESS.parent),
                       env={**os.environ, "NODE_PATH": np_, "HUB_HTML": rendered},
                       capture_output=True, text=True, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    assert r.returncode == 0, "the hub page failed at runtime:\n" + out
    assert "no runtime errors" in out, out
    n = int(out.split(" checks passed")[0].strip().split()[-1])
    assert n >= 18, f"only {n} hub UI assertions ran:\n{out}"
