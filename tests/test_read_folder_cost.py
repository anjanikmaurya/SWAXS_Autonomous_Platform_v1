"""
tests/test_read_folder_cost.py — defect N7.

`average`'s monitor called read_folder() on the WHOLE Reduction folder every
poll, re-reading and re-parsing every .dat each time. At 10 000 files the scan
cost more than the averaging and the monitor fell progressively behind the
acquisition it was tracking, which is why its interval had to be 10 s.

Two mechanisms, tested here on cost (parses performed), not on wall time:

  1. a (mtime_ns, size) parse cache keyed by PATH, so a rewritten file
     REPLACES its entry instead of leaving a dead one behind forever
  2. `skip_names`, so frames the caller has already consumed are never even
     stat'd — making a poll proportional to NEW frames, not to the beamtime

The results must be byte-identical to the uncached path: this is a cost fix,
not a behaviour change.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import src.plot_reduction as pr                                  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    pr.clear_read_cache()
    yield
    pr.clear_read_cache()


def _dat(folder: Path, name: str, n: int = 12, scale: float = 1.0) -> Path:
    """A minimal but real reduced .dat: q / I / sigma columns + a metadata footer."""
    q = np.linspace(0.1, 2.0, n)
    I = scale * np.exp(-q)
    sig = 0.01 * I
    body = "\n".join(f"{a:.6f}\t{b:.6e}\t{c:.6e}" for a, b, c in zip(q, I, sig))
    p = folder / name
    p.write_text(
        "# q_nm-1\tI\tsigma\n" + body
        + "\n# METADATA INFORMATION (YML FORMAT)\n# i0: 1000.0\n# bstop: 900.0\n",
        encoding="utf-8")
    return p


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "Reduction"
    d.mkdir()
    for i in range(1, 13):
        _dat(d, f"Run9_r001_sample_{i:04d}.dat")
    return d


def _count_parses(monkeypatch) -> dict:
    """Wrap the parser so we can count how many files are actually read."""
    calls = {"n": 0}
    real = pr.read_dat_data_metadata

    def counted(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(pr, "read_dat_data_metadata", counted)
    return calls


# ── 1. the parse cache ──────────────────────────────────────────────────────
def test_a_second_poll_over_an_unchanged_folder_parses_nothing(folder, monkeypatch):
    """The N7 fix, stated as cost: polling an idle folder must be free."""
    calls = _count_parses(monkeypatch)

    first = pr.read_folder(folder)
    assert calls["n"] == 12, "first poll reads every file"

    calls["n"] = 0
    second = pr.read_folder(folder)
    assert calls["n"] == 0, "a second poll must not re-parse a single file"
    assert [d["filename"] for d in second] == [d["filename"] for d in first]


def test_only_the_new_frames_are_parsed_on_the_next_poll(folder, monkeypatch):
    pr.read_folder(folder)
    calls = _count_parses(monkeypatch)
    _dat(folder, "Run9_r001_sample_0013.dat")
    _dat(folder, "Run9_r001_sample_0014.dat")

    out = pr.read_folder(folder)

    assert calls["n"] == 2, f"only the 2 new frames should be parsed, got {calls['n']}"
    assert len(out) == 14


def test_a_rewritten_file_is_re_parsed(folder, monkeypatch):
    pr.read_folder(folder)
    target = folder / "Run9_r001_sample_0001.dat"
    _dat(folder, target.name, scale=5.0)          # same name, new content
    os.utime(target, (os.stat(target).st_mtime + 10,) * 2)

    calls = _count_parses(monkeypatch)
    out = pr.read_folder(folder)

    assert calls["n"] == 1, "a changed file must not be served from the cache"
    entry = next(d for d in out if d["filename"] == target.name)
    assert float(entry["I"][0]) == pytest.approx(5.0 * np.exp(-0.1), rel=1e-3)


def test_a_rewritten_file_does_not_leave_a_dead_cache_entry(folder):
    """Keying the whole signature meant every rewrite ADDED an entry and the
    old one was never reachable again — the cache grew with rewrites until the
    bound was hit by dead weight."""
    pr.read_folder(folder)
    before = pr.read_cache_stats()["entries"]
    target = folder / "Run9_r001_sample_0001.dat"
    for k in range(3):
        _dat(folder, target.name, scale=2.0 + k)
        os.utime(target, (os.stat(target).st_mtime + 10 * (k + 1),) * 2)
        pr.read_folder(folder)
    assert pr.read_cache_stats()["entries"] == before, \
        "rewrites must replace an entry, never accumulate"


def test_the_cache_is_bounded_and_evicts_least_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_READ_CACHE_MAX", 5)
    d = tmp_path / "R"
    d.mkdir()
    for i in range(9):
        _dat(d, f"f_{i:04d}.dat")

    pr.read_folder(d)

    stats = pr.read_cache_stats()
    assert stats["entries"] <= 5, \
        f"the bound is a MEMORY bound (~24 kB/entry) and must hold, got {stats}"


def test_cached_results_are_identical_to_an_uncached_read(folder):
    cached = pr.read_folder(folder)
    pr.clear_read_cache()
    fresh = pr.read_folder(folder)
    assert len(cached) == len(fresh)
    for a, b in zip(cached, fresh):
        assert a["filename"] == b["filename"]
        assert a["keyword"] == b["keyword"]
        assert a["scan_idx"] == b["scan_idx"]
        np.testing.assert_allclose(a["q"], b["q"])
        np.testing.assert_allclose(a["I"], b["I"])


# ── 2. skip_names ───────────────────────────────────────────────────────────
def test_skip_names_files_are_never_parsed(folder, monkeypatch):
    calls = _count_parses(monkeypatch)
    consumed = {f"Run9_r001_sample_{i:04d}.dat" for i in range(1, 11)}

    out = pr.read_folder(folder, skip_names=consumed)

    assert calls["n"] == 2, \
        "a poll must cost the NEW frames only, not the consumed back-catalogue"
    assert {d["filename"] for d in out} == {
        "Run9_r001_sample_0011.dat", "Run9_r001_sample_0012.dat"}


def test_skip_names_does_not_cache_the_skipped_files(folder):
    consumed = {f"Run9_r001_sample_{i:04d}.dat" for i in range(1, 11)}
    pr.read_folder(folder, skip_names=consumed)
    assert pr.read_cache_stats()["entries"] == 2, \
        "skipped frames must not occupy the cache either"


def test_skipping_everything_is_a_cheap_no_op(folder, monkeypatch):
    calls = _count_parses(monkeypatch)
    everything = {p.name for p in folder.glob("*.dat")}
    assert pr.read_folder(folder, skip_names=everything) == []
    assert calls["n"] == 0


def test_skip_names_none_is_the_previous_behaviour(folder):
    assert len(pr.read_folder(folder, skip_names=None)) == 12
    pr.clear_read_cache()
    assert len(pr.read_folder(folder)) == 12


def test_skip_names_composes_with_keywords(folder):
    _dat(folder, "Run9_r001_background_0001.dat")
    out = pr.read_folder(folder, keywords=["_sample_"],
                         skip_names={"Run9_r001_sample_0001.dat"})
    names = {d["filename"] for d in out}
    assert "Run9_r001_background_0001.dat" not in names, "keyword filter still applies"
    assert "Run9_r001_sample_0001.dat" not in names, "skip still applies"
    assert len(names) == 11


# ── 3. the average monitor hands its consumed set through ───────────────────
def test_the_average_app_skips_what_it_has_already_averaged(monkeypatch):
    """_consumed_for(det) is what makes the monitor's poll proportional to new
    frames. Keyed per detector: a keyword is only unique within one."""
    os.environ.setdefault("SWAXS_NO_WATCH", "1")
    os.environ.setdefault("SWAXS_NO_BUS", "1")
    import average.app as av

    monkeypatch.setattr(av, "_avg_batch_state", {
        ("saxs", "Run9_r001_sample"): {"files": {"a.dat", "b.dat"}, "n": 1},
        ("saxs", "Run9_r002_sample"): {"files": {"c.dat"}, "n": 1},
        ("waxs", "Run9_r001_sample"): {"files": {"w.dat"}, "n": 1},
    })

    assert av._consumed_for("saxs") == {"a.dat", "b.dat", "c.dat"}
    assert av._consumed_for("waxs") == {"w.dat"}
    assert av._consumed_for("nope") == set()
