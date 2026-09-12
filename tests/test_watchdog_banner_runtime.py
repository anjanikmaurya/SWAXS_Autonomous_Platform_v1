"""
tests/test_watchdog_banner_runtime.py

Runs tests/ui/test_stage_banner.cjs under pytest so the banner's precedence is
covered by the normal suite rather than only by hand.

Unlike tests/test_analyzer_runtime.py this needs **no jsdom** — the checker
extracts the banner functions from the template and drives them against a
small DOM stub, so plain `node` is enough. Skipped if node is absent.

What it protects: `pipeline.current_stage` is the last stage to EMIT an event,
so after a fit completed the banner read FITTING for the whole 20-minute flush
of the next condition. The reactor's phase is what is happening now, and must
outrank it.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CHECKER = _ROOT / "tests" / "ui" / "test_stage_banner.cjs"


def test_the_stage_banner_precedence_holds():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed — the banner checker needs it")
    assert _CHECKER.is_file(), f"missing checker: {_CHECKER}"

    proc = subprocess.run([node, str(_CHECKER)], cwd=str(_ROOT),
                          capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"stage-banner checks failed:\n{out}"
    assert "checks passed" in out, out
