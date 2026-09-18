"""
tests/test_blank_frame_guards.py

Run20 lost condition r006 — both lanes, twenty frames — and the only symptom
was this, two stages downstream:

    ⚠ Run20_r006_bkg [saxs] batch 1: no usable frames — skipped

The cause was a single unguarded number. `set_spec_settings` accepted
`exposure_s` from the reactor UI with no lower bound, while `frames` sitting
directly beside it had always been clamped with `max(1, ...)`. With
exposure_s = 0:

    simulate_frame   img = intensity_map × exposure_s × flux  →  all zeros
    write_raw        size is right, dtype is right, refuses only size == 0
    reduction        i0 and bstop are healthy, so every guard passes; pyFAI
                     integrates zeros into a structurally perfect .dat
    average          the validity mask rejects every frame, batch consumed

Four components each did something defensible and the condition vanished. So
there are four guards now, and a test for each — the point being that any ONE
of them would have caught it, and the cheapest place to catch it is first.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── guard 1: the setting itself ─────────────────────────────────────────────
def _controller():
    from src.reactor.controller import ReactorController
    from src.reactor.config import load_config
    return ReactorController(load_config())


def test_a_zero_exposure_is_refused_and_the_working_value_kept():
    c = _controller()
    c.set_spec_settings({"exposure_s": "10"})
    assert c._spec_exposure == 10.0

    c.set_spec_settings({"exposure_s": "0"})
    assert c._spec_exposure == 10.0, \
        "exposure_s=0 was accepted — every later frame collects nothing"


def test_a_negative_exposure_is_refused_too():
    c = _controller()
    c.set_spec_settings({"exposure_s": "10"})
    c.set_spec_settings({"exposure_s": "-5"})
    assert c._spec_exposure == 10.0


def test_a_blank_exposure_field_still_means_leave_it_alone():
    """The UI posts every field on every save, so an untouched empty box must
    not be read as a change — that behaviour predates this fix and must stay."""
    c = _controller()
    c.set_spec_settings({"exposure_s": "7.5"})
    c.set_spec_settings({"exposure_s": "  "})
    assert c._spec_exposure == 7.5


def test_a_valid_exposure_still_applies():
    c = _controller()
    c.set_spec_settings({"exposure_s": "0.25"})
    assert c._spec_exposure == 0.25, "the guard must not reject small exposures"


# ── guard 2: the collector ──────────────────────────────────────────────────
def test_the_collector_refuses_to_run_with_no_exposure(tmp_path):
    from src.simulator.collector import SimulatedCollector
    sim = SimulatedCollector()
    with pytest.raises(ValueError, match="positive exposure"):
        sim.collect(prefix="r006_bkg", role="bkg", data_dir=str(tmp_path),
                    exposure=0.0, frames=2)


# ── guard 3: the writer ─────────────────────────────────────────────────────
def test_the_writer_refuses_an_all_zero_frame(tmp_path):
    """The size check never caught this: a blank frame is exactly the right
    size. simulate_frame adds a solvent background to every frame, including
    particle-free ones, so zero counts always means something upstream was
    zero."""
    from src.simulator.writer import write_raw
    with pytest.raises(ValueError, match="BLANK"):
        write_raw(tmp_path / "blank.raw", np.zeros((64, 64), dtype=np.int32))
    assert not (tmp_path / "blank.raw").exists()
    assert not (tmp_path / "blank.raw.part").exists(), "left a stub behind"


def test_the_writer_still_accepts_a_nearly_empty_frame(tmp_path):
    """One count is signal. The guard is "no counts at all", not "not much"."""
    from src.simulator.writer import write_raw
    img = np.zeros((64, 64), dtype=np.int32)
    img[0, 0] = 1
    assert write_raw(tmp_path / "faint.raw", img).is_file()


# ── guard 4: reduction ──────────────────────────────────────────────────────
# This is the one that also covers REAL beam, where a closed shutter or a
# mis-set mask produces the same blank frame with no simulator involved.
def test_reduction_refuses_to_publish_a_profile_with_no_signal(tmp_path):
    from src.reduction.core import _assert_has_signal
    logged = []
    with pytest.raises(ValueError, match="no counts|positive intensity"):
        _assert_has_signal(np.zeros(1000), tmp_path / "r006_0000.raw", "SAXS",
                           lambda m, lvl="": logged.append(m))
    assert logged and "blank" in logged[0].lower(), \
        "the operator log must name the cause, not just the symptom"


def test_a_real_profile_passes_the_signal_check(tmp_path):
    from src.reduction.core import _assert_has_signal
    I = np.geomspace(1e-4, 1e-2, 1000)[::-1]
    _assert_has_signal(I, tmp_path / "good.raw", "SAXS", lambda *a, **k: None)


def test_a_profile_that_is_mostly_zero_but_has_signal_is_kept(tmp_path):
    """High-q bins legitimately integrate to zero on a short exposure. Only a
    profile with NOTHING is refused — the threshold matches the averaging
    core's, so a .dat reduction accepts is one the average app can use."""
    from src.reduction.core import _assert_has_signal, _MIN_POSITIVE_POINTS
    I = np.zeros(1000)
    I[:_MIN_POSITIVE_POINTS] = 1e-3
    _assert_has_signal(I, tmp_path / "sparse.raw", "SAXS", lambda *a, **k: None)

    I_short = np.zeros(1000)
    I_short[: _MIN_POSITIVE_POINTS - 1] = 1e-3
    with pytest.raises(ValueError):
        _assert_has_signal(I_short, tmp_path / "too_sparse.raw", "SAXS",
                           lambda *a, **k: None)


def test_the_signal_check_never_fails_a_frame_on_its_own_error(tmp_path):
    """It runs inside the publish path of every single frame. A bug in the
    check itself must not be able to stop a beamtime."""
    from src.reduction.core import _assert_has_signal
    _assert_has_signal(object(), tmp_path / "weird.raw", "SAXS",
                       lambda *a, **k: None)


# ── the guards agree with each other ────────────────────────────────────────
def test_reduction_and_averaging_use_the_same_threshold():
    """If reduction published profiles the average app then rejected, we would
    be back to losing whole batches two stages from the cause."""
    from src.reduction.core import _MIN_POSITIVE_POINTS
    import inspect
    from src.plot_reduction import _average_group
    src = inspect.getsource(_average_group)
    assert f"valid.sum() < {_MIN_POSITIVE_POINTS}" in src, (
        "the averaging core's minimum moved away from reduction's — "
        "reduction would publish .dat files averaging cannot use")
