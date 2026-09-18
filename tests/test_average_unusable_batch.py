"""
tests/test_average_unusable_batch.py

A mock run (Run20) averaged r001–r005 fine and then, at r006, said this for
both lanes:

    ⚠ Run20_r006_bkg [saxs] batch 1: no usable frames — skipped
    ⚠ Run20_r006_sample [saxs] batch 1: no usable frames — skipped

which reads as "the average app is broken". It was not: all twenty .dat files
really were unusable. The reason — `<3 valid points` for every frame — existed
only in logs/average.log, written by the module logger, while the operator was
looking at the app's own live log. Three separate faults made a data problem
look like an app problem:

  1. `_average_group` returns a bare None for four different situations and
     the caller printed one sentence for all of them;
  2. the batch is consumed either way (correctly — the frames are static on
     disk, so retrying is pointless), but nothing downstream was told, so the
     background app waited on a gate that could never fill and the watchdog
     could only say "go read the average app's log";
  3. the validity mask ran AFTER the common q grid was built, so one bad frame
     in a batch of ten took the other nine with it.

These tests cover all three.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.plot_reduction import _average_group, diagnose_unusable   # noqa: E402

Q = np.geomspace(0.01, 1.0, 200)


def frame(name, *, I=None, sigma=None, q=None):
    q = Q if q is None else q
    return {"filename": name, "q": q,
            "I": np.full_like(q, 2.0) if I is None else I,
            "sigma": np.full_like(q, 0.1) if sigma is None else sigma,
            "metadata": {"i0": 1.0}}


def good(n, tag="g"):
    return [frame(f"{tag}{i}.dat") for i in range(n)]


# ── 1. the reason is recoverable ────────────────────────────────────────────
def test_all_zero_intensity_is_named_as_such():
    """What Run20_r006 actually was: every frame integrates to I = 0, so the
    `I > 0` term of the validity mask is false at every q."""
    batch = [frame(f"z{i}.dat", I=np.zeros_like(Q)) for i in range(10)]
    assert _average_group(batch) is None
    why = diagnose_unusable(batch)
    assert "all 10 frames are unusable" in why
    assert "I ≤ 0" in why


def test_a_nan_sigma_column_is_not_reported_as_bad_intensity():
    """Perfectly good I, unusable sigma — the operator would go looking in
    completely the wrong place if this said the intensity was bad."""
    batch = [frame(f"n{i}.dat", sigma=np.full_like(Q, np.nan)) for i in range(10)]
    assert _average_group(batch) is None
    why = diagnose_unusable(batch)
    assert "sigma" in why and "I ≤ 0" not in why


def test_no_overlapping_q_range_says_so_and_gives_the_numbers():
    batch = [frame("a.dat", q=np.geomspace(0.01, 0.05, 50),
                   I=np.full(50, 2.0), sigma=np.full(50, 0.1)),
             frame("b.dat", q=np.geomspace(0.5, 1.0, 50),
                   I=np.full(50, 2.0), sigma=np.full(50, 0.1))]
    assert _average_group(batch) is None
    why = diagnose_unusable(batch)
    assert "no overlapping q range" in why
    assert "0.5" in why and "0.05" in why


def test_an_empty_dat_is_called_empty_not_dark():
    why = diagnose_unusable([frame("e.dat", q=np.array([]),
                                   I=np.array([]), sigma=np.array([]))])
    assert "no data rows" in why
    assert "all 1 frames" not in why, "plural slipped through"


def test_diagnose_never_raises_on_junk():
    """It runs on a path that has already failed once; it must not be the
    thing that takes the monitor thread down."""
    for junk in ([], [{"filename": "x"}], [{"filename": "y", "q": None,
                                            "I": None, "sigma": None}]):
        assert isinstance(diagnose_unusable(junk), str)


# ── 2. one bad frame must not discard the good ones ─────────────────────────
def test_nine_good_frames_survive_one_empty_frame():
    """The regression that mattered most. `_common_q_grid` was computed over
    every frame, so an empty q array raised ValueError and the whole batch —
    nine perfectly good frames — was dropped and consumed."""
    result = _average_group(good(9) + [frame("bad.dat", q=np.array([]),
                                             I=np.array([]), sigma=np.array([]))])
    assert result is not None, "one unusable frame discarded nine usable ones"
    assert result[4] == 9, "only the contributing frames may count"


def test_a_dark_frame_does_not_drag_the_average_down():
    """A frame of zeros is excluded, not averaged in as zero — otherwise I is
    biased low and sigma too small (the denominator bug _average_group's
    comments already warn about)."""
    result = _average_group(good(9) + [frame("dark.dat", I=np.zeros_like(Q))])
    assert result is not None
    q_out, I_out, sig_out, _meta, n_used = result
    assert n_used == 9
    assert np.allclose(I_out, 2.0, rtol=1e-6), "a dark frame leaked into the mean"


def test_the_grid_is_built_from_contributing_frames_only():
    """A junk frame with a narrow q range must not clip the average to it."""
    junk = frame("junk.dat", q=np.geomspace(0.2, 0.3, 50),
                 I=np.zeros(50), sigma=np.full(50, np.nan))
    q_out = _average_group(good(9) + [junk])[0]
    assert q_out.min() == pytest.approx(Q.min(), rel=1e-6)
    assert q_out.max() == pytest.approx(Q.max(), rel=1e-6)


def test_a_genuinely_narrow_good_frame_still_sets_the_grid():
    """The flip side: a frame that CAN contribute gets its vote, even though
    the overlap it forces is small. Excluding it would be silently averaging a
    different set of frames than the batch says."""
    narrow = frame("narrow.dat", q=np.geomspace(0.2, 0.3, 50),
                   I=np.full(50, 2.0), sigma=np.full(50, 0.1))
    q_out, _I, _s, _m, n_used = _average_group(good(9) + [narrow])
    assert n_used == 10
    assert q_out.min() == pytest.approx(0.2, rel=1e-6)
    assert q_out.max() == pytest.approx(0.3, rel=1e-6)


# ── 3. the loop is told, so nothing waits forever ───────────────────────────
def test_the_average_app_emits_a_reason_when_it_drops_a_batch():
    src = (_ROOT / "average" / "app.py").read_text()
    assert "diagnose_unusable(batch)" in src, "the drop is still unexplained"
    assert "emit_average_skipped" in src, (
        "nothing downstream learns the gate can never fill")


def test_average_skipped_is_its_own_event_type():
    """Reusing file.skipped would have folded these into the watchdog's
    Pattern G count, which means 'reduction gave up on a frame'."""
    from src.events import EventBusClient
    published = {}

    class _Spy(EventBusClient):
        def publish(self, etype, data):          # type: ignore[override]
            published.update(type=etype, data=data)
            return True

    _Spy("test").emit_average_skipped(
        keyword="Run20_r006_bkg", detector="saxs", batch=1, n_files=10,
        reason="all 10 frames are unusable (10× I ≤ 0 at every q)",
        files=["a.dat"])
    assert published["type"] == "average.skipped"
    assert published["data"]["keyword"] == "Run20_r006_bkg"
    assert "I ≤ 0" in published["data"]["reason"]


def test_the_watchdog_reports_the_reason_instead_of_pointing_at_a_log():
    from src.watchdog.diagnose import diagnose_stall
    loop = {
        "recipe_id": "Run20_r006",
        "reduce": {"state": "idle", "skipped": 0},
        "average": {"recipe_id": "Run20_r006", "lane": "background",
                    "have": 10, "expected": 10, "state": "stalled",
                    "dropped": {"rid": "Run20_r006", "n_files": 10,
                                "batch": 1, "keyword": "Run20_r006_bkg",
                                "reason": "all 10 frames are unusable "
                                          "(10× I ≤ 0 at every q)"}},
    }
    title, text, alert = diagnose_stall(
        stage="average", overdue_s=900.0, probes={}, loop=loop,
        ai_fallback_enabled=False)
    assert alert is True
    assert "Pattern H" in title
    assert "I ≤ 0" in text
    assert "will not be retried" in text, \
        "the operator has to be told the frames are gone, not merely late"
