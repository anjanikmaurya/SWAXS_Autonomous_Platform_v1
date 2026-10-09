"""Regressions for the October 2026 closed-loop audit fixes.

1. Mapping goals (Pareto, level-set) run to their budget instead of stopping at
   the first in-spec hit (Run19 stopped 5 seeds into a 10-seed Pareto run).
2. Pareto EHVI no longer rewards particles larger than the requested range.
3. An acquisition failure falls back to a space-filling point, so the loop can
   never stall with nothing pending.
4. Resume restores the run's own strategy and its knobs.
5. Abort / a new campaign withdraws queued conditions from the REACTOR, through
   the file contract (file moved to Conditions/done/ -> dropped from the queue).
6. The hub makes the reactor safe over HTTP before killing it (Windows kill
   never ran the SIGTERM/atexit handler).
"""
from __future__ import annotations

import importlib
import importlib.util as _u
import sys
import time
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _space():
    from src.optimizer import ParameterSpace
    from src.reactor import load_config
    return ParameterSpace.from_config(load_config())


def _drive(camp, n_max, key):
    from src.simulator.ground_truth import truth_from_recipe
    n = 0
    while n < n_max:
        r = camp.ask()
        if r is None:
            break
        t = truth_from_recipe(r, None, seed_key=f"{key}{n}")
        camp.tell(r, t["R_nm"], t["pdi"], 0.9, recipe_id=f"{key}{n}")
        n += 1
    return n


# ── 1. mapping goals run to budget ───────────────────────────────────────────
def test_mapping_campaign_does_not_stop_at_first_hit():
    import src.optimizer.methods as M
    from src.simulator.ground_truth import DEFAULTS
    R = DEFAULTS["R_opt"]
    # A huge tolerance makes almost every recipe an in-spec "hit".
    camp = M.make("pareto_ehvi", _space(), target_size=R, tolerance=50.0,
                  pdi_cap=1.0, budget=12, n_init=6, size_lo=R - 2, size_hi=R + 2,
                  stop_on_hit=False)
    camp.start()
    _drive(camp, 30, "map")
    assert camp.status_str == "exhausted"            # used the whole budget
    assert len(camp.history) == 12
    assert camp.converged_condition is not None       # first hit still recorded


def test_target_hit_still_stops_at_first_hit():
    import src.optimizer.methods as M
    from src.simulator.ground_truth import DEFAULTS
    camp = M.make("gp_ei", _space(), target_size=DEFAULTS["R_opt"], tolerance=50.0,
                  pdi_cap=1.0, budget=12, n_init=6)
    camp.start()
    _drive(camp, 30, "hit")
    assert camp.status_str == "converged" and len(camp.history) == 1


def test_build_optimiser_sets_stop_rule_from_goal():
    a = importlib.import_module("analyzer.app")
    cfg = dict(target_size=3.0, tolerance=2.0, pdi_cap=0.1, budget=10, n_init=4)
    for goal, stops in (("pareto", False), ("level-set", False),
                        ("target-hit", True), ("benchmark", True)):
        camp, _ = a._build_optimiser(_space(), goal, [2.0, 10.0], cfg)
        assert camp.stop_on_hit is stops, goal


# ── 2. EHVI does not reward oversized particles ──────────────────────────────
def test_ehvi_treats_out_of_range_as_no_hypervolume():
    import src.optimizer.methods as M
    camp = M.make("pareto_ehvi", _space(), target_size=3.0, tolerance=2.0,
                  pdi_cap=0.2, budget=10, size_lo=1.0, size_hi=5.0)
    _, m2 = camp._obj(np.array([0.5, 1.0, 3.0, 5.0, 9.0]), np.zeros(5))
    rng = camp._range
    assert m2[0] == rng and m2[4] == rng      # below AND above range: reference
    assert m2[3] == 0.0 and m2[2] == 2.0      # inside: larger is better
    from src.optimizer.methods.pareto_ehvi import _hv2d
    assert _hv2d([(0.05, m2[4])], camp._ref) == 0.0   # oversized adds nothing


# ── 3. acquisition failure never stalls the loop ─────────────────────────────
def test_ask_falls_back_when_the_acquisition_raises(monkeypatch):
    import src.optimizer.methods as M
    sp = _space()
    camp = M.make("gp_ei", sp, target_size=3.0, tolerance=0.3, pdi_cap=0.1,
                  budget=20, n_init=3)
    camp.start()
    _drive(camp, 3, "seed")                       # past the seeds

    def boom():
        raise np.linalg.LinAlgError("singular covariance")
    monkeypatch.setattr(camp, "_suggest_bo", boom)
    p = camp.ask()
    assert p is not None and sp.valid(p)
    assert "LinAlgError" in camp.last_error and camp.n_fallbacks == 1


# ── 4. resume restores the strategy and its knobs ────────────────────────────
def test_resume_honours_recorded_strategy_and_params(monkeypatch):
    a = importlib.import_module("analyzer.app")
    monkeypatch.setattr(a, "_active_optimiser", "gp_ei")   # dropdown changed since
    cfg = dict(target_size=3.0, tolerance=0.3, pdi_cap=0.1, budget=10, n_init=4)
    camp, opt = a._build_optimiser(_space(), "target-hit", None, cfg,
                                   method_params={"kappa": 7.5}, chosen="gp_ucb")
    assert opt == "gp_ucb" and camp.kappa == 7.5


# ── 5a. analyzer abort withdraws its queued conditions ───────────────────────
def test_abort_sets_aside_queued_conditions(tmp_path, monkeypatch):
    a = importlib.import_module("analyzer.app")
    d = tmp_path / "1D" / "SAXS" / "Conditions"; d.mkdir(parents=True)
    monkeypatch.setattr(a, "_project_root", str(tmp_path))
    monkeypatch.setattr(a, "_cond_folder", "1D/SAXS/Conditions")
    a.app.config["TESTING"] = True
    c = a.app.test_client()
    if a._campaign is not None:
        c.post("/api/campaign/abort")
    r = c.post("/api/campaign/start", json={"target_size": 3.0, "tolerance": 0.3,
                                             "pdi_cap": 0.1, "budget": 5,
                                             "goal": "target-hit"}).get_json()
    assert r.get("ok"), r
    issued = list(d.glob("*.txt")) + list(d.glob("*.json")) + list(d.glob("*.dat"))
    assert issued, "start should have written the first condition"
    c.post("/api/campaign/abort")
    left = list(d.glob("*.txt")) + list(d.glob("*.json")) + list(d.glob("*.dat"))
    assert left == [], "abort must withdraw the run's queued conditions"
    body = (d / "done" / issued[0].name).read_text()
    assert "NOT RUN" in body and "aborted" in body


# ── 5b. reactor drops a queued condition whose file was withdrawn ────────────
def _boot_reactor(tmp_path, monkeypatch):
    conds = tmp_path / "1D" / "SAXS" / "Conditions"
    conds.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_REACTOR_BACKEND", "mock")
    spec = _u.spec_from_file_location(f"reactor_app_{tmp_path.name}",
                                      _ROOT / "reactor" / "app.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ctrl.cfg.setdefault("spec", {})["enabled"] = False
    mod._ctrl._spec_enabled = False
    return mod, conds


def _wait(pred, timeout=40.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def test_reactor_withdraws_a_condition_moved_to_done(tmp_path, monkeypatch):
    mod, conds = _boot_reactor(tmp_path, monkeypatch)
    try:
        for rid in ("w001", "w002"):
            (conds / f"{rid}.txt").write_text(
                "T_reac = 240\nF_tot = 80\nx_ODE = 0.2\nx_TOP = 0.1\nx_oley = 0.1\n")
        assert _wait(lambda: len(mod._ctrl.queue) == 2)
        (conds / "done").mkdir(exist_ok=True)
        (conds / "w001.txt").replace(conds / "done" / "w001.txt")   # producer withdraws
        assert _wait(lambda: [r.recipe_id for r, _ in mod._ctrl.queue] == ["w002"]), \
            [r.recipe_id for r, _ in mod._ctrl.queue]
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_reactor_does_not_withdraw_on_a_plain_disappearance(tmp_path, monkeypatch):
    """Deleted (not moved to done/) is not a withdrawal signal: an empty listing
    or a glitch must never silently drop queued work."""
    mod, conds = _boot_reactor(tmp_path, monkeypatch)
    try:
        (conds / "k001.txt").write_text(
            "T_reac = 240\nF_tot = 80\nx_ODE = 0.2\nx_TOP = 0.1\nx_oley = 0.1\n")
        assert _wait(lambda: len(mod._ctrl.queue) == 1)
        (conds / "k001.txt").unlink()
        time.sleep(float(mod._CFG.get("poll_interval", 3.0)) * 2 + 0.5)
        assert len(mod._ctrl.queue) == 1
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_withdraw_source_never_touches_other_sources():
    from src.reactor.controller import ReactorController  # noqa: F401
    mod_ctrl = type("C", (), {})()                       # minimal duck for the method
    from collections import deque
    import threading
    R = type("R", (), {})
    def rec(rid, src):
        r = R(); r.recipe_id = rid; r.source = src; return r
    mod_ctrl.queue = deque([(rec("a", "folder:a.txt"), {}), (rec("b", "folder:b.txt"), {}),
                            (rec("m", "api"), {})])
    mod_ctrl._lock = threading.RLock()
    mod_ctrl._log = lambda *a, **k: None
    gone = ReactorController.withdraw_source(mod_ctrl, "folder:a.txt")
    assert gone == ["a"]
    assert [r.recipe_id for r, _ in mod_ctrl.queue] == ["b", "m"]


# ── 6. hub makes the reactor safe before killing it ──────────────────────────
def test_reactor_shutdown_endpoint_is_local_only_and_runs_shutdown(tmp_path, monkeypatch):
    mod, _ = _boot_reactor(tmp_path, monkeypatch)
    try:
        calls = []
        monkeypatch.setattr(mod, "_shutdown_once", lambda why="": calls.append(why))
        c = mod.app.test_client()
        r = c.post("/api/shutdown", environ_base={"REMOTE_ADDR": "10.0.0.5"})
        assert r.status_code == 403 and calls == []
        r = c.post("/api/shutdown", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code == 200 and calls == ["hub stop"]
    finally:
        mod._ctrl.shutdown(collect_wait_s=0.0)


def test_hub_requests_graceful_shutdown_before_killing(monkeypatch):
    monkeypatch.setenv("SWAXS_NO_RESUME", "1")
    spec = _u.spec_from_file_location("hub_graceful", _ROOT / "hub" / "app.py")
    h = _u.module_from_spec(spec); spec.loader.exec_module(h)
    aid = h.APPS[0]["id"]
    order = []

    class P:
        pid = 1
        def poll(self): return None
    monkeypatch.setitem(h._procs, aid, P())
    monkeypatch.setattr(h, "_request_graceful_shutdown",
                        lambda port, timeout=20.0: order.append("graceful") or "graceful")
    monkeypatch.setattr(h.pl, "kill_tree",
                        lambda proc, grace=5.0: order.append("kill") or "terminated")
    monkeypatch.setattr(h.pl, "port_in_use", lambda port: False)
    h._stop_app(aid)
    assert order == ["graceful", "kill"]
