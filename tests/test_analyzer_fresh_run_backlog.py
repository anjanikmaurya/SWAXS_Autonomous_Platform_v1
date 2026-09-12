"""
tests/test_analyzer_fresh_run_backlog.py

The bug, reported live from a fresh Target Run: starting a new run re-fit the
whole back-catalogue (163 historical profiles in the UI's "Analysed profiles"
table), and because the folder watcher fits serially in one thread, the new
run's own frames sat behind that backlog — the pipeline stalled for minutes
before the first live fit appeared.

`_seed_handled_at_boot()` already existed to prevent exactly this, but it only
ran at import and in `set_project`. Three other paths cleared `_handled`
outright and never reseeded it — `/api/campaign/abort` (which is what an
operator does right before starting a fresh run), and both branches of
`/api/folder` (watched folder, quality-gate mode). After any of those the
watcher saw every `.dat` in Subtracted/ as brand new.

Two layers are tested here:

  1. `_reseed_intake()` is the only way to get a clean slate, and it clears
     and reseeds in ONE critical section, so the watcher can never observe an
     empty `_handled`. No path in analyzer/app.py may clear `_handled`
     without it.
  2. `_triage_backlog()` is the backstop: even if a backlog does somehow form,
     one poll takes the historical part out and seeds it, instead of fitting
     it and starving the live run. Live data (inside the crash-gap window) and
     a genuine burst are never throttled.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.app as az                                       # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path, monkeypatch):
    monkeypatch.setattr(az, "_project_root", str(tmp_path))
    monkeypatch.setattr(az, "_sub_folder", "1D/SAXS/Subtracted")
    monkeypatch.setattr(az, "_results_folder", "1D/SAXS/Results")
    monkeypatch.setattr(az, "_gate_mode", "off")   # no Good/ folder in these fixtures
    monkeypatch.setattr(az, "_pending", {})
    monkeypatch.setattr(az, "_pending_at", {})
    az._handled.clear(); az._lastsig.clear()
    yield
    az._handled.clear(); az._lastsig.clear()


def _sub_dir(tmp_path) -> Path:
    d = tmp_path / "1D" / "SAXS" / "Subtracted"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fit_dir(tmp_path) -> Path:
    d = tmp_path / "1D" / "SAXS" / "Results" / "Fit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile(sub: Path, stem: str, age_s: float = 0.0) -> Path:
    """A subtracted profile whose mtime is `age_s` seconds in the past."""
    p = sub / f"{stem}.dat"
    p.write_text("# header\n1.0 2.0 0.1\n", encoding="utf-8")
    if age_s:
        t = time.time() - age_s
        import os
        os.utime(p, (t, t))
    return p


def _with_fit_record(fit: Path, stem: str) -> None:
    (fit / f"fit_{stem}.dat").write_text("# fit record\n", encoding="utf-8")


# ── layer 1: a clean slate always means "reseeded", never "empty" ────────────
def test_reseed_leaves_every_already_fit_profile_marked_handled(tmp_path):
    """The regression itself: after a reset, an old run's profiles must still
    be marked handled, or the watcher re-fits all of them."""
    sub, fit = _sub_dir(tmp_path), _fit_dir(tmp_path)
    stems = [f"Run9_r{i:03d}_sample" for i in range(20)]
    for s in stems:
        _profile(sub, s, age_s=7200)      # yesterday's run
        _with_fit_record(fit, s)

    az._reseed_intake("campaign aborted")

    assert len(az._handled) == 20, "an abort/reset must reseed, not just clear"
    for s in stems:
        assert str(sub / f"{s}.dat") in az._handled


def test_reseed_still_leaves_a_crash_gap_profile_for_the_watcher(tmp_path):
    """A profile written moments ago with no Fit record was never fit — the
    reset must NOT mark it handled, or that frame is lost silently."""
    sub, fit = _sub_dir(tmp_path), _fit_dir(tmp_path)
    _profile(sub, "Run9_r001_sample", age_s=7200); _with_fit_record(fit, "Run9_r001_sample")
    live = _profile(sub, "Run10_r001_sample", age_s=5.0)     # just landed, never fit

    az._reseed_intake("campaign aborted")

    assert str(live) not in az._handled
    assert len(az._handled) == 1


def _toplevel_func_src(src: str, name: str) -> str:
    """The source of one module-level `def name(...)`, by indentation."""
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"def {name}("))
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if ln.strip() and not ln[0].isspace():
            break
        out.append(ln)
    return "\n".join(out)


def _strip_prose(text: str) -> str:
    """Drop docstrings and comments — they discuss the bug by name."""
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    return re.sub(r"^\s*#.*$", "", text, flags=re.M)


def test_no_path_clears_the_intake_memo_without_reseeding_it():
    """Source invariant. `_handled.clear()` outside `_reseed_intake` is the bug:
    it hands the watcher an empty slate and it re-fits the back-catalogue."""
    src = (_ROOT / "analyzer" / "app.py").read_text()
    reseed = _toplevel_func_src(src, "_reseed_intake")
    assert "_handled.clear()" in _strip_prose(reseed), \
        "_reseed_intake must clear before it reseeds"

    outside = _strip_prose(src.replace(reseed, ""))
    assert "_handled.clear()" not in outside, (
        "some path clears _handled without reseeding — call _reseed_intake() instead"
    )


@pytest.mark.parametrize("route_fn", ["set_project", "api_folder", "api_campaign_abort"])
def test_the_three_reset_routes_go_through_reseed(route_fn):
    """The three paths that regressed must each call _reseed_intake."""
    src = (_ROOT / "analyzer" / "app.py").read_text()
    fn = src.split(f"def {route_fn}(")[1].split("\n@app.route")[0]
    assert "_reseed_intake(" in fn, f"{route_fn} must reseed the intake memo"


# ── layer 2: the watcher never fits a historical backlog ─────────────────────
def test_triage_takes_a_historical_backlog_out_of_one_poll(tmp_path):
    sub = _sub_dir(tmp_path)
    old = [(_profile(sub, f"Run9_r{i:03d}_sample", age_s=7200), (10, int(1e9 * (time.time() - 7200))))
           for i in range(163)]

    keep = az._triage_backlog(old)

    assert len(keep) == az._BACKLOG_TRIAGE_N, \
        "a 163-file backlog must not be fit in one poll — that is the stall"
    assert len(az._handled) == 163 - az._BACKLOG_TRIAGE_N, \
        "the dropped files must be SEEDED, or they come straight back next poll"


def test_triage_never_throttles_live_data(tmp_path):
    """A genuine burst — every file inside the crash-gap window — is real work
    from the running loop and must all be fit, however many there are."""
    sub = _sub_dir(tmp_path)
    now = time.time()
    live = [(_profile(sub, f"Run10_r{i:03d}_sample", age_s=5.0), (10, int(1e9 * (now - 5))))
            for i in range(50)]

    keep = az._triage_backlog(live)

    assert len(keep) == 50
    assert az._handled == {}


def test_triage_keeps_live_data_and_drops_history_in_a_mixed_poll(tmp_path):
    sub = _sub_dir(tmp_path)
    now = time.time()
    items = [(_profile(sub, f"Run9_r{i:03d}_sample", age_s=7200),
              (10, int(1e9 * (now - 7200)))) for i in range(100)]
    fresh = [(_profile(sub, f"Run10_r{i:03d}_sample", age_s=4.0),
              (10, int(1e9 * (now - 4)))) for i in range(3)]

    keep = az._triage_backlog(items + fresh)
    kept = {p.name for p, _ in keep}

    for p, _ in fresh:
        assert p.name in kept, "live data must never be dropped by triage"
    assert len(keep) == az._BACKLOG_TRIAGE_N


def test_a_normal_poll_is_untouched(tmp_path):
    sub = _sub_dir(tmp_path)
    now = time.time()
    items = [(_profile(sub, f"Run10_r{i:03d}_sample", age_s=2.0),
              (10, int(1e9 * (now - 2)))) for i in range(4)]

    assert az._triage_backlog(items) == items
    assert az._handled == {}


def test_one_poll_after_an_abort_fits_only_the_fresh_runs_data(tmp_path, monkeypatch):
    """End to end over the real poll body: an aborted run's 163 profiles are
    still on disk, the operator starts a fresh run, one live profile lands.
    Exactly one fit must happen."""
    sub, fit = _sub_dir(tmp_path), _fit_dir(tmp_path)
    for i in range(163):
        s = f"Run9_r{i:03d}_sample"
        _profile(sub, s, age_s=7200); _with_fit_record(fit, s)

    az._reseed_intake("campaign aborted")          # what /api/campaign/abort now does

    live = _profile(sub, "Run10_r001_sample", age_s=1.0)
    fitted: list = []
    monkeypatch.setattr(az, "_analyze_file", lambda p: fitted.append(p.name))

    az._watch_once()        # first poll: new file, signature unstable -> "wait"
    assert fitted == [], "a file is never fit on the poll it first appears"
    az._watch_once()        # second poll: stable -> fit
    assert fitted == [live.name], \
        f"expected only the fresh run's profile to be fit, got {len(fitted)}"


# ── Continue offers the LATEST run, or nothing ──────────────────────────────
def _campaign_rec(tmp_path, *, cid: str, run_no: int, started: str,
                   outcome: str | None = None) -> Path:
    d = tmp_path / "1D" / "SAXS" / "Results"
    d.mkdir(parents=True, exist_ok=True)
    rec = {"campaign_id": cid, "run_no": run_no, "run_tag": f"Run{run_no}",
           "target_size": 5.0, "tolerance": 0.3, "pdi_cap": 0.15,
           "budget": 25, "n_init": 10, "started_at": started, "status": "running"}
    if outcome:
        rec["outcome"] = outcome
    p = d / f"campaign_{cid}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_a_stale_unfinished_run_is_not_offered_once_a_later_run_exists(tmp_path):
    """The reported bug, exactly: Continue was stuck on Run9.

    Run9 ended without its record being finalised (a crash, a kill -9), so it
    had no "outcome" key. The old rule — highest run_no with no outcome — made
    it the Continue candidate forever, even after Run10/11/12 had been and
    gone, because those DID record an outcome. Continuing it would rebuild a
    campaign from weeks-old records and propose conditions tagged Run9."""
    _campaign_rec(tmp_path, cid="c9",  run_no=9,  started="2026-09-01T00:00:00")
    _campaign_rec(tmp_path, cid="c10", run_no=10, started="2026-09-02T00:00:00",
                  outcome="converged")
    _campaign_rec(tmp_path, cid="c11", run_no=11, started="2026-09-03T00:00:00",
                  outcome="aborted")

    assert az._latest_run_record()["run_no"] == 11
    assert az._latest_incomplete_run() is None, \
        "the latest run finished — there is nothing to continue, Start fresh"


def test_the_latest_run_is_offered_while_it_is_unfinished(tmp_path):
    _campaign_rec(tmp_path, cid="c9",  run_no=9,  started="2026-09-01T00:00:00")
    _campaign_rec(tmp_path, cid="c12", run_no=12, started="2026-09-04T00:00:00")

    rec = az._latest_incomplete_run()
    assert rec is not None and rec["run_no"] == 12, \
        "the run just interrupted is the one to continue, not the oldest stale one"


def test_the_latest_run_is_decided_by_run_no_then_start_time(tmp_path):
    """Two records can share a run_no if one was written before the number was
    derived; started_at breaks the tie."""
    _campaign_rec(tmp_path, cid="a", run_no=7, started="2026-09-01T00:00:00",
                  outcome="converged")
    _campaign_rec(tmp_path, cid="b", run_no=7, started="2026-09-05T00:00:00")
    rec = az._latest_incomplete_run()
    assert rec is not None and rec["campaign_id"] == "b"


def test_no_records_at_all_means_nothing_to_continue(tmp_path):
    assert az._latest_run_record() is None
    assert az._latest_incomplete_run() is None


def test_the_route_says_why_continue_is_unavailable(tmp_path, monkeypatch):
    """An empty response reads as "the button is broken" — which is how a
    stale Run9 offer went unquestioned. The reason reads as "Start a new one"."""
    monkeypatch.setattr(az, "_campaign", None)
    client = az.app.test_client()

    r = client.get("/api/campaign/incomplete").get_json()
    assert "no previous run" in r["reason"], r

    _campaign_rec(tmp_path, cid="c11", run_no=11, started="2026-09-03T00:00:00",
                  outcome="converged")
    r = client.get("/api/campaign/incomplete").get_json()
    assert r.get("run_tag") is None
    assert "Run11" in r["reason"] and "converged" in r["reason"], r
    assert r["latest_run_tag"] == "Run11"

    _campaign_rec(tmp_path, cid="c12", run_no=12, started="2026-09-04T00:00:00")
    r = client.get("/api/campaign/incomplete").get_json()
    assert r["run_tag"] == "Run12" and "reason" not in r, r
