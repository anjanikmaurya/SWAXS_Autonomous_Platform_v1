"""Sample resolver for the assistant's keyword-based analysis tools.

Added 2026-09-24 (pre-demo): the operator wanted to be able to ask for the
LATEST run and to have Guinier/p(r)/model reach the SUBTRACTED folder — the
old `_find_averaged_sample` matched averaged files only and refused when
several matched. `_find_sample` prefers the subtracted stage, picks the most
recent file (by manifest ``created_at``) when several match, and the old
wrapper stays averaged-only for frame-level QC.
"""
import importlib

import pytest

m = importlib.import_module("src.ai.assistant")
Asst = m.SWAXSAssistant


@pytest.fixture
def asst(monkeypatch):
    fake = {"files": {
        "Run36_r014_sample_SAXS_avg.dat": {
            "stage": "averaged", "detector": "SAXS",
            "path": "/p/avg_old.dat", "created_at": "2026-09-20T10:00:00"},
        "Run36_r014_sample_SAXS_sub.dat": {
            "stage": "subtracted", "detector": "SAXS",
            "path": "/p/sub_old.dat", "created_at": "2026-09-20T11:00:00"},
        "Run37_r020_sample_SAXS_sub.dat": {
            "stage": "subtracted", "detector": "SAXS",
            "path": "/p/sub_new.dat", "created_at": "2026-09-23T09:00:00"},
        "Run40_r001_sample_WAXS_sub.dat": {
            "stage": "subtracted", "detector": "WAXS",
            "path": "/p/waxs_sub.dat", "created_at": "2026-09-23T09:05:00"},
    }}
    monkeypatch.setattr(m, "_load_manifest_cached", lambda *a, **k: fake)
    return object.__new__(Asst)


def test_subtracted_is_preferred_over_averaged(asst):
    entry, _mf, _matches, stage = asst._find_sample("/proj", "Run36_r014", "SAXS")
    assert stage == "subtracted"
    assert entry["path"] == "/p/sub_old.dat"


def test_broad_keyword_picks_most_recent(asst):
    entry, _mf, matches, stage = asst._find_sample("/proj", "sample", "SAXS")
    assert stage == "subtracted"
    assert len(matches) == 2
    assert entry["path"] == "/p/sub_new.dat"          # newest by created_at


def test_no_keyword_returns_latest(asst):
    entry, _mf, _matches, stage = asst._find_sample("/proj", "", "SAXS")
    assert entry["path"] == "/p/sub_new.dat"


def test_detector_is_respected(asst):
    entry, _mf, _matches, stage = asst._find_sample("/proj", "", "WAXS")
    assert entry["path"] == "/p/waxs_sub.dat"


def test_falls_through_to_averaged_when_no_subtracted(asst):
    # keyword only present on the averaged file
    entry, _mf, _matches, stage = asst._find_sample(
        "/proj", "avg", "SAXS", stages=("subtracted", "averaged"))
    assert stage == "averaged"
    assert entry["path"] == "/p/avg_old.dat"


def test_backcompat_wrapper_stays_averaged_only(asst):
    entry, _mf, _matches = asst._find_averaged_sample("/proj", "Run36", "SAXS")
    assert entry["path"] == "/p/avg_old.dat"          # never the subtracted one


def test_no_match_returns_none(asst):
    entry, _mf, matches, stage = asst._find_sample("/proj", "nonesuch", "SAXS")
    assert entry is None and matches == [] and stage is None


def test_subtracted_warning_fires_on_averaged_fallback(asst):
    # analysis must be on subtracted data; averaged fallback is flagged loudly
    assert Asst._subtracted_warning("subtracted", "Run4_r001", "p(r)") == ""
    w = Asst._subtracted_warning("averaged", "Run4_r001", "model fit")
    assert "not background-subtracted" in w.lower()
    assert "run background subtraction" in w.lower()


def test_pick_note_only_when_ambiguous(asst):
    assert asst._pick_note([1], {"path": "/p/x.dat"}, "saxs", "subtracted", "k") == ""
    note = asst._pick_note([1, 2], {"path": "/p/sub_new.dat"},
                           "saxs", "subtracted", "sample")
    assert "most recent" in note and "sub_new.dat" in note
