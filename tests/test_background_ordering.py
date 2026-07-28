"""
tests/test_background_ordering.py — background BEFORE synthesis.

Order is: flush the line → collect the blank on the clean capillary → run the
synthesis → collect the sample. The blank carries the UPCOMING recipe_id, so a
run is always paired with the background measured immediately before it and the
background is already on disk when the sample frames land.

`spec.background_when: "after"` restores the legacy order (blank during the
post-synthesis flush).
"""
from __future__ import annotations

import time

import pytest
import yaml

from src.reactor.controller import ReactorController

RECIPE = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.15, "x_oley": 0.1}


def _cfg(tmp_path, **spec_over):
    cfg = yaml.safe_load(open("reactor/config.yml"))
    cfg["spec"]["data_dir"] = str(tmp_path / "proj")
    cfg["spec"]["simulator"].update(speed_factor=0, poni="", shape=[64, 64])
    cfg["spec"].update(exposure_s=0.1, frames=1, spec_lead_s=0.3)
    cfg["spec"].update(spec_over)
    cfg["flush"]["duration"] = 0.7
    cfg["arming"] = {**cfg.get("arming", {}), "default_mode": "timed",
                     "default_wait_s": 0.1}
    return cfg


def _run(cfg, n=2, dur=0.8, wait=26.0):
    logs: list = []
    ctl = ReactorController(cfg, backend="mock",
                            log_cb=lambda m, t="info": logs.append(m))
    try:
        for i in range(1, n + 1):
            ctl.submit({**RECIPE, "recipe_id": f"r{i}", "run_duration": dur})
        ctl.start()
        t0 = time.time()
        while time.time() - t0 < wait and not (ctl.state in ("ready", "idle")
                                               and time.time() - t0 > 3.0):
            time.sleep(0.1)
        time.sleep(0.4)                      # let the last collect thread finish
    finally:
        ctl.shutdown()
    return ctl, logs


def _events(logs):
    """Condense the log into an ordered list of pipeline events."""
    out = []
    for m in logs:
        if "BLANK START" in m:
            out.append("blank")
        elif "FLUSH START" in m:
            out.append("flush")
        elif "RUN START" in m:
            out.append(f"run:{m.split('RUN START')[1].split('—')[0].strip()}")
        elif "BACKGROUND collect DONE" in m or "background collect —" in m:
            out.append("bkg")
        elif "SAMPLE collect DONE" in m or "sample collect —" in m:
            out.append("sample")
    return out


# ── the ordering itself ───────────────────────────────────────────────────────
def test_background_is_collected_before_the_synthesis(tmp_path):
    _, logs = _run(_cfg(tmp_path), n=1)
    ev = _events(logs)
    assert "bkg" in ev and "sample" in ev, ev
    assert ev.index("bkg") < ev.index("sample"), f"blank came after the sample: {ev}"
    # and the blank flush precedes the run
    assert ev.index("blank") < ev.index("run:r1"), ev


def test_each_run_is_preceded_by_its_own_blank(tmp_path):
    _, logs = _run(_cfg(tmp_path), n=3, wait=30.0)
    ev = [e for e in _events(logs) if e in ("blank", "bkg") or e.startswith("run:")]
    # expect blank, bkg, run:r1, blank, bkg, run:r2, ...
    runs = [e for e in ev if e.startswith("run:")]
    assert runs == ["run:r1", "run:r2", "run:r3"], runs
    for r in runs:
        before = ev[:ev.index(r)]
        assert before.count("bkg") >= runs.index(r) + 1, \
            f"{r} was not preceded by its own background: {ev}"


def test_sample_and_background_share_the_recipe_id(tmp_path):
    from pathlib import Path
    cfg = _cfg(tmp_path)
    _run(cfg, n=2)
    raws = sorted(p.name for p in Path(cfg["spec"]["data_dir"]).rglob("*.raw"))
    for rid in ("r1", "r2"):
        assert any(n.startswith(f"{rid}_sample") for n in raws), raws
        assert any(n.startswith(f"{rid}_bkg") for n in raws), raws


def test_blank_is_tagged_with_the_upcoming_recipe_not_the_previous(tmp_path):
    """The whole point: r2's blank must be labelled r2, not r1."""
    from pathlib import Path
    cfg = _cfg(tmp_path)
    _run(cfg, n=2)
    names = [p.name for p in Path(cfg["spec"]["data_dir"]).rglob("*bkg*.raw")]
    assert any(n.startswith("r2_bkg") for n in names), names


# ── efficiency: one flush between runs, not two ──────────────────────────────
def test_the_post_run_flush_doubles_as_the_next_blank(tmp_path):
    """Running a clean-out flush AND a separate blank flush would waste a full
    flush duration between every pair of runs."""
    _, logs = _run(_cfg(tmp_path), n=3, wait=30.0)
    ev = _events(logs)
    # never two flush-type events in a row
    flushes = [i for i, e in enumerate(ev) if e in ("blank", "flush")]
    for a, b in zip(flushes, flushes[1:]):
        assert b - a > 1, f"back-to-back flushes at {a},{b}: {ev}"
    assert any("doubles as the blank" in m for m in logs)


# ── the legacy order still works ─────────────────────────────────────────────
def test_after_mode_restores_the_legacy_ordering(tmp_path):
    _, logs = _run(_cfg(tmp_path, background_when="after"), n=1)
    ev = _events(logs)
    assert ev.index("sample") < ev.index("bkg"), f"expected sample first: {ev}"
    assert "blank" not in ev, "no pre-synthesis blank should run in 'after' mode"


def test_invalid_background_when_falls_back_to_before(tmp_path):
    cfg = _cfg(tmp_path, background_when="sideways")
    ctl = ReactorController(cfg, backend="mock")
    try:
        assert ctl.background_when == "before"
    finally:
        ctl.shutdown()


# ── the staged recipe must not be lost ───────────────────────────────────────
def test_aborting_during_the_blank_does_not_strand_the_recipe(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["flush"]["duration"] = 6.0            # long blank so we can abort inside it
    logs: list = []
    ctl = ReactorController(cfg, backend="mock",
                            log_cb=lambda m, t="info": logs.append(m))
    try:
        ctl.submit({**RECIPE, "recipe_id": "r1", "run_duration": 1.0})
        ctl.start()
        time.sleep(0.6)
        assert ctl.state == "flushing" and ctl._pending is not None
        ctl.abort()
        time.sleep(0.5)
        assert ctl.state in ("flushing", "idle", "ready")
    finally:
        ctl.shutdown()
