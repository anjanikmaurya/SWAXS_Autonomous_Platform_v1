"""
tests/test_audit_undefined_and_logic.py — the platform-wide audit findings.

Every defect below was reproduced before being fixed. They share a theme: code
that *looks* correct, runs without complaint, and quietly does the wrong thing.

1. CRITICAL — `src/reactor/controller.py` had no `logger`, and the control-loop
   fault handler called `logger.exception(...)` BEFORE `self.estop()`. The
   NameError skipped the E-stop and escaped the handler, killing the supervisor
   thread with reagent pumps commanded and the heater on, while `_alive` stayed
   True so the controller reported itself healthy.
2. CRITICAL — one un-serialisable event payload permanently disabled an app's
   event bus. `mask.sum()` is `np.int64`; `json.dumps` refused it; `publish()`
   treated the encoding error as a transport failure and dropped the socket. The
   socket was still open, so the reconnect loop never fired and every later event
   was silently discarded — including `file.averaged`, which is what advances the
   autonomous campaign.
3. CRITICAL — `reduction/app.py` referenced an undefined `_project_root`, so its
   monitor state was never saved and never resumed. Reduction alone did not come
   back after a restart, while every other app did.
4. HIGH — the quality gate's LLM adjudication was overwritten one line after
   being computed, so it changed nothing and every paid call was wasted.
5. HIGH — `guinier_quality` read `"r2"` but `guinier_fit` returns `"R2"`, making
   the R² gate unreachable: a fit with R² = 0.42 was reported PASS.
6. HIGH — `_scalar_results` dropped bools, discarding `converged` and
   `at_bounds`: a railed fit was persisted as a clean number.
7. HIGH — `stage.capitalize()` turned "NeedsReview" into "Needsreview" and could
   not address `Subtracted/Good` at all, so batch analysis silently ran over the
   unfiltered folder.
"""
from __future__ import annotations

import importlib.util as u
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(tag: str, rel: str):
    spec = u.spec_from_file_location(tag, str(ROOT / rel))
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


# ── 1. the reactor control loop ──────────────────────────────────────────────
def test_controller_module_has_a_logger():
    """The absence of this name was a safety defect, not a cosmetic one."""
    from src.reactor import controller
    assert getattr(controller, "logger", None) is not None


def test_a_control_loop_fault_estops_and_the_supervisor_survives():
    pytest.importorskip("yaml")
    from src.reactor import ReactorController, load_config
    cfg = load_config()
    cfg.setdefault("spec", {})["backend"] = "mock"
    c = ReactorController(cfg)
    try:
        calls = []
        real_estop = c.estop
        c.estop = lambda *a, **k: (calls.append(1), real_estop(*a, **k))[1]

        n = {"i": 0}

        def transient():
            n["i"] += 1
            if n["i"] <= 2:
                raise RuntimeError("serial port vanished")

        c._tick_once = transient
        end = time.time() + 10
        while time.time() < end and c.status()["loop_faults"] < 1:
            time.sleep(0.1)

        assert calls, "a control-loop fault did not trigger the emergency stop"
        st = c.status()
        assert st["loop_faults"] >= 1
        assert st["last_fault"] and "serial port" in st["last_fault"]
        # and the supervisor must still be supervising
        assert c._thread.is_alive(), "the control loop thread died on a fault"
        assert st["supervising"] is True
    finally:
        c.shutdown()


def _code_only(text: str) -> str:
    """Drop comment lines. Without this, an assertion can match the very comment
    that explains the bug it is guarding against."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def test_the_estop_runs_before_anything_that_could_fail():
    """Ordering is the whole fix: logging first is what skipped the E-stop."""
    src = (ROOT / "src" / "reactor" / "controller.py").read_text()
    body = src[src.index("    def _loop(self)"):src.index("    def _tick_once(self)")]
    handler = _code_only(body[body.index("except Exception as exc:"):])
    assert handler.index("self.estop()") < handler.index("logger.exception"), \
        "logging happens before the emergency stop again"


def test_status_distinguishes_a_dead_supervisor_from_a_healthy_one():
    pytest.importorskip("yaml")
    from src.reactor import ReactorController, load_config
    cfg = load_config()
    cfg.setdefault("spec", {})["backend"] = "mock"
    c = ReactorController(cfg)
    try:
        assert c.status()["supervising"] is True
        c._alive = False                       # simulate the loop stopping
        c._thread.join(timeout=3)
        assert c.status()["supervising"] is False, \
            "a controller with no live loop still reports that it is supervising"
    finally:
        c.shutdown()


# ── 2. the event bus ─────────────────────────────────────────────────────────
def _fake_bus():
    from src import events as ev
    c = ev.EventBusClient.__new__(ev.EventBusClient)
    c._app_id = "viewer"
    c._connected = True
    sent = []

    class WS:
        def send(self, m):
            sent.append(m)

    c._ws = WS()
    return c, sent


def test_a_numpy_payload_no_longer_kills_the_event_bus():
    c, sent = _fake_bus()
    ok = c.publish("file.averaged", {"n_files": np.array([True, False, True]).sum(),
                                     "curve": np.arange(3)})
    assert ok is True, "a numpy scalar still fails to publish"
    assert c._connected is True, "the connection was dropped for an encoding error"
    assert c._ws is not None
    data = json.loads(sent[0])["data"]
    assert data["n_files"] == 2 and data["curve"] == [0, 1, 2]


def test_an_unencodable_payload_drops_the_event_but_keeps_the_connection():
    c, sent = _fake_bus()

    class Hostile:
        def __repr__(self): raise RuntimeError("no")
        __str__ = __repr__

    ok = c.publish("x", {"bad": Hostile()})
    assert ok is False and sent == []
    assert c._connected is True, "one bad payload disabled the bus again"
    # and the next good event still goes out
    assert c.publish("x", {"n": 1}) is True


def test_the_viewer_reports_the_real_frame_count():
    """`mask.sum()` was also the WRONG number — positive q-points, not frames."""
    v = (ROOT / "viewer" / "app.py").read_text()
    assert "n_files  = mask.sum()" not in v and "n_files=mask.sum()" not in v
    m = _load("viewer_frames", "viewer/app.py")
    assert m._frames_from_name("sample_A_batch003_30files_Average.dat") == 30
    assert m._frames_from_name("sample_A_12files_Average.dat") == 12
    assert m._frames_from_name("no_count_here.dat") == 0


def test_a_blank_detector_folder_is_skipped_not_globbed_from_the_cwd():
    """`Path("")` is the CWD, which exists — so the "not found" guard was dead and
    a SAXS-only run averaged whatever .dat files sat in the launch directory."""
    v = (ROOT / "viewer" / "app.py").read_text()
    seg = _code_only(v[v.index('for det, raw in [("saxs", body.get("saxs_folder"'):][:900])
    assert 'Path("")' not in seg, "Path('') is back — blank means the CWD again"
    assert 'if not (raw or "").strip():' in seg


# ── 3. reduction's restart resume ────────────────────────────────────────────
def test_reduction_state_root_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    m = _load("red_state", "reduction/app.py")
    got = m._state_root()               # used to raise NameError
    assert got == str(tmp_path)


def test_reduction_persists_its_monitor_state(tmp_path, monkeypatch):
    """The consequence of the NameError: save_monitor was never reached, so
    reduction was the one app that did not come back after a restart."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    (tmp_path / "2D" / "SAXS").mkdir(parents=True)
    m = _load("red_persist", "reduction/app.py")
    from src.runstate import save_monitor, load_monitor
    save_monitor(m._state_root(), "reduction", True, {"interval": 5})
    assert (tmp_path / ".swaxs_state" / "reduction_monitor.json").is_file(), \
        "reduction still writes its monitor state nowhere"
    # SWAXS_NO_RESUME is set above (so importing the app does not start a
    # monitor); lift it just for the read-back.
    monkeypatch.delenv("SWAXS_NO_RESUME", raising=False)
    assert load_monitor(m._state_root(), "reduction") == {"interval": 5}


# ── 4. the quality gate honours its own adjudication ─────────────────────────
def test_the_llm_verdict_is_no_longer_thrown_away(tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    q = _load("ql_verdict", "quality/app.py")
    rec = {"path": "/x/a.dat", "score": 40.0}          # below any sane threshold
    assert q._effective_verdict(rec) == "bad"
    rec["llm_verdict"] = "good"
    assert q._effective_verdict(rec) == "good", \
        "the LLM adjudication is still overwritten by the plain threshold"
    q._overrides["/x/a.dat"] = {"verdict": "bad"}
    assert q._effective_verdict(rec) == "bad", "a human override must still win"


def test_grading_is_memoised_by_size_and_mtime(tmp_path, monkeypatch):
    """It re-graded every profile every cycle — each one a full locked
    read-modify-write of manifest.json, plus an LLM call if borderline."""
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    q = _load("ql_memo", "quality/app.py")
    src = (ROOT / "quality" / "app.py").read_text()
    loop = src[src.index("def _grader_loop"):]
    assert "always reprocess" not in loop, "the unconditional re-grade is back"
    assert "_graded.get(rp) == sig" in loop, "no size+mtime memo"
    assert callable(q._invalidate_grades)
    q._graded["/x"] = (1, 2)
    q._invalidate_grades()
    assert q._graded == {}, "changing the rules must force a re-grade"


def test_changing_the_rules_invalidates_the_memo():
    """Otherwise every already-graded profile stays pinned to a verdict computed
    under the old threshold."""
    src = (ROOT / "quality" / "app.py").read_text()
    for fn in ("_recolor", "_rescore_all", "_adapt_threshold"):
        body = src[src.index(f"def {fn}()"):]
        body = body[:body.index("\ndef ", 5)]
        assert "_invalidate_grades()" in body, f"{fn} does not invalidate the memo"


# ── 5-6. analysis QC and persistence ─────────────────────────────────────────
def test_the_guinier_r2_gate_actually_fires():
    from src.analysis.core import guinier_quality
    out = guinier_quality({"Rg": 4.0, "R2": 0.42, "q_range": [0.05, 0.3], "I0": 10})
    assert any("R²" in w for w in out["warnings"]), \
        f"a Guinier fit with R²=0.42 still passes silently: {out}"
    assert out["verdict"] != "PASS"
    # a good fit must still pass
    good = guinier_quality({"Rg": 4.0, "R2": 0.999, "q_range": [0.05, 0.3], "I0": 10})
    assert not any("R²" in w for w in good["warnings"])


def test_fit_trust_flags_survive_being_saved():
    from src.analysis.io import _scalar_results
    out = _scalar_results({"radius": 4.1, "converged": False,
                           "at_bounds": ["radius"], "pdi": 0.1, "plot": "blob",
                           "curve": [1, 2, 3]})
    assert out["converged"] is False, "`converged` is dropped again"
    assert out["at_bounds"] == "radius", "`at_bounds` is dropped again"
    assert "plot" not in out and "curve" not in out, "arrays leaked into the record"


# ── 7. the analysis stage folders ────────────────────────────────────────────
def test_every_pipeline_stage_including_the_quality_gate_is_addressable(tmp_path,
                                                                       monkeypatch):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    for d in ("Reduction", "Averaged", "Subtracted",
              "Subtracted/Good", "Subtracted/NeedsReview"):
        (tmp_path / "1D" / "SAXS" / d).mkdir(parents=True, exist_ok=True)
    m = _load("an_stage", "analysis/app.py")
    m._project_root = str(tmp_path)

    def rel(stage):
        r = m._stage_dir("saxs", stage)
        return None if r is None else str(r.relative_to(tmp_path))

    assert rel("Subtracted") == "1D/SAXS/Subtracted"
    assert rel("Good") == "1D/SAXS/Subtracted/Good", \
        "the quality gate's accepted folder is still unaddressable"
    assert rel("NeedsReview") == "1D/SAXS/Subtracted/NeedsReview"
    assert rel("needs_review") == "1D/SAXS/Subtracted/NeedsReview", \
        "capitalize() mangling is back"
    assert rel("Averaged") == "1D/SAXS/Averaged"
    assert rel("Bogus") is None


def test_capitalize_is_not_used_to_build_a_stage_path():
    src = (ROOT / "analysis" / "app.py").read_text()
    body = src[src.index("def _stage_dir"):]
    body = body[:body.index("\ndef ", 5)]
    assert "stage.capitalize()" not in body


# ── the class of bug itself ──────────────────────────────────────────────────
def test_no_shipped_module_references_an_undefined_name():
    """A ruff F821 sweep. Three real defects in this audit were undefined names
    sitting on paths nobody had executed — including one in a safety handler."""
    import subprocess
    r = subprocess.run([sys.executable, "-m", "ruff", "check", "--select", "F821",
                        "--exclude", "tests", "--output-format", "concise", "."],
                       cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode not in (0, 1):
        pytest.skip("ruff not available: " + (r.stderr or "")[:200])
    offenders = [ln for ln in r.stdout.splitlines()
                 if ": F821 " in ln]
    assert not offenders, "undefined names in shipped code:\n" + "\n".join(offenders)


# ── 8. a dead monitor thread must not report a healthy monitor ───────────────
# Four apps kept a bare `_monitoring = True` that only the worker loop cleared.
# If that thread died, the flag stayed True: status reported healthy, the hub card
# stayed green, and /api/monitor/start refused with "Already monitoring" — so the
# app could not be recovered from the UI and nothing was processed for the rest of
# the night.
MONITOR_APPS = [
    ("reduction/app.py", "_monitoring", "_monitor_thread"),
    ("viewer/app.py", "_avg_monitoring", "_avg_monitor_thread"),
    ("background/app.py", "_sub_monitoring", "_sub_monitor_thread"),
    ("quality/app.py", "_grading", "_grader_thread"),
]


def test_monitor_alive_is_the_thread_not_the_flag():
    from src.runstate import monitor_alive

    class T:
        def __init__(self, a): self._a = a
        def is_alive(self): return self._a

    assert monitor_alive(True, T(True)) is True
    assert monitor_alive(True, T(False)) is False, "a dead thread reads as running"
    assert monitor_alive(True, None) is False
    assert monitor_alive(False, T(True)) is False


@pytest.mark.parametrize("rel,flag,thread", MONITOR_APPS)
def test_every_monitor_guard_and_status_use_thread_liveness(rel, flag, thread):
    src = (ROOT / rel).read_text()
    start = src[src.index('@app.route("/api/monitor/start"'):]
    start = start[:start.index('@app.route', 10)]
    assert f"monitor_alive({flag}, {thread})" in _code_only(start), \
        f"{rel}: the already-running guard still trusts the bare flag"
    status = src[src.index("def monitor_status"):]
    status = status[:status.index("\n@app.route", 5)]
    assert "monitor_alive(" in _code_only(status), \
        f"{rel}: /api/monitor/status still reports the bare flag"


@pytest.mark.parametrize("tag,rel,thread,body,dirs", [
    ("v_live", "viewer/app.py", "_avg_monitor_thread",
     {"interval": 1, "frames_per_average": 2,
      "saxs_folder": "1D/SAXS/Reduction", "waxs_folder": ""},
     ["1D/SAXS/Reduction"]),
    ("b_live", "background/app.py", "_sub_monitor_thread",
     {"interval": 1, "saxs_avg_folder": "1D/SAXS/Averaged"}, ["1D/SAXS/Averaged"]),
    ("q_live", "quality/app.py", "_grader_thread",
     {"interval": 1, "saxs_folder": "1D/SAXS/Subtracted", "waxs_folder": ""},
     ["1D/SAXS/Subtracted"]),
])
def test_a_killed_worker_is_reported_and_can_be_restarted(tmp_path, monkeypatch,
                                                          tag, rel, thread, body, dirs):
    import ctypes
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    for d in dirs:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    m = _load(tag, rel)
    c = m.app.test_client()
    b = {k: (str(tmp_path / v) if isinstance(v, str) and v.startswith("1D") else v)
         for k, v in body.items()}
    assert (c.post("/api/monitor/start", json=b).get_json() or {}).get("ok")
    end = time.time() + 5
    while time.time() < end and not c.get("/api/monitor/status").get_json().get("monitoring"):
        time.sleep(0.1)
    assert c.get("/api/monitor/status").get_json()["monitoring"] is True

    th = getattr(m, thread)
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(th.ident),
                                               ctypes.py_object(SystemExit))
    end = time.time() + 10
    while time.time() < end and th.is_alive():
        time.sleep(0.1)
    if th.is_alive():
        pytest.skip("could not kill the worker thread on this interpreter")

    assert c.get("/api/monitor/status").get_json()["monitoring"] is False, \
        "a dead worker still reports a healthy monitor"
    again = c.post("/api/monitor/start", json=b).get_json()
    assert again.get("ok") is True, \
        f'the app refused to restart after its worker died: {again}'
    m_flag = getattr(m, [f for r, f, t in MONITOR_APPS if r == rel][0])
    assert m_flag is True
    for _, ln in list(getattr(m, "_log", None) or getattr(m, "_sub_log", None)
                      or getattr(m, "_avg_log", None) or []):
        if "previous monitor thread had died" in ln["msg"]:
            break
    else:
        pytest.fail("taking over from a dead worker was not announced")
