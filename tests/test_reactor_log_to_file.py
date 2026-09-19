"""
tests/test_reactor_log_to_file.py

logs/reactor.log contained nothing but werkzeug HTTP access lines.

The reactor app never configured logging, so the only logger with a handler
was werkzeug's own; everything else fell through to Python's last-resort
handler, which is WARNING-level. Two things were lost:

  * The 2D simulator's acquisition lines. src/beamline/driver.py already
    passes SimulatedCollector a `log=lambda m: logger.info(...)` callback, and
    those lines carry the prefix, frame count, exposure, the TRUE R/PDI being
    generated, and — at the end — the peak counts and non-zero fraction of what
    was written. Every one was discarded at INFO. Run20's twenty blank frames
    needed a purpose-built diagnostic tool to explain; that last line alone
    would have said "peak 0 counts, 0% of pixels non-zero".

  * The operator log — collect START/DONE, arming, faults, E-stop — which
    lived only in a 500-entry in-memory deque served over SSE. It existed only
    while a browser was watching and nothing survived the night, which is
    precisely when an autonomous run is unattended.

Both now reach stderr, which the hub captures into logs/reactor.log.
"""
from __future__ import annotations

import importlib.util as u
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")
os.environ.setdefault("SWAXS_NO_BUS", "1")


@pytest.fixture(scope="module")
def rx():
    spec = u.spec_from_file_location("rx_logfile", _ROOT / "reactor" / "app.py")
    mod = u.module_from_spec(spec)
    sys.modules["rx_logfile"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── the operator log reaches the file ───────────────────────────────────────
def test_the_operator_log_is_written_not_only_streamed(rx, caplog):
    with caplog.at_level(logging.INFO, logger="reactor"):
        rx._emit("📷 2D SAMPLE collect START — 10 frame(s) × 10s", "ok")
    assert any("collect START" in r.message for r in caplog.records), \
        "the operator log still exists only in the in-memory SSE buffer"


@pytest.mark.parametrize("tag,level", [("ok", logging.INFO),
                                       ("info", logging.INFO),
                                       ("warn", logging.WARNING),
                                       ("error", logging.ERROR)])
def test_the_tag_becomes_the_log_level(rx, caplog, tag, level):
    """Grepping an overnight log for trouble has to find it. A fault recorded
    at INFO is a fault nobody greps up."""
    with caplog.at_level(logging.INFO, logger="reactor"):
        rx._emit(f"message tagged {tag}", tag)
    rec = [r for r in caplog.records if f"tagged {tag}" in r.message]
    assert rec and rec[-1].levelno == level


def test_emitting_still_feeds_the_sse_buffer(rx):
    """The file is an addition, not a replacement — the UI reads the deque."""
    before = len(rx._log)
    rx._emit("still streamed", "info")
    assert len(rx._log) == before + 1
    assert rx._log[-1][1]["msg"] == "still streamed"


def test_a_logging_failure_cannot_stall_the_controller(rx, monkeypatch):
    """_emit is called from the controller thread, including on the E-stop
    path. A broken handler must not propagate into it."""
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rx.logger, "log", boom)
    rx._emit("🛑 E-STOP", "error")          # must not raise
    assert rx._log[-1][1]["msg"] == "🛑 E-STOP", "the SSE buffer must still get it"


# ── the simulator's acquisition lines reach the file ────────────────────────
def test_the_simulator_announces_each_acquisition_at_info(rx, caplog):
    """The line naming the TRUE R/PDI, and the one reporting what actually
    landed on disk, are the two that answer "what did this condition collect?"
    """
    from src.simulator import SimulatedCollector
    log = logging.getLogger("src.beamline.driver")
    sim = SimulatedCollector(log=lambda m: log.info("%s", m))

    with tempfile.TemporaryDirectory() as tmp:
        with caplog.at_level(logging.INFO, logger="src.beamline.driver"):
            sim.collect(prefix="Run21_r001_sample", role="sample",
                        data_dir=tmp, exposure=0.01, frames=1)

    text = "\n".join(r.message for r in caplog.records)
    assert "sample acquisition 'Run21_r001_sample'" in text
    assert "TRUE R=" in text and "PDI=" in text
    # The line that would have identified the blank frames immediately.
    assert "non-zero" in text and "peak" in text


def test_our_packages_are_at_info_but_third_party_noise_is_not(rx):
    """basicConfig(level=INFO) would have put pyFAI, matplotlib and urllib3
    chatter into the same file and buried the lines that matter."""
    for name in ("src.beamline", "src.simulator", "src.reactor"):
        assert logging.getLogger(name).getEffectiveLevel() <= logging.INFO, \
            f"{name} is above INFO — its messages will not be recorded"
    assert logging.getLogger().getEffectiveLevel() >= logging.WARNING, \
        "the root logger was raised to INFO; the log will be mostly noise"
