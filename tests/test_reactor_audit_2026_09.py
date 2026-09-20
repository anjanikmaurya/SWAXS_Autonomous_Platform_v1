"""
tests/test_reactor_audit_2026_09.py

Regression tests for the September 2026 reactor audit — docs/audits/REACTOR_AUDIT.md.

Separate from tests/test_reactor_audit_fixes.py, which holds the EARLIER safety
audit's regressions (backend-string normalisation, the control-loop fault
ordering, serial mapping). Different register, different numbering — these are
R1-R25.
One test (or group) per finding, named for what would break, and every one of
them FAILS against the code as it stood before the fix.

The findings this file holds down, in the order they appear below:

  R1  stopping the app from the hub never handed the rig back
  R2  one Stop during a blank flush killed background collection for the session
  R3  the over-temperature interlock could be blind for hours, alarm suppressed
  R4  a counting command that may open the shutter, fired ~86,400×/day
  R5  the E-stop's "could NOT idle" went to a tiny slot in another card
  R6  supervising / last_fault / temperature.source were computed, never shown
  R7  saved pump limits and conditions folder were never loaded at startup
  R8  every run-setting except exposure accepted zero and negative numbers
  R9  a pump's own max_flow was enforced at intake only, never while running
  R10 Vent during a run discarded the run with no record and no event
  R11 an arm timeout dropped the condition silently and stalled the queue
  R13 the shipped arming default contradicted the documentation
  R14 every sensor_min was 0, so the low-flow rejection could never fire
  R15 flow-fault detection was 15× faster in mock than on the rig
  R16 five controls failed silently; two were never disabled
  R17 no disconnect indicator
  R18 two in-process lists grew without bound
  R19 shutdown did not wait for an in-flight acquisition
  R20 the conditions folder was accepted without a glance, then created
  R21 a failed temperature command was swallowed
  R22 status() was rebuilt twice a second per connected browser
  R23 dead code
  R24 a detector-geometry path hard-coded to one machine
  R25 /api/set_project accepted a path that was not there

R12 (a rejected condition file is a log line only) is DELIBERATELY NOT HERE —
the operator deferred it. Do not add it without adding the fix.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.reactor import load_config, ReactorController          # noqa: E402
from src.reactor.hardware import _flow_ok, MockPump, TempController  # noqa: E402
from src.reactor.recipe import Recipe, RecipeError               # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────
def make_controller(**over):
    """A controller with its own config, the 2D simulator off, and the control
    loop STOPPED so a test can drive _tick_once() deterministically."""
    cfg = copy.deepcopy(load_config())
    cfg.setdefault("spec", {})["simulator"] = {"enabled": False}
    for dotted, v in over.items():
        sec, key = dotted.split(".", 1)
        cfg.setdefault(sec, {})[key] = v
    logs: list[tuple[str, str]] = []
    c = ReactorController(cfg, backend="mock",
                          log_cb=lambda m, t="info": logs.append((t, m)))
    c._alive = False
    time.sleep(0.25)          # let the loop thread notice and exit
    c.logs = logs             # type: ignore[attr-defined]
    return c


def recipe(rid="r001", **kw):
    d = dict(T_reac=240, F_tot=80, x_ODE=0.2, x_TOP=0.1, x_oley=0.1, recipe_id=rid)
    d.update(kw)
    return d


def said(c, needle: str) -> bool:
    return any(needle.lower() in m.lower() for _t, m in c.logs)


# ═════════════════════════════════════════════════════════════════════════════
# R2 — the stranded blank
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("action", ["abort", "estop", "vent_all"])
def test_r2_stopping_a_blank_flush_returns_the_recipe_instead_of_stranding_it(action):
    """The headline finding. `_pending` holds the recipe whose blank is being
    collected, and only _end_flush cleared it — so Stop, E-stop or Vent during
    that flush left it set forever. The condition was lost AND, because
    _begin_next only stages a blank when _pending is None, EVERY LATER
    CONDITION SILENTLY RAN WITH NO BACKGROUND."""
    c = make_controller()
    c.submit(recipe("rXYZ"))
    c.start()
    assert c.state == "flushing" and c._flush_kind == "blank"
    assert c._pending is not None

    getattr(c, action)()

    assert c._pending is None, f"{action} stranded the staged recipe"
    assert "rXYZ" in [r.recipe_id for r, _ in c.queue], \
        f"{action} lost the staged condition instead of re-queueing it"


def test_r2_later_conditions_still_get_a_background_after_an_abort():
    """The consequence that made R2 expensive rather than annoying: the run log
    stayed entirely normal while every subsequent condition lost its blank, and
    it only surfaced in the subtraction app much later.

    Note the expected order. r001 was recovered to the FRONT of the queue by
    the abort, so it runs first and r002/r003 follow — the recovered condition
    is not sent to the back and is not dropped."""
    c = make_controller()
    c.submit(recipe("r001"))
    c.start()
    c.abort()                                   # the stranding event
    c.submit(recipe("r002"))
    c.submit(recipe("r003"))

    # Driven with auto-run OFF, so since the R27 fix each Start takes exactly
    # one condition and the loop pauses after its flush. That is what makes
    # this readable: every iteration is one full blank → synthesis → flush.
    ran = []
    for _ in range(3):
        assert c.state in ("idle", "ready"), f"unexpected {c.state} before Start"
        c.start()
        assert c.state == "flushing" and c._flush_kind == "blank", \
            "a condition skipped its pre-synthesis blank"
        rid = c._bkg_recipe_id
        assert rid, "a condition would run with no background at all"
        c._end_flush()                          # blank done → synthesis
        assert c.state == "arming"
        assert c.current.recipe_id == rid, \
            f"the blank was tagged {rid!r} but {c.current.recipe_id!r} is running"
        ran.append(rid)
        c._enter_running()
        c._run_reason = "test"
        c._end_run(flush=True)                  # → plain clean-out flush
        c._end_flush()                          # → ready, paused

    assert ran == ["r001", "r002", "r003"], \
        f"expected the recovered condition to run first, got {ran}"


def test_r2_begin_next_recovers_a_stranded_pending_loudly():
    """Defence in depth. If anything ever strands _pending again, the next
    condition must say so rather than quietly skipping blanks forever.

    What "recovered" looks like: the stranded recipe is back in the queue and
    gets a proper blank flush of its own — so _pending IS set again at the end,
    legitimately, staging the same recipe. The bug was _pending holding a
    recipe that no flush was collecting for."""
    c = make_controller()
    r = Recipe.from_dict(recipe("ghost"))
    c._pending = (r, {n: 0.0 for n in c.pumps.pumps})   # simulate the old leak
    c.queue.clear()
    c.logs.clear()

    c._begin_next()

    assert said(c, "staged behind a blank"), "the recovery was silent"
    assert c.state == "flushing" and c._flush_kind == "blank", \
        "the recovered condition did not get a blank of its own"
    assert c._bkg_recipe_id == "ghost"
    assert c._pending is not None and c._pending[0].recipe_id == "ghost", \
        "the recovered condition was dropped instead of re-staged"


# ═════════════════════════════════════════════════════════════════════════════
# R3 — the bounded stale-suppression
# ═════════════════════════════════════════════════════════════════════════════
def test_r3_a_normal_collection_still_suppresses_the_stale_alarm():
    """The suppression exists for a good reason and must survive: with
    read_source 'spec' a 100 s acquisition blanks the reading every time, and
    reporting that as a sensor fault sent the operator to inspect a counter
    that was working perfectly."""
    c = make_controller()
    bl = c.beamline
    bl._collecting = True
    bl._collect_started = time.time()
    bl._collect_expected_s = 100.0
    assert c.temp.polling_paused is True
    assert c.temp.stale is False


def test_r3_a_backend_that_cannot_report_an_overrun_keeps_the_old_behaviour():
    """Caught by tests/test_reactor_safety.py while writing the fix, and worth
    its own case. The first version wrapped is_collecting() and
    collect_overrun_s() in ONE try, so a backend without the new method fell
    into the except and reported "not paused" — which would have alarmed on
    every ordinary acquisition. An unjudgeable pause must keep the original
    treatment, not be guessed either way."""
    class OldDriver:
        def is_collecting(self): return True
        # deliberately no collect_overrun_s

    c = make_controller()
    c.temp.beamline = OldDriver()
    assert c.temp.polling_paused is True
    assert c.temp.collect_overrun_s == 0.0


def test_r3_an_overrunning_collection_stops_excusing_a_frozen_temperature():
    """What was missing: a ceiling. A hung SPEC macro holds the lock for up to
    cmd_wait_s per line (12 × 600 s on the shipped macro = two hours) and for
    all of it the reactor flowed reagents with the T_max cut-out blind and the
    alarm switched off."""
    c = make_controller()
    bl = c.beamline
    bl._collecting = True
    bl._collect_expected_s = 100.0
    bl._collect_started = time.time() - (100.0 + c.temp.collect_pause_grace_s + 30)
    c.temp._last_read_ok = time.time() - 300

    assert c.temp.polling_paused is False, "the suppression has no ceiling again"
    assert c.temp.stale is True, "the alarm still cannot fire"

    c.state = "running"
    c._temp_stale_warned = False
    events: list[tuple[str, dict]] = []
    c._event = lambda t, d: events.append((t, d))
    c._safety_check()
    assert any(t == "reactor.safety" for t, _ in events)
    assert said(c, "holding the SPEC lock"), \
        "the alarm blamed the counter instead of naming the overrun"
    bl._collecting = False


# ═════════════════════════════════════════════════════════════════════════════
# R8 — bounded numbers
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field,value", [
    ("run_duration", "-5"), ("run_duration", "0"),
    ("flush_rate", "-50"), ("flush_rate", "0"),
    ("flush_duration", "0"), ("arm_wait_s", "-30"),
])
def test_r8_run_settings_refuse_zero_and_negative(field, value):
    """Only exposure_s was bounded. flush_rate=0 was the worst of the rest: a
    full-length 'flush' that moved no liquid, announced complete, and left the
    next condition's background to be measured on a dirty capillary."""
    c = make_controller()
    before = {k: getattr(c, k) for k in
              ("live_duration", "live_flush_rate", "live_flush_duration", "live_arm_wait")}
    c.set_run_settings({field: value})
    after = {k: getattr(c, k) for k in before}
    assert after == before, f"{field}={value} was accepted"
    assert said(c, "refused"), f"{field}={value} was refused in silence"


def test_r8_a_flush_always_commands_a_usable_rate():
    """Last line of defence: whatever the callers do, the flush pump is never
    commanded at zero, because that failure is invisible end to end."""
    c = make_controller()
    c.live_flush_rate = 0.0          # bypass set_run_settings entirely
    c._enter_flush()
    assert c.pumps.pumps[c._flush_pump].target > 0


@pytest.mark.parametrize("field", ["run_duration", "flush_rate", "flush_duration"])
def test_r8_a_condition_file_carrying_a_zero_override_is_rejected(field):
    """The same hole on the autonomous path. arm_wait_s was sign-checked here;
    its three neighbours were not."""
    with pytest.raises(RecipeError, match=field):
        Recipe.from_dict(recipe(**{field: 0}))


# ═════════════════════════════════════════════════════════════════════════════
# R9 — per-pump max at runtime
# ═════════════════════════════════════════════════════════════════════════════
def test_r9_a_narrowed_pump_limit_applies_to_an_already_queued_recipe():
    """Limits were checked at intake and never again, so narrowing one — the
    natural reaction to a pump misbehaving — did not apply to the recipe
    already in the queue. On the shipped config that is a 20× gap."""
    c = make_controller()
    c.submit(recipe("r020", F_tot=120, x_ODE=0.3))     # ode_dilution = 36 µL/min
    r, sp = c.queue.popleft()
    c.set_pump_limits({"ode_dilution": {"sensor_min": 0.0, "max_flow": 5.0}})
    c._pending = None
    c._start_recipe(r, sp)
    c._enter_running()
    c._safety_check()
    assert c.state == "estop", "a pump ran past its own max_flow unchecked"


# ═════════════════════════════════════════════════════════════════════════════
# R10 — Vent during a run
# ═════════════════════════════════════════════════════════════════════════════
def test_r10_vent_during_a_run_still_writes_the_run_record():
    """Vent jumped straight to idle without _end_run, so the synthesis left no
    history entry, no done.json, no manifest record and no event — the
    condition vanished while its 2D data sat on disk."""
    c = make_controller()
    c.submit(recipe("r010"))
    r, sp = c.queue.popleft()
    c._pending = None
    c._start_recipe(r, sp)
    c._enter_running()

    seen: list[str] = []
    c._event = lambda t, d: seen.append(t)
    c._feedback = lambda rid, p: seen.append("feedback:" + str(rid))
    c._manifest = lambda rec: seen.append("manifest")

    c.vent_all()

    assert len(c.history) == 1, "the run left no record"
    assert "reactor.run_complete" in seen
    assert "feedback:r010" in seen
    assert "manifest" in seen


def test_r10_vent_reports_a_pump_that_would_not_idle():
    """Same rule as the E-stop route: never print 'done' over a pump that is
    still delivering."""
    c = make_controller()
    failed = c.vent_all()
    assert failed == [], "mock pumps should idle cleanly"
    assert isinstance(failed, list), "vent_all must report, not return None"


# ═════════════════════════════════════════════════════════════════════════════
# R11 — the abandoned condition
# ═════════════════════════════════════════════════════════════════════════════
def test_r11_an_arm_timeout_is_reported_and_does_not_stall_the_queue():
    """Going quietly to idle was a three-part silence: no event (so Auto Watch
    could not report it), no feedback file (so the optimizer waited forever),
    and no _begin_next (so the rest of the queue sat there)."""
    c = make_controller()
    events: list[tuple[str, dict]] = []
    fed: list[str] = []
    c._event = lambda t, d: events.append((t, d))
    c._feedback = lambda rid, p: fed.append(str(rid))
    c.set_auto_run(True)

    c.submit(recipe("r100"))
    r, sp = (c.queue.popleft() if c.queue else (None, None))
    if r is None:                      # auto_run consumed it at submit
        r, sp = c._pending or (None, None)
        c._pending = None
    c.submit(recipe("r101"))           # something waiting behind it

    c._pending = None
    c._start_recipe(r, sp)
    c._arm_deadline = time.time() - 1
    c.temp.current = 25.0
    c._tick_once()

    assert any(t == "reactor.run_abandoned" for t, _ in events), \
        "Auto Watch cannot see an arm timeout"
    assert "r100" in fed, "the optimizer gets no feedback for an abandoned condition"
    assert c.history and c.history[-1]["status"] == "abandoned"
    assert c.history[-1]["reason"] == "arm timeout"
    assert c.state != "idle" or not c.queue, \
        "the queue was left stalled behind the abandoned condition"


def test_r11_an_abandoned_condition_is_not_written_to_the_manifest_as_a_run():
    """Nothing was synthesised. A manifest record reads as a completed run to
    every other app."""
    c = make_controller()
    wrote: list = []
    c._manifest = lambda rec: wrote.append(rec)
    c.submit(recipe("r110"))
    r, sp = c.queue.popleft()
    c._pending = None
    c._start_recipe(r, sp)
    c._abandon_condition("r110", "arm timeout")
    assert wrote == []


# ═════════════════════════════════════════════════════════════════════════════
# R13 — arming mode that cannot succeed
# ═════════════════════════════════════════════════════════════════════════════
def test_r13_the_shipped_config_and_the_documentation_agree():
    cfg = yaml.safe_load((_ROOT / "reactor" / "config.yml").read_text())
    shipped = str(cfg["arming"]["default_mode"]).lower()
    doc = (_ROOT / "reactor" / "knowledge.md").read_text()
    m = re.search(r"shipped default is\s+\**`?(\w+)`?\**", doc)
    assert m, "knowledge.md no longer states the shipped arming default"
    assert m.group(1).lower() == shipped, (
        f"config.yml ships {shipped!r} but knowledge.md says {m.group(1)!r}. "
        f"This is not cosmetic: temperature arming cannot succeed without a "
        f"live reading, so believing the wrong one costs 900 s per condition.")


def test_r13_temperature_arming_warns_immediately_when_it_cannot_succeed():
    """It used to wait out the full 900 s timeout first — once per condition,
    all night, with the reason only visible at the end of each wait."""
    c = make_controller()
    c.submit(recipe("r130", arm_mode="temperature"))
    r, sp = c.queue.popleft()
    c._pending = None
    c.temp.beamline = None          # no live source
    c.logs.clear()
    c._start_recipe(r, sp)
    assert said(c, "will wait the full"), \
        "a doomed temperature arm starts silently"


# ═════════════════════════════════════════════════════════════════════════════
# R14 — the disarmed low-flow guard
# ═════════════════════════════════════════════════════════════════════════════
def test_r14_a_pump_delivering_nothing_is_never_flow_ok():
    """With flow_sensitivity 1.0 a pump commanded to 0.04 µL/min and delivering
    EXACTLY ZERO sat inside the ±1.0 absolute band and reported healthy."""
    assert _flow_ok(0.0, 0.04, 1.0, 0.2) is False
    assert _flow_ok(0.0, 40.0, 1.0, 0.2) is False


def test_r14_a_healthy_pump_is_still_flow_ok():
    """The guard must not manufacture false alarms — those teach an operator to
    ignore the real ones."""
    assert _flow_ok(39.0, 40.0, 1.0, 0.2) is True
    assert _flow_ok(36.0, 40.0, 1.0, 0.2) is True
    assert _flow_ok(0.0, 0.0, 1.0, 0.2) is True        # not commanded


def test_r14_real_backend_refuses_to_open_ports_with_a_zero_sensor_min():
    """The number itself has to come from the installed sensor and cannot be
    guessed, so the code refuses to run with the guard switched off instead."""
    from src.reactor.hardware import PumpBank
    cfg = copy.deepcopy(load_config())
    for p in cfg["pumps"].values():
        p["sensor_min"] = 0.0
    with pytest.raises(RuntimeError, match="sensor_min"):
        PumpBank(cfg, backend="real")


# ═════════════════════════════════════════════════════════════════════════════
# R15 — a fault timer that means the same thing on both backends
# ═════════════════════════════════════════════════════════════════════════════
def test_r15_bad_flow_is_measured_in_seconds_not_ticks():
    """bad_flow_tol counted ticks, and a tick is 0.2 s in mock but 3 s on the
    rig — so the same 3 meant 0.8 s in rehearsal and 12 s at the beamline."""
    p = MockPump("t", max_flow=50.0, bad_flow_s=12.0)
    p.set_flow(40.0)
    p._settle_left = 0.0
    p.actual = 0.0                                   # commanded, delivering nothing
    for _ in range(int(11.0 / 0.2)):                 # 11 s of mock ticks
        p._update_health(0.2)
    assert p.flow_fault is False, "faulted before bad_flow_s elapsed"
    for _ in range(int(2.0 / 0.2)):                  # past 12 s
        p._update_health(0.2)
    assert p.flow_fault is True

    q = MockPump("t", max_flow=50.0, bad_flow_s=12.0)
    q.set_flow(40.0)
    q._settle_left = 0.0
    q.actual = 0.0
    for _ in range(4):                               # 4 real-pump polls = 12 s
        q._update_health(3.0)
    assert q.flow_fault is True, "the two backends still disagree"


# ═════════════════════════════════════════════════════════════════════════════
# R16 — controls that used to fail silently
# ═════════════════════════════════════════════════════════════════════════════
def test_r16_abort_and_reset_say_when_they_did_nothing():
    c = make_controller()
    ok, why = c.abort()
    assert ok is False and why, "Stop in idle still reports a bare success"
    ok, why = c.reset()
    assert ok is False and "already idle" in why


def test_r16_reset_refuses_mid_flush_with_a_reason():
    c = make_controller()
    c.submit(recipe("r160"))
    c.start()
    assert c.state == "flushing"
    ok, why = c.reset()
    assert ok is False and "flushing" in why


def test_r16_the_ui_disables_flush_and_tare_when_they_cannot_work():
    tpl = (_ROOT / "reactor" / "templates" / "index.html").read_text()
    assert "b-flush" in tpl, "the Flush now button has no id to disable"
    assert re.search(r"\$\('b-flush'\).*disabled", tpl, re.S), \
        "Flush now is still clickable in every state"
    assert "#tareRows button" in tpl, "the tare buttons are never disabled"


def test_r16_the_ui_surfaces_the_tare_reply():
    tpl = (_ROOT / "reactor" / "templates" / "index.html").read_text()
    body = re.sub(r"<!--.*?-->", "", tpl, flags=re.S)
    m = re.search(r"async function tare\([^)]*\)\s*\{(.*?)\n\}", body, re.S)
    assert m, "tare() is gone"
    assert "tareMsg" in m.group(1), "tare() still throws the reply away"


# ═════════════════════════════════════════════════════════════════════════════
# R5 / R6 / R17 — what the page tells the operator
# ═════════════════════════════════════════════════════════════════════════════
def _tpl() -> str:
    return (_ROOT / "reactor" / "templates" / "index.html").read_text()


def test_r5_estop_failures_go_to_a_dedicated_persistent_banner():
    """They used to go to #form-err — extra-small type at the bottom of the
    SYNTHESIS RECIPE card, a different card from the E-stop button, and wiped
    by the next recipe submission."""
    tpl = _tpl()
    assert 'id="faultBanner"' in tpl
    body = re.sub(r"<!--.*?-->", "", tpl, flags=re.S)
    assert "/api/estop" in body and "LOUD" in body, \
        "the E-stop route is not routed to the loud banner"
    m = re.search(r"const LOUD\s*=\s*\[([^\]]*)\]", body)
    assert m and "/api/estop" in m.group(1) and "/api/vent" in m.group(1)


def test_r6_the_page_shows_whether_anything_is_supervising():
    tpl = _tpl()
    for field in ("supervising", "last_fault", "loop_faults"):
        assert field in tpl, f"{field} is still computed and never displayed"


def test_r6_the_page_says_whether_the_temperature_is_a_measurement():
    """The docstring on TempController.source says anything displaying a
    temperature has to distinguish a live reading from an ambient placeholder.
    The page printed the number and nothing else."""
    tpl = _tpl()
    for field in ("source", "stale", "age_s"):
        assert re.search(rf"T\.{field}\b|temperature\.{field}\b", tpl), \
            f"temperature.{field} is not read by the UI"
    assert "NOT A MEASUREMENT" in tpl


def test_r6_delivered_volume_is_displayed():
    assert "v_delivered" in _tpl()


def test_r17_the_page_notices_when_the_app_stops_answering():
    """render() only runs on a frame, so a dead app used to leave
    'running · 240 °C' on screen indefinitely."""
    tpl = _tpl()
    assert "es.onerror" in tpl, "EventSource failures are still unhandled"
    assert "DISCONNECTED" in tpl
    assert "_lastFrame" in tpl, "nothing tracks how old the last frame is"


# ═════════════════════════════════════════════════════════════════════════════
# R1 / R7 / R19 / R20 / R22 / R23 / R24 / R25 — the app shell and config
# ═════════════════════════════════════════════════════════════════════════════
def _app_src() -> str:
    return (_ROOT / "reactor" / "app.py").read_text()


def test_r1_the_reactor_hands_the_rig_back_on_sigterm():
    """atexit does not run on SIGTERM, and SIGTERM is exactly how the hub stops
    every app — so Stop on the hub card left every pump holding its last
    commanded flow with the supervisor gone, and SPEC locked to a dead process."""
    src = _app_src()
    assert "import signal" in src, "no signal handling at all"
    assert "SIGTERM" in src and "SIGINT" in src
    assert "_shutdown_once" in src, "the two exit paths are not deduplicated"
    m = re.search(r"for _sig in \(([^)]*)\)", src)
    assert m and "SIGTERM" in m.group(1)


def test_r7_saved_pump_limits_and_folder_are_loaded_at_startup(tmp_path, monkeypatch):
    """Proven end-to-end in the audit: both reverted to config.yml on every
    restart, because the only caller was /api/set_project — which the hub POSTs
    only when the folder CHANGES, not on launch."""
    (tmp_path / "reactor_limits.json").write_text(json.dumps(
        {"limits": {"top": {"sensor_min": 2.0, "max_flow": 7.0}}}))
    (tmp_path / "reactor_settings.json").write_text(json.dumps(
        {"recipes_folder": str(tmp_path / "conds")}))
    (tmp_path / "conds").mkdir()

    src = _app_src()
    # The startup block must call both, and before the run-settings restore.
    block = src[src.index("if _project_root:\n    # The project root holds"):]
    block = block[:block.index("\n\n\n")]
    for fn in ("_load_limits()", "_load_recipes_folder()"):
        assert fn in block, f"{fn} still runs only from /api/set_project"
    assert block.index("_load_limits()") < block.index("_restore_auto_run()"), \
        "limits load after auto-run may already have started a recipe"
    # and they must be DEFINED above the block, or this is a NameError at import
    assert src.index("def _load_limits") < src.index("if _project_root:\n    # The project root holds")


def test_r19_shutdown_waits_for_an_acquisition_before_releasing_spec():
    c = make_controller()
    c.beamline._collecting = True
    t0 = time.time()
    c.shutdown(collect_wait_s=0.4)
    assert time.time() - t0 >= 0.35, "shutdown did not wait at all"
    c.beamline._collecting = False


def test_r19_shutdown_idles_the_pumps_first_whatever_else_happens():
    """Ordering is the point: the pumps must be idled even if the beamline
    calls throw."""
    c = make_controller()
    c.submit(recipe("r190"))
    r, sp = c.queue.popleft()
    c._pending = None
    c._start_recipe(r, sp)
    c._enter_running()
    assert any(p.target > 0 for p in c.pumps.pumps.values())

    class Broken:
        def is_collecting(self): raise RuntimeError("bServer gone")
        def close_shutter(self): raise RuntimeError("bServer gone")
        def close(self): raise RuntimeError("bServer gone")
    c.beamline = Broken()
    c.shutdown(collect_wait_s=0.0)
    assert all(p.target == 0 for p in c.pumps.pumps.values())


def test_r20_the_conditions_folder_is_checked_before_it_is_accepted():
    """The watcher CREATES its folder if missing, so a typo used to be accepted
    in silence: an empty directory appeared and the campaign stopped receiving
    conditions with no error anywhere."""
    src = _app_src()
    fn = src[src.index("def api_recipes_folder"):]
    fn = fn[:fn.index("@app.route", 10)]
    assert "is_dir()" in fn, "any string is still accepted as a conditions folder"
    assert "no such folder" in fn


def test_r22_status_is_shared_across_clients_not_rebuilt_per_stream():
    """status() takes the controller lock and calls is_collecting(); /api/stream
    rebuilt it twice a second for EVERY connected browser."""
    src = _app_src()
    assert "_status_cached" in src
    stream = src[src.index("def api_stream"):]
    assert "_ctrl.status()" not in stream[:stream.index("return Response")], \
        "the SSE loop still builds its own status per client"


def test_r23_the_dead_branches_are_gone():
    assert "if True:" not in (_ROOT / "src" / "reactor" / "controller.py").read_text()
    src = _app_src()
    assert 'elif etype == "fit.complete"' not in src


def test_r24_the_simulator_poni_is_not_hard_coded_to_one_machine():
    """An absolute path to one laptop makes every other machine fall back to
    synthetic geometry — and a wrong q-scale makes the recovered particle size
    wrong with nothing saying so."""
    cfg = yaml.safe_load((_ROOT / "reactor" / "config.yml").read_text())
    poni = str(cfg["spec"]["simulator"].get("poni", "") or "")
    assert not poni.startswith("/Users/"), \
        f"simulator.poni is hard-coded to {poni}"


def test_r25_set_project_refuses_a_path_that_is_not_there():
    src = _app_src()
    fn = src[src.index("def set_project"):]
    fn = fn[:fn.index("@app.route", 10)]
    assert "is_dir()" in fn and "not a folder" in fn


# ═════════════════════════════════════════════════════════════════════════════
# R4 / R18 — the settings and the slow leaks
# ═════════════════════════════════════════════════════════════════════════════
def test_r4_the_counter_refresh_is_throttled_without_slowing_the_read():
    """Two settings, two jobs — and the first fix conflated them.

    `read_refresh_cmd` is `ct 0.1`: it obeys sauto, so it may open the fast
    shutter, and it used to run once per READ — ~86,400 times a day on whatever
    was in the beam, between runs included. The first fix throttled the READ
    (1 s → 10 s), which did cut the dose but also slowed the over-temperature
    interlock to 10 s and turned the live temperature trace into a visible
    staircase, because `current` only moved once per sample.

    Reading is two HTTP GETs and costs nothing. Counting is the dose. So the
    read stays fast and the refresh is throttled on its own.

    THE SHIPPED VALUE IS THE OPERATOR'S CALL, and they chose the live trace:
    `refresh_min_interval_s: 0.0`, i.e. `ct 0.1` on every read, as it was
    originally. So this test does NOT assert a dose policy — asserting one
    would be this file overruling the person who owns the beamtime. What it
    asserts is that the choice is a real, wired, one-line change rather than
    an invisible default, which is what the finding was actually about.
    """
    cfg = yaml.safe_load((_ROOT / "reactor" / "config.yml").read_text())
    interval = float(cfg["temperature"]["read_interval_s"])

    assert interval <= 2.0, (
        f"read_interval_s is {interval:g}s — that is the interlock's reaction "
        f"time and the plot's resolution. Throttle refresh_min_interval_s "
        f"instead; reading is free.")
    assert "refresh_min_interval_s" in cfg["spec"], (
        "the dose knob is gone from the config, so the only way to cut the "
        "counting is to slow the read again — which is the mistake this "
        "setting exists to prevent")


def test_r4_the_driver_actually_honours_the_refresh_throttle():
    """The config value has to be wired, not just documented."""
    import src.beamline.driver as drv
    src = (_ROOT / "src" / "beamline" / "driver.py").read_text()
    # SpecBeamline's, not the base class's one-line stub — which is what the
    # first version of this slice picked up.
    spec_cls = src[src.index("class SpecBeamline"):]
    fn = spec_cls[spec_cls.index("def _do_read_counters"):]
    fn = fn[:fn.index("\n    def ", 10)]
    assert "refresh_min_interval_s" in fn, \
        "the refresh is not throttled — it still runs on every read"
    assert "_last_refresh" in fn
    assert drv._DEFAULTS.get("refresh_min_interval_s", 0) > 0


def test_r4_a_fast_read_does_not_mean_a_fast_count():
    """The behaviour itself: many reads, few counts."""
    from src.beamline.driver import SpecBeamline
    counted, read = [], []

    bl = SpecBeamline.__new__(SpecBeamline)          # no HTTP session needed
    bl.cfg = {"read_refresh_cmd": "ct 0.1", "refresh_min_interval_s": 10.0,
              "temp_counter": "CTEMP", "bstop_counter": "bstop", "i0_counter": "i0"}
    bl._collecting = False
    bl._last_refresh = 0.0
    bl._cmd = lambda c: counted.append(c)
    bl._wait = lambda **kw: None
    bl._sis = lambda c, **kw: (read.append(c), ["CTEMP"] if "mnemonics" in c else [25.0])[1]

    for _ in range(20):                               # 20 reads back to back
        bl._do_read_counters()

    assert len(counted) == 1, (
        f"20 reads fired {len(counted)} counting commands; the throttle is not "
        f"working")
    assert len(read) == 40, "the reads themselves were throttled too"


def test_r4_the_shutter_risk_is_on_the_pre_beamtime_checklist():
    doc = (_ROOT / "docs" / "audits" / "PRE_BEAMTIME_READINESS.md").read_text()
    assert "sauto off" in doc and "dose" in doc.lower()


def test_r18_the_run_history_is_bounded():
    c = make_controller()
    assert c.history.maxlen, "history is still an unbounded list"
    for i in range(c.history.maxlen + 25):
        c.history.append({"recipe_id": f"r{i}"})
    assert len(c.history) == c.history.maxlen


def test_r18_the_mock_collection_log_is_bounded():
    c = make_controller()
    assert getattr(c.beamline.collections, "maxlen", None), \
        "MockBeamline.collections is still an unbounded list"


# ═════════════════════════════════════════════════════════════════════════════
# R21 — a temperature command that failed used to be silent
# ═════════════════════════════════════════════════════════════════════════════
def test_r21_a_failed_temperature_command_is_reported():
    """It was swallowed with a bare `pass`. In TIMED arming the pumps then
    start regardless, at whatever temperature the reactor happens to be — and
    the end-of-run cooldown and the vent-to-zero failed just as quietly."""
    said_lines: list[tuple[str, str]] = []
    cfg = copy.deepcopy(load_config())

    class Broken:
        def set_temperature(self, T): raise RuntimeError("bServer refused")
        def is_collecting(self): return False
        def collect_overrun_s(self): return 0.0

    tc = TempController(cfg, backend="real", beamline=Broken(),
                        log=lambda m, t="info": said_lines.append((t, m)))
    ok = tc.set_temperature(240.0)
    assert ok is False, "a failed command still reports success"
    assert said_lines and said_lines[0][0] == "error"
    assert "TEMPERATURE COMMAND FAILED" in said_lines[0][1]
    assert tc.target == 240.0, "the target must still be recorded for the UI"


def test_r21_a_working_temperature_command_stays_quiet():
    c = make_controller()
    c.logs.clear()
    assert c.temp.set_temperature(200.0) is True
    assert not any(t == "error" for t, _ in c.logs)


# ═════════════════════════════════════════════════════════════════════════════
# R26 — turning autonomous mode OFF did not stop the reactor taking work
# ═════════════════════════════════════════════════════════════════════════════
# Reported by the operator: "when i stop run autonomously is stopped then also
# run is not stop accepting the new conditions."
#
# Correct, and worse than it sounds. The folder watcher runs regardless of the
# toggle — by design, the toggle decides whether a recipe STARTS — but it also
# MOVED each file into done/ the instant it parsed it. So with autonomous mode
# off the reactor kept swallowing conditions out of the watched folder, and the
# only record of them was an in-memory queue. An app restart lost them from
# both places, silently.
#
# The operator's call (asked, not assumed): keep queueing so the queue can be
# reviewed and started by hand, but DO NOT consume the file until the reactor
# is actually finished with the condition. The file is the durable queue.
import importlib.util as _u


def _boot_app(tmp_path, monkeypatch):
    """Import reactor/app.py against a throwaway project folder."""
    # exist_ok: the R28 tests seed leftover condition files BEFORE booting, so
    # the folder is already there by the time we get here.
    conds = tmp_path / "1D" / "SAXS" / "Conditions"
    conds.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    spec = _u.spec_from_file_location(
        f"reactor_app_{tmp_path.name}", _ROOT / "reactor" / "app.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ctrl.cfg.setdefault("spec", {})["enabled"] = False
    mod._ctrl._spec_enabled = False
    return mod, conds


def _drop(folder: Path, rid: str) -> None:
    (folder / f"{rid}.txt").write_text(
        "T_reac = 240\nF_tot = 80\nx_ODE = 0.2\nx_TOP = 0.1\nx_oley = 0.1\n")


def _txt(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.txt")) if folder.is_dir() else []


def _wait_queue(ctrl, n: int, timeout: float = 40.0) -> list[str]:
    """Wait until the folder watcher has queued `n` conditions.

    A flat nine-second sleep was enough for the 3 s poll when these tests ran
    alone, and not when the suite ran them alongside everything else — they
    passed in isolation and failed in the batch, which is the worst kind of
    test to leave behind. Poll for the outcome instead of guessing how long
    the machine will take. Also takes about 40 s off this file.
    """
    end = time.time() + timeout
    while time.time() < end:
        if len(ctrl.queue) >= n:
            time.sleep(0.3)               # let the watcher finish its pass
            break
        time.sleep(0.2)
    return list(ctrl.status()["queue"])


def test_r26_conditions_are_queued_but_their_files_are_not_consumed(tmp_path, monkeypatch):
    """The operator's report. Autonomous mode off: still queued (so it can be
    reviewed and started by hand) but the FILES STAY, so nothing is lost."""
    mod, conds = _boot_app(tmp_path, monkeypatch)
    try:
        assert mod._ctrl.auto_run is False
        for rid in ("r001", "r002", "r003"):
            _drop(conds, rid)
        _wait_queue(mod._ctrl, 3)

        assert list(mod._ctrl.status()["queue"]) == ["r001", "r002", "r003"]
        assert _txt(conds) == ["r001.txt", "r002.txt", "r003.txt"], \
            "the watcher consumed the files with autonomous mode off"
        assert _txt(conds / "done") == [], \
            "files were retired before the reactor had finished with them"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r26_the_intake_order_is_deterministic(tmp_path, monkeypatch):
    """Oldest first is the rule, and it held. What did NOT hold was the TIE.

    The optimizer can easily write several conditions in the same instant, and
    sorting on mtime alone then left the order to whatever the filesystem
    happened to return — the probe that found this got r001, r003, r002 from
    three files stamped identically. A campaign must run the proposals in the
    order they were proposed, so the filename is now the tie-break.

    The mtimes are forced equal here; writing three files in a loop is not a
    reliable way to collide them, and a test that only sometimes exercises the
    thing it is named for is worse than none."""
    mod, conds = _boot_app(tmp_path, monkeypatch)
    try:
        for rid in ("r003", "r001", "r002"):      # created out of order …
            _drop(conds, rid)
        stamp = time.time() - 60
        for p in conds.glob("*.txt"):             # … and stamped identically
            os.utime(p, (stamp, stamp))
        _wait_queue(mod._ctrl, 3)
        assert list(mod._ctrl.status()["queue"]) == ["r001", "r002", "r003"], \
            "identical timestamps still leave the order to the filesystem"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r26_a_condition_that_runs_has_its_file_retired(tmp_path, monkeypatch):
    """The other half — the file must not stay forever, or it is re-ingested on
    the next restart and the condition runs twice."""
    mod, conds = _boot_app(tmp_path, monkeypatch)
    try:
        _drop(conds, "r010")
        _wait_queue(mod._ctrl, 1)
        assert _txt(conds) == ["r010.txt"]

        r, sp = mod._ctrl.queue.popleft()
        mod._ctrl._pending = None
        mod._ctrl._start_recipe(r, sp)
        mod._ctrl._enter_running()
        mod._ctrl._run_reason = "test"
        mod._ctrl._end_run(flush=False)
        time.sleep(0.4)

        assert _txt(conds) == [], "the file was not retired after the run"
        assert _txt(conds / "done") == ["r010.txt"]
        body = (conds / "done" / "r010.txt").read_text()
        assert "RESULT (measured" in body, "the delivered-flow footer is missing"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r26_an_abandoned_condition_has_its_file_retired_too(tmp_path, monkeypatch):
    """An arm timeout is 'finished with' as much as a completed run is. Leaving
    the file would re-run a condition that already failed to arm."""
    mod, conds = _boot_app(tmp_path, monkeypatch)
    try:
        _drop(conds, "r020")
        _wait_queue(mod._ctrl, 1)
        r, sp = mod._ctrl.queue.popleft()
        mod._ctrl._pending = None
        mod._ctrl._start_recipe(r, sp)
        mod._ctrl._abandon_condition("r020", "arm timeout")
        time.sleep(0.4)

        assert _txt(conds) == []
        body = (conds / "done" / "r020.txt").read_text()
        assert "NOT RUN" in body and "arm timeout" in body, \
            "the file does not say why it never ran"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r26_clear_queue_actually_clears(tmp_path, monkeypatch):
    """Because the file now outlives the queue entry, Clear queue has to retire
    the files as well — otherwise the next restart re-reads them and the button
    did nothing durable."""
    mod, conds = _boot_app(tmp_path, monkeypatch)
    try:
        for rid in ("r030", "r031"):
            _drop(conds, rid)
        _wait_queue(mod._ctrl, 2)
        assert len(mod._ctrl.queue) == 2

        client = mod.app.test_client()
        resp = client.post("/api/queue/clear", json={})
        assert resp.get_json()["cleared"] == 2

        assert list(mod._ctrl.status()["queue"]) == []
        assert _txt(conds) == [], "cleared conditions would be re-ingested on restart"
        assert _txt(conds / "done") == ["r030.txt", "r031.txt"]
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r26_clear_queue_reports_what_it_removed():
    """It returned a bare count, so the caller could not retire the files."""
    c = make_controller()
    c.submit(recipe("r040"))
    c.submit(recipe("r041"))
    removed = c.clear_queue()
    assert [d["recipe_id"] for d in removed] == ["r040", "r041"]
    assert all("source" in d for d in removed), \
        "without the source the app cannot find the file to retire"


def test_r26_the_ui_says_conditions_are_waiting_while_autonomous_is_off():
    """Silence here is how the queue grows unnoticed, which is the shape of the
    original complaint."""
    tpl = (_ROOT / "reactor" / "templates" / "index.html").read_text()
    assert 'id="queueNote"' in tpl
    assert "autonomous mode is OFF" in tpl


# ═════════════════════════════════════════════════════════════════════════════
# R27 — "Stop autonomous" did not stop the autonomous loop
# ═════════════════════════════════════════════════════════════════════════════
# The operator's clarification of R26, and the bigger half of it:
#
#   "when i stop autonomous, I just want to pause the autonomous after the
#    current conditions, then flushing. Keep queuing the new condition only.
#    at this time i would like change the beamline frame time wait time input
#    and set once autonomous run is started again and it take the new
#    conditions."
#
# `auto_run` was read in exactly two places — at intake (submit) and on the
# re-arm — and NEVER by the loop itself. So once a campaign was rolling,
# _end_flush → _begin_next → _end_run → _end_flush → … chained through the
# whole queue no matter what the toggle said. Turning it off mid-campaign
# changed one thing only: a newly arriving condition no longer auto-started if
# the reactor happened to be idle at that instant. The rig kept going.

def _armed_and_running(c, rid="r001", extra=()):
    """Get to 'r001 is synthesising' with auto-run on and `extra` queued."""
    c.set_auto_run(True)
    c.submit(recipe(rid))
    for e in extra:
        c.submit(recipe(e))
    c._end_flush()                 # blank done -> arming
    assert c.state == "arming"
    c._enter_running()
    assert c.state == "running"
    return c


def test_r27_stopping_autonomous_lets_the_current_condition_finish():
    """Explicitly NOT an abort. Nothing is interrupted."""
    c = make_controller()
    _armed_and_running(c, "r001", extra=("r002",))
    c.set_auto_run(False)
    assert c.state == "running", "stopping autonomous interrupted the run"
    assert c.pausing() is True
    assert c.status()["pausing"] is True


def test_r27_the_loop_pauses_after_that_conditions_flush():
    """The whole point: run → flush → STOP, with the queue kept."""
    c = make_controller()
    _armed_and_running(c, "r001", extra=("r002", "r003"))
    c.set_auto_run(False)

    c._run_reason = "test"
    c._end_run(flush=True)
    assert c.state == "flushing"
    c._end_flush()

    assert c.state == "ready", f"the loop carried on into {c.state}"
    assert c.current is None
    assert list(c.status()["queue"]) == ["r002", "r003"], "the queue was not kept"
    assert all(p.target == 0 for p in c.pumps.pumps.values()), "pumps left flowing"
    assert c.status()["paused_with_queue"] is True
    assert said(c, "PAUSED"), "the pause was silent"


def test_r27_a_paused_flush_does_not_stage_the_next_conditions_blank():
    """With the loop paused, staging the next blank would pair a sample with a
    background taken BEFORE the operator changed the exposure — which is the
    very thing they stop the loop to do."""
    c = make_controller()
    _armed_and_running(c, "r001", extra=("r002",))
    c.set_auto_run(False)
    c._run_reason = "test"
    c._end_run(flush=True)
    assert c._flush_kind == "flush", "a blank was staged while paused"
    assert c._pending is None
    assert c._bkg_recipe_id == ""


def test_r27_the_data_collection_settings_unlock_while_paused():
    """The reason the operator wants the pause. Frozen during a campaign by
    design (they define what an acquisition IS); editable the moment the loop
    has actually stopped."""
    c = make_controller()
    _armed_and_running(c, "r001", extra=("r002",))
    c.set_auto_run(False)
    assert c.status()["spec"]["locked"] is True, "settings unlocked mid-run"

    c._run_reason = "test"
    c._end_run(flush=True)
    c._end_flush()

    assert c.status()["spec"]["locked"] is False, \
        "still frozen after the loop paused — the operator cannot change anything"
    ok, msg = c.set_spec_settings({"exposure_s": "20", "frames": "5",
                                   "spec_lead_s": "60"})
    assert ok, msg
    assert (c._spec_exposure, c._spec_frames, c._spec_lead) == (20.0, 5, 60.0)


def test_r27_resuming_uses_the_new_settings_and_the_kept_queue():
    """End to end, the operator's sentence."""
    c = make_controller()
    _armed_and_running(c, "r001", extra=("r002", "r003"))
    c.set_auto_run(False)
    c._run_reason = "test"
    c._end_run(flush=True)
    c._end_flush()
    c.set_spec_settings({"exposure_s": "20", "frames": "5", "spec_lead_s": "60"})

    c.logs.clear()
    c.set_auto_run(True)

    assert c.state == "flushing" and c._flush_kind == "blank", \
        "resuming did not pick the queue back up"
    assert c._bkg_recipe_id == "r002", "resumed on the wrong condition"
    assert list(c.status()["queue"]) == ["r003"]
    assert (c._spec_exposure, c._spec_frames, c._spec_lead) == (20.0, 5, 60.0), \
        "the new acquisition settings were not carried into the resumed run"
    assert said(c, "resuming with 2 queued"), \
        "resuming did not say what it was about to do, or with what settings"


def test_r27_conditions_still_queue_while_paused():
    """'Keep queuing the new condition only.' Intake is unaffected by the
    pause — it just must not START anything."""
    c = make_controller()
    _armed_and_running(c, "r001")
    c.set_auto_run(False)
    c._run_reason = "test"
    c._end_run(flush=True)
    c._end_flush()
    assert c.state == "ready"

    c.submit(recipe("r050"))          # arrives while paused
    assert list(c.status()["queue"]) == ["r050"]
    assert c.state == "ready", "a condition arriving while paused started itself"


def test_r27_start_runs_exactly_one_condition_while_paused():
    """Manual mode falls out of the same change: Start takes one, and the loop
    stops again after its flush instead of running away with the queue."""
    c = make_controller()
    c.submit(recipe("r060"))
    c.submit(recipe("r061"))
    assert c.auto_run is False

    c.start()
    c._end_flush()                      # blank -> arming r060
    c._enter_running()
    c._run_reason = "test"
    c._end_run(flush=True)
    c._end_flush()

    assert c.state == "ready"
    assert list(c.status()["queue"]) == ["r061"], \
        "Start ran on past the one condition it was asked for"


def test_r27_turning_it_off_says_when_the_pause_takes_effect():
    """'OFF' alone reads as 'stopping now' while the rig has ten more minutes
    of synthesis in front of it."""
    c = make_controller()
    _armed_and_running(c, "r001")
    c.logs.clear()
    c.set_auto_run(False)
    assert said(c, "will FINISH"), "the operator is not told the pause is deferred"
    assert said(c, "Nothing is interrupted")


def test_r27_the_ui_distinguishes_pausing_from_paused_from_running():
    tpl = (_ROOT / "reactor" / "templates" / "index.html").read_text()
    assert "st.pausing" in tpl, "the UI cannot tell 'pausing' from 'off'"
    assert "Pausing after this condition" in tpl
    assert "st.paused_with_queue" in tpl


# ═════════════════════════════════════════════════════════════════════════════
# R28 — a restart starts with an empty queue
# ═════════════════════════════════════════════════════════════════════════════
# Operator: "i would like to clear the queue automatically if reactor app was
# stopped from hub and restarted it."
#
# This is the deliberate counterpart to R26. Condition files stay in the
# watched folder until the reactor has finished with them, which makes a queue
# survive a crash — and means a restart would otherwise inherit whatever the
# optimizer had proposed before the stop. Those proposals are stale: they were
# computed against the data available then.
#
# Clearing the in-memory queue alone achieves nothing, because the watcher
# re-reads the same files within one poll. The FILES have to be set aside.
# Nothing is deleted.

def test_r28_a_restart_sets_aside_conditions_left_over_from_before(tmp_path, monkeypatch):
    conds = tmp_path / "1D" / "SAXS" / "Conditions"
    conds.mkdir(parents=True)
    for rid in ("r001", "r002"):
        _drop(conds, rid)

    mod, _ = _boot_app(tmp_path, monkeypatch)
    try:
        assert list(mod._ctrl.status()["queue"]) == [], "the restart inherited the queue"
        assert _txt(conds) == [], "the leftover files are still waiting to be read"
        assert _txt(conds / "done") == ["r001.txt", "r002.txt"]
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r28_nothing_is_deleted_and_the_file_says_why(tmp_path, monkeypatch):
    """A cleared condition has to be recoverable — move it back and it runs."""
    conds = tmp_path / "1D" / "SAXS" / "Conditions"
    conds.mkdir(parents=True)
    _drop(conds, "r010")

    mod, _ = _boot_app(tmp_path, monkeypatch)
    try:
        body = (conds / "done" / "r010.txt").read_text()
        assert "T_reac = 240" in body, "the recipe itself was not preserved"
        assert "NOT RUN" in body
        assert "clear_queue_on_restart" in body, \
            "the file does not say which setting cleared it"
        assert "Move this file back" in body, "no route back for the operator"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r28_it_is_announced_not_silent(tmp_path, monkeypatch):
    """Two conditions vanishing at start-up must not be a quiet event.

    This assertion is why the first version of the feature was caught: it
    referenced _watch_handled, which was declared FURTHER DOWN the module than
    the start-up helper that used it, so the NameError went into a broad
    `except` and the log said "could not clear leftover conditions" while the
    files had in fact already been moved. The success line never printed."""
    conds = tmp_path / "1D" / "SAXS" / "Conditions"
    conds.mkdir(parents=True)
    for rid in ("r020", "r021"):
        _drop(conds, rid)

    mod, _ = _boot_app(tmp_path, monkeypatch)
    try:
        lines = [e["msg"] for _s, e in mod._log]
        assert any("empty queue" in m for m in lines), \
            "the clear-out was silent, or it failed and reported a warning"
        assert not any("could not clear" in m for m in lines)
        assert any("set aside 2 leftover" in m for m in lines), \
            "the count is not in the log"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r28_a_condition_arriving_after_startup_is_still_picked_up(tmp_path, monkeypatch):
    """Only the leftovers are cleared. The watcher must carry on normally, or
    this feature would stop the campaign rather than resetting it."""
    mod, conds = _boot_app(tmp_path, monkeypatch)
    try:
        _drop(conds, "r030")
        assert _wait_queue(mod._ctrl, 1) == ["r030"]
        assert _txt(conds) == ["r030.txt"], "a fresh condition was set aside too"
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r28_the_behaviour_can_be_turned_off(tmp_path, monkeypatch):
    """`run.clear_queue_on_restart: false` restores crash-resume."""
    conds = tmp_path / "1D" / "SAXS" / "Conditions"
    conds.mkdir(parents=True)
    _drop(conds, "r040")

    mod, _ = _boot_app(tmp_path, monkeypatch)
    try:
        mod._CFG.setdefault("run", {})["clear_queue_on_restart"] = False
        _drop(conds, "r041")                      # a second leftover
        assert mod._clear_stale_conditions() == 0, \
            "the setting is not honoured — leftovers are cleared regardless"
        assert "r041.txt" in _txt(conds)
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_r28_the_shipped_default_is_on():
    cfg = yaml.safe_load((_ROOT / "reactor" / "config.yml").read_text())
    assert cfg["run"]["clear_queue_on_restart"] is True


def test_r28_no_function_in_the_reactor_app_is_defined_twice():
    """Guard for a mistake made while writing this feature: I added
    _clear_stale_conditions twice — two different bodies, the second silently
    winning — and only noticed because the surviving copy used a different
    config key than the one I had put in config.yml. Python does not complain
    about a redefinition, and neither does any linter in this repo's config."""
    src = (_ROOT / "reactor" / "app.py").read_text()
    names = re.findall(r"^def (\w+)", src, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"defined more than once in reactor/app.py: {dupes}"


def test_r28_startup_helpers_do_not_reference_state_declared_below_them():
    """The other half of that mistake, generalised. Anything the module calls
    DURING execution can only use state declared above it; getting this wrong
    raises NameError into whatever except-block happens to be nearby."""
    src = (_ROOT / "reactor" / "app.py").read_text()
    for name in ("_watch_handled", "_watch_lastsig"):
        decl = re.search(rf"^{name}: dict", src, re.M)
        assert decl, f"{name} is no longer declared at module level"
        for fn in ("_retire_condition_file", "_clear_stale_conditions"):
            use = src.index(f"def {fn}")
            assert decl.start() < use, \
                f"{name} is declared after {fn}, which touches it"


# ═════════════════════════════════════════════════════════════════════════════
# R29 — beamline data-collection settings persist across a restart
# ═════════════════════════════════════════════════════════════════════════════
# Operator request. Run settings (arm_mode, run_duration, flush_*) were restored
# on restart (R7-adjacent); the SPEC data-collection settings — exposure, frames,
# trigger-before-end, tags, save folder — were IN-MEMORY ONLY, so exposure 20s×5
# reverted to config.yml's 10s×10 on the next start with the UI none the wiser.

def test_r29_spec_settings_survive_a_restart(tmp_path, monkeypatch):
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")

    def boot(tag):
        import importlib.util as u
        spec = u.spec_from_file_location(f"reactor_app_r29_{tag}", _ROOT / "reactor" / "app.py")
        mod = u.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    m = boot("a")
    try:
        r = m.app.test_client().post("/api/spec_settings", json={
            "exposure_s": "20", "frames": "5", "spec_lead_s": "60",
            "sample_tag": "smp", "bkg_tag": "blank"})
        assert r.get_json()["ok"] is True
    finally:
        m._ctrl.shutdown(collect_wait_s=0.0)

    m2 = boot("b")                                    # the "restart"
    try:
        sp = m2._ctrl.status()["spec"]
        assert (sp["exposure_s"], sp["frames"], sp["spec_lead_s"]) == (20.0, 5, 60.0)
        assert (sp["sample_tag"], sp["bkg_tag"]) == ("smp", "blank")
    finally:
        m2._ctrl.shutdown(collect_wait_s=0.0)


def test_r29_a_refused_spec_change_is_not_persisted(tmp_path, monkeypatch):
    """A 409 (settings frozen mid-campaign, or exposure 0) must not become the
    stored value — only what the controller accepted is saved."""
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    import importlib.util as u
    spec = u.spec_from_file_location("reactor_app_r29c", _ROOT / "reactor" / "app.py")
    m = u.module_from_spec(spec); spec.loader.exec_module(m)
    try:
        m._ctrl.set_auto_run(True)                    # freezes spec settings
        r = m.app.test_client().post("/api/spec_settings", json={"exposure_s": "99"})
        assert r.status_code == 409
        from src.runstate import load_state
        assert not load_state(str(tmp_path), "reactor_spec_settings",
                              honour_no_resume=False), "a refused change was saved"
    finally:
        m._ctrl.shutdown(collect_wait_s=0.0)


def test_r29_the_restore_banner_lists_the_beamline_params_too(tmp_path, monkeypatch):
    """Operator request: the '♻ settings restored' banner listed only the run
    settings (arm_mode, run_duration, flush_*). The beamline settings were
    restored silently. The banner must name them too, in the one list."""
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    import importlib.util as u

    def boot(tag):
        spec = u.spec_from_file_location(f"reactor_app_r29b_{tag}", _ROOT / "reactor" / "app.py")
        mod = u.module_from_spec(spec); spec.loader.exec_module(mod); return mod

    m = boot("a"); c = m.app.test_client()
    try:
        c.post("/api/run_settings", json={"arm_mode": "temperature",
               "arm_wait_s": "120", "run_duration": "60", "flush_rate": "50",
               "flush_duration": "60", "flush_pump": "ode_dilution"})
        c.post("/api/spec_settings", json={"exposure_s": "20", "frames": "5",
               "spec_lead_s": "60", "sample_tag": "smp", "bkg_tag": "blank"})
    finally:
        m._ctrl.shutdown(collect_wait_s=0.0)

    m2 = boot("b")
    try:
        n = m2.app.test_client().get("/api/restart_notice").get_json()
        assert n["level"] == "restored"
        for p in ("arm_mode", "run_duration", "flush_rate",          # run
                  "exposure_s", "frames", "spec_lead_s"):            # beamline
            assert p in n["params"], f"{p} missing from the restore banner"
        assert "data-collection" in n["message"] or "beamline" in n["message"]
    finally:
        m2._ctrl.shutdown(collect_wait_s=0.0)
