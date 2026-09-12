"""
tests/test_watchdog_app.py — Auto Watch's app module.

`watchdog/app.py` had no tests, and that is where the audit
(docs/audits/AUTO_WATCH_AUDIT.md) found almost everything. Covers the four
"fails open / fails quiet" defects and the three broken-chart data bugs:

  W1  the metrics snapshot is computed once and shared, not per SSE client
  W2  the event window is recovered from manifest.json, so a restarted
      Auto Watch can still see a stall that began before it started
  W3  a malformed config.yml holds sending OFF instead of defaulting it ON
  W4  the config loads with no project folder selected
  W9  the per-project config override lives inside the project folder
  plots  a directory provenance input resolves to a real frame mtime;
         sizeless fits are omitted rather than plotted at 0 nm; the
         "Analysed" funnel counts nanoparticle fits only
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("SWAXS_NO_WATCH", "1")   # no daemon loops on import
os.environ.setdefault("SWAXS_NO_BUS", "1")

import watchdog.app as wd                                       # noqa: E402


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))
    monkeypatch.setattr(wd, "_config_error", "")
    wd._recent_events.clear()
    wd._metrics_cache["data"] = None
    wd._metrics_cache["ts"] = 0.0
    yield
    wd._recent_events.clear()


def _write_config(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


GOOD_CONFIG = """
notify:
  quiet_hours: "23:00-07:00"
  summary: "off"
  snooze_default_min: 30
  min_interval_s: 3
  slack_enabled: false
  categories:
    safety: false
    stalls: false
    results: false
    progress: false
    campaign: false
"""


# ── W3 / W4: config is loaded, and a bad one fails CLOSED ───────────────────
def test_config_loads_with_no_project_folder_selected(tmp_path, monkeypatch):
    """Auto Watch needs no project to probe apps or detect stalls, so an
    operator who never picks a folder must still get their own config.yml —
    it used to return {} and run on defaults (master ON, all categories ON)."""
    monkeypatch.setattr(wd, "_project_root", "")
    cfg = wd._load_config("")
    assert cfg.get("slack_enabled") is not None, "config must load without a project"
    assert "categories" in cfg and len(cfg["categories"]) == len(wd.CATEGORIES)


def test_operators_slack_off_is_honoured_without_a_project(tmp_path, monkeypatch):
    cfg_file = tmp_path / "watchdog_config.yml"
    _write_config(cfg_file, GOOD_CONFIG)
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))
    cfg = wd._load_config(str(tmp_path))
    assert cfg["slack_enabled"] is False, \
        "the operator turned sending off; it must not come back on"


def test_a_malformed_config_holds_sending_off(tmp_path, monkeypatch):
    """The old fallback was slack_enabled True with every category on, so one
    typo in config.yml overrode Stop and flooded Slack."""
    bad = tmp_path / "watchdog_config.yml"
    _write_config(bad, "notify:\n  summary: 'every-picosecond'\n")
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))

    cfg = wd._load_config(str(tmp_path))

    assert cfg["slack_enabled"] is False, "a config that cannot be read must fail CLOSED"
    assert not any(cfg["categories"].values()), "no category may be on when config is broken"
    assert wd._config_error, "the parse error must be recorded for the UI to show"


def test_a_good_config_clears_a_previous_error(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_config_error", "stale error")
    cfg_file = tmp_path / "watchdog_config.yml"
    _write_config(cfg_file, GOOD_CONFIG)
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))
    wd._load_config(str(tmp_path))
    assert wd._config_error == ""


# ── W9: the per-project override is INSIDE the project folder ──────────────
def test_project_config_override_is_inside_the_project_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))
    assert wd._config_path() == wd._HERE / "config.yml", "no override → app default"
    override = tmp_path / "watchdog_config.yml"
    _write_config(override, GOOD_CONFIG)
    assert wd._config_path() == override, \
        "an override in the project folder must win (it used to look in the parent)"


# ── W2: the event window survives a restart ────────────────────────────────
def _manifest_with_events(tmp_path, events: list) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"events": events}),
                                            encoding="utf-8")


def test_event_window_is_recovered_from_the_manifest(tmp_path):
    """A stalled pipeline emits nothing, so an Auto Watch restarted to
    investigate one never refilled its in-memory window and reported no stall."""
    _manifest_with_events(tmp_path, [
        {"type": "file.reduced", "timestamp": "2026-09-11T02:00:00+00:00",
         "data": {"file_path": "/x/a.dat", "keyword": "Run9_r001_sample"}},
        {"type": "file.averaged", "timestamp": "2026-09-11T02:05:00+00:00", "data": {}},
    ])
    n = wd._seed_recent_events()
    assert n == 2
    assert [e["type"] for e in wd._recent_events] == ["file.reduced", "file.averaged"]
    assert wd._recent_events[0]["data"]["keyword"] == "Run9_r001_sample", \
        "the trimmed payload must survive too — the lane/recipe view reads it"


def test_recovered_events_do_not_duplicate_or_displace_live_ones(tmp_path):
    live = {"type": "fit.complete", "timestamp": "2026-09-11T03:00:00+00:00", "data": {}}
    wd._recent_events.append(dict(live))
    _manifest_with_events(tmp_path, [
        {"type": "file.reduced", "timestamp": "2026-09-11T02:00:00+00:00", "data": {}},
        dict(live),                      # already delivered by the live bus
    ])
    wd._seed_recent_events()
    types = [e["type"] for e in wd._recent_events]
    assert types == ["file.reduced", "fit.complete"], types
    assert types.count("fit.complete") == 1, "must not double-count a live event"


def test_seeding_a_project_with_no_manifest_is_a_no_op(tmp_path):
    assert wd._seed_recent_events() == 0
    assert wd._recent_events == []


def test_seeding_ignores_events_with_no_timestamp(tmp_path):
    _manifest_with_events(tmp_path, [{"type": "file.reduced"}, {"nope": 1}])
    assert wd._seed_recent_events() == 0


# ── W1: one shared snapshot, not one per client ────────────────────────────
def test_metrics_snapshot_is_computed_once_and_reused(monkeypatch):
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"marker": calls["n"]}

    monkeypatch.setattr(wd, "_compute_metrics", counted)
    wd._metrics_cache["data"] = None

    first = wd._metrics_snapshot()
    for _ in range(50):                     # 50 SSE ticks across many tabs
        wd._metrics_snapshot()

    assert calls["n"] == 1, \
        "the disk scan must not run per tick per client — that is the W1 stall"
    assert wd._metrics_snapshot() == first


# ── plots: a directory provenance input must resolve to a real frame ───────
def test_directory_input_resolves_to_the_newest_frame_before_the_output(tmp_path):
    """The average app records input_files=[folder]. A directory's own mtime
    changes whenever anything lands in it, which gave averaging a latency of
    ~0 (or negative) and left the bar reading "no data yet" all run."""
    folder = tmp_path / "Reduction"
    folder.mkdir()
    now = time.time()
    for i, age in enumerate((300, 200, 100)):
        f = folder / f"frame{i}.dat"
        f.write_text("x")
        os.utime(f, (now - age, now - age))
    later = folder / "frame_after.dat"     # arrived AFTER the averaged output
    later.write_text("x")
    os.utime(later, (now, now))

    out_mtime = now - 50                   # the averaged file
    resolved = wd._input_mtime(folder, before=out_mtime)

    assert resolved == pytest.approx(now - 100, abs=1.0), \
        "must pick the newest frame that predates the output, not the directory"
    assert wd._input_mtime(folder, before=out_mtime) < out_mtime


def test_plain_file_input_is_unchanged(tmp_path):
    f = tmp_path / "a.raw"
    f.write_text("x")
    assert wd._input_mtime(f, before=time.time() + 10) == pytest.approx(f.stat().st_mtime)


def test_directory_with_no_eligible_frame_returns_none(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    assert wd._input_mtime(folder, before=time.time()) is None


# ── plots: only the analyzer's fits are fits ───────────────────────────────
def _manifest_with_analyses() -> dict:
    return {"analyses": {
        "a": {"type": "guinier", "file_path": "/x/1_sub.dat",
              "updated_at": "2026-09-11T01:00:00", "results": {}},
        "b": {"type": "porod", "file_path": "/x/1_sub.dat",
              "updated_at": "2026-09-11T01:01:00", "results": {}},
        "c": {"type": "nanoparticle", "file_path": "/x/1_sub.dat",
              "updated_at": "2026-09-11T02:00:00",
              "results": {"name": "r2", "diameter": 9.8, "pdi": 0.12, "confidence": 0.7}},
        "d": {"type": "nanoparticle", "file_path": "/x/2_sub.dat",
              "updated_at": "2026-09-11T01:30:00",
              "results": {"name": "r1", "diameter": 10.4, "pdi": 0.2, "confidence": 0.4}},
        "e": {"type": "nanoparticle", "file_path": "/x/3_sub.dat",
              "updated_at": "2026-09-11T01:45:00",
              "results": {"name": "r_failed", "diameter": None, "pdi": None,
                          "confidence": 0.0}},
    }}


def test_nanoparticle_filter_excludes_the_other_analysis_types():
    fits = wd._nanoparticle_analyses(_manifest_with_analyses())
    assert [e["results"]["name"] for e in fits] == ["r1", "r_failed", "r2"], \
        "nanoparticle fits only, ordered by updated_at"


def test_runs_omit_fits_with_no_size_and_are_time_ordered(monkeypatch):
    monkeypatch.setattr(wd, "_read_manifest", lambda: _manifest_with_analyses())
    monkeypatch.setattr(wd, "_probe_all_cached", lambda: {})
    monkeypatch.setattr(wd, "_throughput_last_24h", lambda: [])
    monkeypatch.setattr(wd, "_loop_state", lambda probes: {})
    monkeypatch.setattr(wd, "_health_row", lambda probes: [])

    out = wd._compute_metrics()
    runs = out["runs"]

    assert [r["recipe_id"] for r in runs] == ["r1", "r2"], \
        "a fit with no diameter must be omitted, not plotted at 0 nm"
    assert all(isinstance(r["size"], float) and r["size"] > 0 for r in runs)
    assert out["files"]["analysed"] == 3, \
        "the funnel counts nanoparticle-fitted FILES, not every analysis record"
