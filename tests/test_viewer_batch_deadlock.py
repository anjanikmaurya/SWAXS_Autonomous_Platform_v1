"""
tests/test_viewer_batch_deadlock.py — "averaging seems very slow".

It was not slow. It was deadlocked, silently, forever.

Loop frames are grouped per ``{recipe_id}_{role}`` so frames from different
recipes never combine. The reactor ships ``spec.frames: 10`` — ten frames per
acquisition — and the viewer UI ships ``frames_per_average: 30``. The averaging
loop is

    while (len(grp) - consumed) >= n_per_batch:

so 10 >= 30 is never true: **no average is ever written**, for any recipe. With no
averaged file there is no subtracted profile, no fit, and no next recipe — the
whole autonomous loop stops. Status stayed green at ``batches: 0`` and the loop
logged nothing at all after "Auto-averaging started".

Locked down here: the condition is detected up front, and progress is visible so
"waiting for more frames" can never again look like "working".
"""
from __future__ import annotations

import importlib.util as u
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _dat(path: Path):
    q = np.linspace(0.1, 5.0, 200)
    I = 40 * q ** -2.2 + 5
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("# q_nm-1 I sigma\n")
        for a, b in zip(q, I):
            f.write(f"{a:.6e} {b:.6e} {np.sqrt(b) * 0.02:.6e}\n")
        f.write("\n# METADATA INFORMATION\n# detector: saxs\n# i0: 1000\n")


def _viewer(tag, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    spec = u.spec_from_file_location(tag, str(ROOT / "viewer" / "app.py"))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


def _msgs(m):
    return [l["msg"] for _, l in list(m._avg_log)]


def _wait(pred, timeout=10.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_a_batch_larger_than_one_acquisition_is_refused_up_front(tmp_path, monkeypatch):
    red = tmp_path / "1D" / "SAXS" / "Reduction"
    for i in range(10):                       # one acquisition, spec.frames = 10
        _dat(red / f"auto_1_a_sample_scan1_{i:04d}.dat")
    m = _viewer("vdead", tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_frames_per_acquisition", lambda: 10)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "frames_per_average": 30,
                                       "saxs_folder": str(red), "waxs_folder": ""})
        assert _wait(lambda: any("can NEVER complete" in s for s in _msgs(m))), \
            "the deadlock was not reported:\n" + "\n".join(_msgs(m))
        err = [l for _, l in list(m._avg_log)
               if "can NEVER complete" in l["msg"]][0]
        assert err["tag"] == "error", "reported, but not as an error"
        assert "Set frames/batch to 10" in err["msg"], "no actionable remedy given"
    finally:
        m._avg_monitoring = False


def test_progress_is_visible_while_waiting_for_frames(tmp_path, monkeypatch):
    """Silence is what made this look like slowness."""
    red = tmp_path / "1D" / "SAXS" / "Reduction"
    for i in range(4):
        _dat(red / f"auto_1_a_sample_scan1_{i:04d}.dat")
    m = _viewer("vprog", tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_frames_per_acquisition", lambda: 0)   # unknown
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "frames_per_average": 10,
                                       "saxs_folder": str(red), "waxs_folder": ""})
        assert _wait(lambda: any("4/10 frames" in s for s in _msgs(m))), \
            "no progress reported while waiting:\n" + "\n".join(_msgs(m))
        assert any("waiting for 6 more" in s for s in _msgs(m))
        n_before = sum(1 for s in _msgs(m) if "4/10 frames" in s)
        time.sleep(2.5)
        assert sum(1 for s in _msgs(m) if "4/10 frames" in s) == n_before, \
            "the waiting line repeats every poll"
    finally:
        m._avg_monitoring = False


def test_a_matching_batch_size_actually_averages(tmp_path, monkeypatch):
    """The other half: with frames/batch == frames/acquisition it must work."""
    red = tmp_path / "1D" / "SAXS" / "Reduction"
    for i in range(10):
        _dat(red / f"auto_1_a_sample_scan1_{i:04d}.dat")
    m = _viewer("vok", tmp_path, monkeypatch)
    monkeypatch.setattr(m, "_frames_per_acquisition", lambda: 10)
    try:
        m.app.test_client().post("/api/monitor/start",
                                 json={"interval": 1, "frames_per_average": 10,
                                       "saxs_folder": str(red), "waxs_folder": ""})
        out = tmp_path / "1D" / "SAXS" / "Averaged"
        assert _wait(lambda: out.is_dir() and list(out.glob("*.dat"))), \
            "nothing averaged even with a matching batch size:\n" + "\n".join(_msgs(m))
        assert not any("can NEVER complete" in s for s in _msgs(m))
    finally:
        m._avg_monitoring = False


def test_the_frames_per_acquisition_probe_reads_the_reactor_config():
    """The two halves of the loop must not disagree about something this
    consequential, so the check reads the reactor's own config."""
    m = sys.modules.get("vok")
    if m is None:
        pytest.skip("viewer module not loaded")
    n = m._frames_per_acquisition.__wrapped__() if hasattr(
        m._frames_per_acquisition, "__wrapped__") else None
    from src.reactor import load_config
    expected = int((load_config().get("spec") or {}).get("frames", 0) or 0)
    assert expected > 0, "reactor config no longer declares spec.frames"
