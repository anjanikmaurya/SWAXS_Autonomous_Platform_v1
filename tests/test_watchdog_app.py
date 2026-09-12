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
  cycle  the stage chart is PER RUN (recipe_id), not per file — a per-file
         elapsed time was meaningless once anything stalled or backfilled
  plots  sizeless fits are omitted rather than plotted at 0 nm; the
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


# ── the per-run cycle chart ────────────────────────────────────────────────
def _files(entries: list) -> dict:
    return {"files": {f"k{i}": e for i, e in enumerate(entries)}}


def _f(stage: str, keyword: str, path: str) -> dict:
    return {"stage": stage, "keyword": keyword, "path": path}


def test_cycle_times_are_per_run_and_never_negative(tmp_path, monkeypatch):
    """The per-FILE metric was meaningless once anything stalled: it measured
    one file's mtime minus its input's, so a sample subtracted hours after it
    was averaged read as "subtract takes 5 h". Per run, every boundary is that
    run's own stage completion, so each span is real and non-negative."""
    t0 = 1_000_000.0
    made = {}

    def touch(name: str, at: float) -> str:
        fp = tmp_path / name
        fp.write_text("x")
        os.utime(fp, (at, at))
        made[name] = at
        return str(fp)

    manifest = _files([
        _f("reduced",    "Run9_r001_sample", touch("a1.dat", t0 + 0)),
        _f("reduced",    "Run9_r001_sample", touch("a2.dat", t0 + 60)),
        _f("averaged",   "Run9_r001_sample", touch("a3.dat", t0 + 90)),
        _f("subtracted", "Run9_r001_sample", touch("a4.dat", t0 + 120)),
    ])
    manifest["analyses"] = {"x": {
        "type": "nanoparticle", "file_path": str(tmp_path / "Run9_r001_sample.dat"),
        "updated_at": __import__("datetime").datetime.fromtimestamp(
            t0 + 160, __import__("datetime").timezone.utc).isoformat(),
        "results": {"name": "Run9_r001", "diameter": 9.0}}}

    cy = wd._run_cycle_times(manifest)

    assert cy["reduce"]["avg_s"] == 60.0, "acquisition span: last frame - first frame"
    assert cy["average"]["avg_s"] == 30.0
    assert cy["subtract"]["avg_s"] == 30.0
    assert cy["_runs"] == 1
    assert all(v["avg_s"] is None or v["avg_s"] >= 0
               for k, v in cy.items() if not k.startswith("_"))


def test_the_background_lane_does_not_date_the_run_early(tmp_path):
    """The reactor collects background during the flush BEFORE the sample, so
    counting it would inflate every run's reduce span by the flush time."""
    t0 = 2_000_000.0

    def touch(name: str, at: float) -> str:
        fp = tmp_path / name
        fp.write_text("x")
        os.utime(fp, (at, at))
        return str(fp)

    manifest = _files([
        _f("reduced", "Run9_r002_background", touch("b0.dat", t0 - 1800)),  # flush
        _f("reduced", "Run9_r002_sample",     touch("b1.dat", t0)),
        _f("reduced", "Run9_r002_sample",     touch("b2.dat", t0 + 45)),
    ])
    cy = wd._run_cycle_times(manifest)
    assert cy["reduce"]["avg_s"] == 45.0, \
        "the background lane must not be part of the sample cycle"


def test_a_stall_lands_on_one_stage_not_all_of_them(tmp_path):
    t0 = 3_000_000.0

    def touch(name: str, at: float) -> str:
        fp = tmp_path / name
        fp.write_text("x")
        os.utime(fp, (at, at))
        return str(fp)

    manifest = _files([
        _f("reduced",    "Run9_r003_sample", touch("c1.dat", t0)),
        _f("reduced",    "Run9_r003_sample", touch("c2.dat", t0 + 30)),
        _f("averaged",   "Run9_r003_sample", touch("c3.dat", t0 + 40)),
        _f("subtracted", "Run9_r003_sample", touch("c4.dat", t0 + 4000)),  # stalled
    ])
    cy = wd._run_cycle_times(manifest)
    assert cy["reduce"]["avg_s"] == 30.0
    assert cy["average"]["avg_s"] == 10.0
    assert cy["subtract"]["avg_s"] == 3960.0, "the stall belongs to subtract alone"


def test_an_incomplete_run_contributes_only_the_stages_it_reached(tmp_path):
    t0 = 4_000_000.0

    def touch(name: str, at: float) -> str:
        fp = tmp_path / name
        fp.write_text("x")
        os.utime(fp, (at, at))
        return str(fp)

    manifest = _files([
        _f("reduced",  "Run9_r004_sample", touch("d1.dat", t0)),
        _f("reduced",  "Run9_r004_sample", touch("d2.dat", t0 + 20)),
        _f("averaged", "Run9_r004_sample", touch("d3.dat", t0 + 35)),
    ])
    cy = wd._run_cycle_times(manifest)
    assert cy["reduce"]["avg_s"] == 20.0 and cy["average"]["avg_s"] == 15.0
    assert cy["subtract"]["avg_s"] is None and cy["fit"]["avg_s"] is None, \
        "a stage the run never reached is 'no data', never a zero bar"


def test_no_runs_at_all_reports_no_data_rather_than_zeros():
    cy = wd._run_cycle_times({})
    assert cy["_total_s"] is None and cy["_runs"] == 0
    assert all(cy[s]["avg_s"] is None for s in ("reduce", "average", "subtract", "fit"))


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


# ── quiet hours: visible, and changeable without editing YAML ──────────────
def test_quiet_hours_active_is_reported(tmp_path, monkeypatch):
    """The reported "I stopped getting Slack messages": quiet hours took
    effect for the first time once _load_config stopped returning {} (W4),
    and it suppresses results/progress SILENTLY. Its effect has to be
    reportable, or "no messages" is indistinguishable from a broken webhook."""
    import datetime as dt
    monkeypatch.setattr(wd, "_settings", {"quiet_hours": ((23, 0), (7, 0))})

    # 01:30 local — inside 23:00-07:00
    monkeypatch.setattr(wd, "_now",
                        lambda: dt.datetime(2026, 9, 12, 1, 30).astimezone())
    assert wd._quiet_now() is True

    # 13:30 local — outside
    monkeypatch.setattr(wd, "_now",
                        lambda: dt.datetime(2026, 9, 12, 13, 30).astimezone())
    assert wd._quiet_now() is False

    monkeypatch.setattr(wd, "_settings", {"quiet_hours": None})
    assert wd._quiet_now() is False, "no window configured → never quiet"


def test_quiet_hours_can_be_set_and_turned_off_over_the_api(tmp_path, monkeypatch):
    cfg = tmp_path / "watchdog_config.yml"
    _write_config(cfg, GOOD_CONFIG)
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))
    client = wd.app.test_client()

    r = client.post("/api/settings", json={"quiet_hours": None}).get_json()
    assert r["ok"] and r["quiet_hours"] is None
    assert wd._settings["quiet_hours"] is None, "must be reloaded, not just written"

    r = client.post("/api/settings", json={"quiet_hours": "01:00-05:30"}).get_json()
    assert r["ok"] and r["quiet_hours"] == "01:00-05:30"
    assert wd._settings["quiet_hours"] == ((1, 0), (5, 30)), \
        "should_send needs the PARSED tuple, not the string that was written"


def test_an_invalid_quiet_hours_window_is_refused_and_the_file_survives(tmp_path, monkeypatch):
    cfg = tmp_path / "watchdog_config.yml"
    _write_config(cfg, GOOD_CONFIG)
    monkeypatch.setattr(wd, "_project_root", str(tmp_path))
    client = wd.app.test_client()

    r = client.post("/api/settings", json={"quiet_hours": "banana"})
    assert r.status_code == 400
    assert "HH:MM" in r.get_json()["error"]
    assert "notify:" in cfg.read_text(), "a rejected value must not truncate config.yml"


def test_settings_writes_are_atomic(tmp_path, monkeypatch):
    """config.yml is tracked in git and, since the fail-closed fix, an
    unreadable one stops sending — so a half-written file is expensive."""
    from src.watchdog.settings import save_notify_settings
    cfg = tmp_path / "watchdog_config.yml"
    _write_config(cfg, GOOD_CONFIG)
    save_notify_settings(cfg, slack_enabled=True)
    assert not list(tmp_path.glob("*.part")), "the temp file must be replaced, not left"
    assert "slack_enabled: true" in cfg.read_text()


# ── the reactor's pre-collection phases ────────────────────────────────────
# The dashboard had nothing to say between "run requested" and the first frame
# on disk: the pipeline stage is 'collect' or 'idle' and the reactor circle
# showed the bare state name. But that gap is the longest part of a cycle —
# the shipped flush is 20 minutes and arming waits for temperature on top —
# so half an hour of a working rig read as a stopped one.
def test_flushing_reports_time_left_and_the_background_collection():
    phase, label, detail = wd._reactor_phase({
        "state": "flushing", "supervising": True,
        "flush_remaining_s": 845.0, "flush_pump": "P3",
        "spec": {"collecting": True}})
    assert (phase, label) == ("flushing", "FLUSHING")
    assert "14m 05s left" in detail
    assert "pump P3" in detail
    assert "background" in detail, \
        "the background is collected during the flush — say so, it is not idle time"


def test_temperature_gated_arming_reports_the_gap_to_target():
    phase, label, detail = wd._reactor_phase({
        "state": "arming", "supervising": True, "arm_mode": "temp",
        "temperature": {"current": 188.4, "target": 240.0, "tolerance": 2.0,
                        "source": "beamline", "stale": False, "stable": False},
        "spec": {}})
    assert (phase, label) == ("ramping", "RAMPING TO TEMPERATURE")
    assert "188.4 → 240.0 °C" in detail and "+51.6 to go" in detail
    assert "±2 °C band" in detail, "the tolerance decides when the run starts"


def test_arming_at_target_says_so_rather_than_plus_zero():
    _p, _l, detail = wd._reactor_phase({
        "state": "arming", "supervising": True, "arm_mode": "temp",
        "temperature": {"current": 240.0, "target": 240.0, "tolerance": 2.0,
                        "source": "beamline", "stale": False, "stable": True},
        "spec": {}})
    assert "at target" in detail and "stable" in detail


# ── the temperature has to be a MEASUREMENT, or say that it is not ─────────
# With no sensor wired, TempController.read() is still the shipped stub: it
# returns the last value (the 25 °C ambient default) forever. Rendering that
# as "25.0 → 240.0 °C (+215.0 to go)" presents a placeholder as a reading, it
# never changes, and temperature-gated arming can never open its gate.
def test_an_unwired_sensor_is_named_rather_than_shown_as_a_reading():
    detail = wd._temp_detail({"target": 240.0, "current": 25.0, "tolerance": 2.0,
                              "source": "unwired", "stale": False})
    assert "25.0" not in detail, \
        "the placeholder value must NOT be shown — it looks like a measurement"
    assert "target 240.0 °C" in detail
    assert "no temperature sensor wired" in detail
    assert "timed arming" in detail, "say what to do about it"


def test_a_stale_beamline_reading_reports_its_age_not_its_value():
    detail = wd._temp_detail({"target": 240.0, "current": 25.0, "tolerance": 2.0,
                              "source": "beamline", "stale": True, "age_s": 735.0})
    assert "25.0" not in detail, \
        "a stale value is whatever it was when the source died — do not show it"
    assert "STALE" in detail and "12m 15s old" in detail
    assert "time out" in detail


def test_a_simulated_reading_is_labelled_simulated():
    detail = wd._temp_detail({"target": 240.0, "current": 231.0, "tolerance": 2.0,
                              "source": "mock", "stale": False})
    assert "231.0 → 240.0 °C" in detail and "simulated" in detail


def test_a_live_reading_carries_no_disclaimer():
    detail = wd._temp_detail({"target": 240.0, "current": 188.4, "tolerance": 2.0,
                              "source": "beamline", "stale": False})
    for word in ("simulated", "placeholder", "STALE"):
        assert word not in detail, detail


def test_missing_numbers_fall_back_to_the_target_alone():
    assert wd._temp_detail({"target": 240.0, "source": "beamline",
                            "stale": False}) == "target 240.0 °C"
    assert wd._temp_detail({}) == "target —"


def test_the_reactor_publishes_where_its_temperature_comes_from():
    """_temp_detail is only honest if the reactor tells it. TempController
    exposes source/trustworthy and controller.status() forwards them."""
    import inspect
    from src.reactor.hardware import TempController
    assert isinstance(TempController.source, property)
    assert isinstance(TempController.trustworthy, property)
    src = inspect.getsource(__import__("src.reactor.controller",
                                        fromlist=["x"]).ReactorController.status)
    for key in ('"source"', '"stale"', '"age_s"', '"trustworthy"'):
        assert key in src, f"status() must publish {key} for the dashboard"


def test_timed_arming_is_a_countdown_not_a_temperature():
    phase, label, detail = wd._reactor_phase({
        "state": "arming", "supervising": True, "arm_mode": "timed",
        "arm_remaining_s": 95.0, "arm_total_s": 300.0, "spec": {}})
    assert (phase, label) == ("arming", "ARMING")
    assert "1m 35s of 5m 00s left" == detail


def test_running_is_synthesising_until_the_detector_starts():
    phase, label, detail = wd._reactor_phase({
        "state": "running", "supervising": True,
        "elapsed_s": 412.0, "duration_s": 900.0, "spec": {"collecting": False}})
    assert (phase, label) == ("synthesising", "SYNTHESISING")
    assert detail == "6m 52s of 15m 00s"


def test_running_becomes_collecting_once_spec_is_collecting():
    phase, label, detail = wd._reactor_phase({
        "state": "running", "supervising": True,
        "spec": {"collecting": True, "frames": 10, "exposure_s": 30}})
    assert (phase, label) == ("collecting", "COLLECTING")
    assert "10 frames" in detail and "30s exposure" in detail


@pytest.mark.parametrize("state,phase,label", [
    ("ready", "ready", "READY TO START"),
    ("estop", "estop", "EMERGENCY STOP"),
    ("idle", "idle", ""),
])
def test_the_remaining_reactor_states(state, phase, label):
    p, l, _d = wd._reactor_phase({"state": state, "supervising": True, "spec": {}})
    assert (p, l) == (phase, label)


def test_no_reactor_answer_is_empty_not_a_guess():
    assert wd._reactor_phase({}) == ("", "", "")
    assert wd._reactor_phase(None) == ("", "", "")


def test_an_unknown_future_state_is_passed_through_rather_than_hidden():
    p, l, _d = wd._reactor_phase({"state": "purging", "supervising": True, "spec": {}})
    assert (p, l) == ("purging", "PURGING"), \
        "a state this map has not seen must still reach the operator"


def test_the_loop_state_carries_the_phase_to_the_dashboard(monkeypatch):
    """_loop_state is what the banner and the reactor circle read."""
    probes = {"reactor": {"state": "flushing", "supervising": True,
                          "flush_remaining_s": 600.0, "spec": {}},
              "analyzer": {}, "status": {}, "monitors": {}}
    loop = wd._loop_state(probes)
    assert loop["reactor"]["phase"] == "flushing"
    assert loop["reactor"]["phase_label"] == "FLUSHING"
    assert "10m 00s left" in loop["reactor"]["phase_detail"]
    assert loop["reactor"]["detail"] == "FLUSHING", \
        "the circle should read the phase, not the bare state name"


def test_fmt_secs_short_is_readable_and_never_negative():
    assert wd._fmt_secs_short(47) == "47s"
    assert wd._fmt_secs_short(200) == "3m 20s"
    assert wd._fmt_secs_short(3864) == "1h 04m"
    assert wd._fmt_secs_short(-5) == ""
    assert wd._fmt_secs_short(None) == ""


# ── the five step boxes must be identical ──────────────────────────────────
# Circles clipped their text, so the steps became boxes — and then the boxes
# were ragged, because a detail that wrapped to two lines grew its own box
# (min-height) while the captions below differ in height by three rows. These
# are the CSS rules that make them identical; asserted as text because there is
# no layout engine in the suite to measure with.
def _ring_css() -> str:
    html = (_ROOT / "watchdog" / "templates" / "index.html").read_text()
    css = html.split("<style>")[1].split("</style>")[0]
    # Just the ring block, so an unrelated rule elsewhere cannot satisfy these.
    return css[css.index(".ring {"):css.index("/* Node") if "/* Node" in css
               else css.index(".rx-track")]


def test_the_box_height_is_a_fixed_track_not_a_minimum():
    css = _ring_css()
    assert "grid-template-rows:var(--box-h)" in css.replace(" ", ""), \
        "the box must sit in a FIXED grid row — min-height let a two-line " \
        "detail grow its own box and leave the row ragged"
    assert "min-height:var(--box-h)" not in css.replace(" ", ""), \
        "min-height is what made the boxes unequal"


def test_the_boxes_share_the_row_equally():
    css = _ring_css().replace(" ", "")
    assert "flex:1 1 0".replace(" ", "") in css, \
        "flex-basis 0 with grow 1 is what makes every box the same WIDTH"
    assert "min-width:0" in css, "without it long content widens its own box"


def test_the_box_fills_its_track_and_clips_rather_than_growing():
    css = _ring_css().replace(" ", "")
    assert "height:100%" in css, "the box must fill the fixed track exactly"
    assert "overflow:hidden" in css


def test_a_long_detail_is_clamped_and_kept_reachable():
    html = (_ROOT / "watchdog" / "templates" / "index.html").read_text()
    css = _ring_css().replace(" ", "")
    assert "-webkit-line-clamp:2" in css, \
        "two lines, then clip — one freak value must not resize one box"
    assert "detail.title = text" in html, \
        "a clamped value has to stay reachable on hover"


def test_the_captions_have_a_reserved_height_of_their_own():
    css = _ring_css().replace(" ", "")
    assert "--cap-h:" in css and "minmax(var(--cap-h),auto)" in css, \
        "captions differ by three rows between Reactor and Fit; reserving a " \
        "height keeps the boxes aligned regardless"


def test_every_step_uses_the_same_box_class():
    html = (_ROOT / "watchdog" / "templates" / "index.html").read_text()
    assert html.count('class="step-box"') == 5
    assert html.count('class="step-hd"') == 5, \
        "name+chip header on every box, or they differ in internal layout"
    assert 'class="step-circle"' not in html, "circles clipped the text"
