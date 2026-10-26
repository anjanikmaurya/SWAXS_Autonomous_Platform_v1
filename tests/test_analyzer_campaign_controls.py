"""
tests/test_analyzer_campaign_controls.py: the campaign controls added with the
goal-column Analyser page (October 2026).

* Held results: a fit of another shape than the campaign expects uses its
  budget slot but does not train the surrogate, cannot be the best or the hit,
  until the operator accepts it.
* Starting recipes: the operator's own recipes are validated against the search
  bounds and the reactor's hard caps, issued first, and survive resume/continue.
* Preview: shows exactly the cold-start recipes a Start would issue.
* Pause: no new condition while paused; resume issues the next one.
* Search range: read-only bounds and caps from the reactor config.
"""
from __future__ import annotations

import importlib.util as u
import sys

import pytest

from src.optimizer import CampaignController, ParameterSpace
from src.optimizer.intake_rules import (DEFAULT_SHAPE, check_seeds, held_reason,
                                        base_model)
from src.reactor import load_config


def _load(tag: str, path: str = "analyzer/app.py"):
    spec = u.spec_from_file_location(tag, path)
    m = u.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _no_threads(monkeypatch):
    monkeypatch.delenv("SWAXS_RESUME", raising=False)
    monkeypatch.setenv("SWAXS_NO_WATCH", "1")


@pytest.fixture
def an(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    m = _load(f"an_ctl_{tmp_path.name}")
    return m


def _space():
    return ParameterSpace.from_config(load_config())


def _cond_files(m):
    return sorted(p.stem for p in m._resolve_cond().glob("*.txt"))


# ── the shape rule ───────────────────────────────────────────────────────────
def test_shape_rule():
    assert base_model("sphere@hardsphere") == "sphere"
    assert held_reason("sphere", "sphere_cs") is None
    assert held_reason("polydisperse_sphere", "sphere") is None        # ls_sphere's model
    assert held_reason("core_shell_sphere", "sphere_cs") is None
    assert held_reason("core_shell_sphere", "sphere") == "shape"
    assert held_reason("cylinder", "sphere_cs") == "shape"
    assert held_reason("sphere@stickyhardsphere", "sphere") is None   # interactions, same shape
    assert held_reason("cylinder", "any") is None
    assert held_reason(None, DEFAULT_SHAPE) is None                   # no model = the sphere fit


# ── held results in the controller ───────────────────────────────────────────
def _ctl(**kw):
    c = CampaignController(_space(), target_size=2.5, tolerance=0.3, pdi_cap=0.1,
                           budget=20, n_init=3, **kw)
    return c.start()


def test_held_result_uses_budget_but_does_not_steer_or_win():
    c = _ctl()
    p = c.ask()
    # a perfect-looking cylinder: on target, narrow, confident
    c.tell(p, 2.5, 0.05, 0.95, recipe_id="Run1_r001", held="shape", model="cylinder")
    assert c.status_str == "running", "a held result must not end the run"
    assert len(c.history) == 1, "it still counts against the budget"
    assert c.fit_surrogate() is None, "it must not train the surrogate"
    assert c.held()[0]["recipe_id"] == "Run1_r001"
    # a worse, but honest, sphere becomes the best over the held one
    c.tell(c.ask(), 3.4, 0.08, 0.9, recipe_id="Run1_r002", model="sphere")
    assert c.best["recipe_id"] == "Run1_r002"
    assert c.status()["n_held"] == 1


def test_accept_lets_the_result_count_and_can_converge():
    c = _ctl()
    c.tell(c.ask(), 2.5, 0.05, 0.95, recipe_id="Run1_r001", held="shape", model="cylinder")
    rec = c.accept("Run1_r001")
    assert rec is not None and rec.get("accepted") and not rec.get("held")
    assert c.status_str == "converged", "an accepted in-spec, confident result is the hit"
    assert c.best["recipe_id"] == "Run1_r001"
    assert c.accept("Run1_r001") is None, "nothing left to accept"


def test_held_never_feeds_pareto_or_turbo():
    from src.optimizer import methods as M
    sp = _space()
    for mid in ("pareto_ehvi", "turbo", "bax", "gp_ucb", "random"):
        c = M.make(mid, sp, target_size=2.5, tolerance=0.3, pdi_cap=0.1, budget=10,
                   n_init=2).start()
        for i in range(4):
            c.tell(c.ask(), 2.5 + 0.1 * i, 0.05, 0.9, recipe_id=f"r{i}", held="shape",
                   model="cylinder")
        assert c.fit_surrogate() is None, mid
        assert c.ask() is not None, f"{mid} must still propose with only held data"


# ── starting recipes ─────────────────────────────────────────────────────────
def test_seed_validation():
    sp = _space()
    cfg = load_config()
    good = sp.sobol(1, seed=7)[0]
    clean, errs = check_seeds([good], sp, cfg.get("safety"))
    assert clean == [good] and not errs
    hot = {**good, "T_reac": 999.0}
    _, errs = check_seeds([hot], sp, cfg.get("safety"))
    assert errs and "safety cap" in errs[0]
    out = {**good, "x_ODE": 0.3, "x_TOP": 0.3, "x_oley": 0.3, "T_reac": good["T_reac"]}
    _, errs = check_seeds([{**out, "x_oley": 0.31}], sp, cfg.get("safety"))
    assert errs
    _, errs = check_seeds([{"T_reac": 200}], sp, cfg.get("safety"))
    assert errs and "missing" in errs[0]
    _, errs = check_seeds([good] * 6, sp, cfg.get("safety"))
    assert any("at most" in e for e in errs)
    _, errs = check_seeds([{**good, "F_tot": float("nan")}], sp, cfg.get("safety"))
    assert errs


def test_start_refuses_a_bad_recipe_and_leaves_the_queue_alone(an):
    cl = an.app.test_client()
    cond = an._resolve_cond()
    (cond / "Run9_r001.txt").write_text("recipe_id = Run9_r001\nT_reac = 240\n")
    r = cl.post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 5,
        "seeds": [{"T_reac": 999, "F_tot": 60, "x_ODE": .1, "x_TOP": .1, "x_oley": .1}]})
    assert r.status_code == 400 and "safety cap" in r.get_json()["error"]
    assert an._campaign is None
    assert (cond / "Run9_r001.txt").exists(), "a refused Start must not clear the queue"


def test_start_refuses_cold_start_longer_than_budget(an):
    r = an.app.test_client().post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 5, "n_init": 8})
    assert r.status_code == 400 and "longer than the whole budget" in r.get_json()["error"]


def test_own_recipes_go_first_and_preview_matches_start(an):
    sp = _space()
    seeds = sp.sobol(2, seed=11)
    body = {"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20,
            "n_init": 5, "seeds": seeds, "expected_shape": "sphere"}
    cl = an.app.test_client()
    pv = cl.post("/api/campaign/preview", json=body).get_json()
    assert pv["ok"] and [r["kind"] for r in pv["recipes"]] == ["own", "own", "spread", "spread", "spread"]
    assert an._campaign is None, "preview must not start anything"
    assert cl.post("/api/campaign/start", json=body).get_json()["ok"]
    first = an._pending[list(an._pending)[0]]
    assert first == pytest.approx(seeds[0])
    # every cold-start recipe the campaign will issue equals the preview, in order
    issued = [dict(s) for s in an._campaign._seeds]
    for got, want in zip(issued, pv["recipes"]):
        assert got == pytest.approx(want["params"])
    assert an._campaign_meta["seeds"] == seeds
    assert an._campaign_meta["expected_shape"] == "sphere"


def test_cylinder_fit_is_held_in_the_live_loop_and_accept_route(an):
    cl = an.app.test_client()
    cl.post("/api/campaign/start", json={"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1,
                                         "budget": 20, "n_init": 4,
                                         "expected_shape": "sphere_cs"})
    rid = list(an._pending)[0]
    an._feed_campaign(f"{rid}_s_SAXS_subtracted.dat",
                      {"size": {"radius": 2.5}, "pdi": 0.05, "confidence": 0.9,
                       "model": "cylinder"})
    st = cl.get("/api/campaign").get_json()
    assert st["status"] == "running" and st["held"][0]["recipe_id"] == rid
    assert cl.post("/api/campaign/accept", json={"recipe_id": "nope"}).status_code == 404
    r = cl.post("/api/campaign/accept", json={"name": f"{rid}_s_SAXS_subtracted.dat"})
    assert r.get_json()["ok"]
    assert an._campaign.status_str == "converged"


def test_campaign_without_expected_shape_keeps_old_behaviour(an):
    """A run started before the setting existed (no expected_shape recorded) must
    not suddenly hold its non-sphere fits after a restart."""
    an._campaign_meta = {}
    assert an._expected_shape() == "any"


# ── pause ────────────────────────────────────────────────────────────────────
def test_pause_holds_back_the_next_condition_and_resume_issues_it(an):
    cl = an.app.test_client()
    cl.post("/api/campaign/start", json={"target_size": 2.5, "tolerance": 0.3,
                                         "pdi_cap": 0.1, "budget": 20, "n_init": 4})
    rid = list(an._pending)[0]
    assert cl.post("/api/campaign/pause", json={"paused": True}).get_json()["paused"] is True
    an._feed_campaign(f"{rid}_s_SAXS_subtracted.dat",
                      {"size": {"radius": 3.4}, "pdi": 0.08, "confidence": 0.9})
    assert len(an._campaign.history) == 1, "a result for an issued condition is still recorded"
    assert not an._pending, "no new condition while paused"
    assert cl.get("/api/campaign").get_json()["paused"] is True
    assert cl.post("/api/campaign/pause", json={"paused": False}).get_json()["paused"] is False
    assert len(an._pending) == 1, "resume issues the next condition"


def test_pause_needs_a_running_campaign(an):
    assert an.app.test_client().post("/api/campaign/pause", json={"paused": True}).status_code == 409


def test_pause_and_seeds_survive_a_restart(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_RESUME", "1")
    a1 = _load("an_ctl_r1")
    seeds = _space().sobol(2, seed=3)
    a1.app.test_client().post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 5,
        "seeds": seeds})
    a1.app.test_client().post("/api/campaign/pause", json={"paused": True})
    a2 = _load("an_ctl_r2")
    a2._restore_campaign()
    assert a2._campaign is not None and a2._paused is True
    assert a2._campaign._seeds[0] == pytest.approx(seeds[0])


# ── search range ─────────────────────────────────────────────────────────────
def test_search_range_comes_from_the_reactor_config(an):
    cfg = load_config()
    r = an.app.test_client().get("/api/search_range").get_json()
    assert r["ok"]
    assert r["bounds"]["T_reac"] == list(map(float, cfg["bounds"]["T_reac"]))
    assert r["x_sum_max"] == float(cfg["bounds"]["x_sum_max"])
    assert r["safety"]["T_max"] == cfg["safety"]["T_max"]
    assert set(r["shapes"]) == {"sphere_cs", "sphere", "any"}


# ── end to end: every recipe the loop writes passes the reactor's own check ──
def test_simulated_loop_only_writes_recipes_the_reactor_accepts(an):
    """Own recipe first, a cylinder held mid-run, pause and resume, then the
    loop to convergence or budget, against the ground-truth simulator. Every
    condition file the analyzer writes is parsed back and checked with the
    reactor's validate() (bounds, pump windows, hard caps)."""
    from src.reactor.recipe import Recipe, parse_param_file, validate
    from src.simulator.ground_truth import truth_from_recipe
    cfg = load_config()
    own = _space().sobol(1, seed=21)[0]
    cl = an.app.test_client()
    r = cl.post("/api/campaign/start", json={
        "target_size": 4.0, "tolerance": 0.25, "pdi_cap": 0.1, "budget": 16,
        "n_init": 6, "seeds": [own], "expected_shape": "sphere_cs"}).get_json()
    assert r["ok"], r
    seen = set()
    for step in range(16):
        if not an._pending:
            break
        rid = list(an._pending)[0]
        f = an._resolve_cond() / f"{rid}.txt"
        rec = Recipe.from_dict(parse_param_file(f.read_text()))
        validate(rec, cfg)                       # raises on anything unsafe
        params = an._pending[rid]
        if step == 0:
            assert params == pytest.approx(own), "your own recipe is issued first"
        t = truth_from_recipe(params, seed_key=rid)
        model = "cylinder" if step == 2 else "sphere"
        if step == 3:
            cl.post("/api/campaign/pause", json={"paused": True})
        an._feed_campaign(f"{rid}_sample_SAXS_subtracted.dat",
                          {"size": {"radius": t["R_nm"]}, "pdi": t["pdi"],
                           "confidence": 0.9, "model": model})
        if step == 3:
            assert not an._pending, "paused: nothing new issued"
            cl.post("/api/campaign/pause", json={"paused": False})
        seen.add(rid)
    st = cl.get("/api/campaign").get_json()
    assert st["status"] in ("running", "converged", "exhausted")
    assert any(h.get("held") == "shape" for h in an._campaign.history), "the cylinder was held"
    assert len(seen) >= 4


# ── the fitter belongs to the campaign (audit AN1, AN5, AN7) ─────────────────
def _start_with_fitter(mod, fitter="catalogue_bic"):
    cl = mod.app.test_client()
    assert cl.post("/api/methods", json={"fitter": fitter}).get_json()["active"]["fitter"] == fitter
    r = cl.post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 3}).get_json()
    assert r["ok"], r
    return cl


def test_a_restart_keeps_the_campaigns_fitter(tmp_path, monkeypatch):
    """AN1: a resumed campaign fitted with the app's default (ls_sphere) instead
    of the fitter it started with, so non-spheres were suddenly sized as spheres."""
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_RESUME", "1")
    a1 = _load("an_fit_r1")
    _start_with_fitter(a1, "catalogue_bic")
    a2 = _load("an_fit_r2")
    assert a2._active_fitter == "ls_sphere"                       # the app default
    a2._restore_campaign()
    assert a2._campaign is not None and a2._active_fitter == "catalogue_bic"
    assert a2._paused is False


def test_a_restart_pauses_when_the_campaigns_fitter_is_missing(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_RESUME", "1")
    a1 = _load("an_fit_m1")
    _start_with_fitter(a1, "catalogue_bic")
    issued = set(a1._pending)
    a2 = _load("an_fit_m2")
    real = a2.fitting.available
    monkeypatch.setattr(a2.fitting, "available",
                        lambda: [m for m in real() if m["id"] != "catalogue_bic"])
    a2._restore_campaign()
    assert a2._campaign is not None and a2._paused is True
    assert a2._active_fitter == "ls_sphere"
    assert "catalogue_bic" in a2._CAMPAIGN_NOTICE["message"]
    assert set(a2._pending) == issued          # the in-flight one is kept, nothing new issued


def test_the_fitter_cannot_change_during_a_campaign(an):
    """AN5: the page locked the control, the API did not."""
    cl = _start_with_fitter(an, "catalogue_bic")
    r = cl.post("/api/methods", json={"fitter": "ls_sphere"})
    assert r.status_code == 409 and "fitter" in r.get_json()["error"]
    assert an._active_fitter == "catalogue_bic"
    # posting the same fitter (the page does this on every method change) is fine
    assert cl.post("/api/methods", json={"fitter": "catalogue_bic"}).status_code == 200


def test_continue_keeps_the_fitter_and_its_frozen_knowledge(an, monkeypatch):
    """AN1 and AN7 for Continue: the durable record carries both."""
    monkeypatch.setattr(an, "_latest_incomplete_run", lambda: {
        "campaign_id": "c9", "run_no": 9, "run_tag": "Run9", "target_size": 2.5,
        "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 3,
        "fitter": "catalogue_bic", "fit_knowledge": {"hash": "abc123def456", "sources": ["catalogue"]}})
    monkeypatch.setattr(an, "_load_fit_records_for_campaign", lambda cid, tag: [])
    assert an._active_fitter == "ls_sphere"
    an._continue_run()
    assert an._active_fitter == "catalogue_bic"
    assert an._campaign_meta["fit_knowledge"]["hash"] == "abc123def456"


def test_continue_refuses_when_the_fitter_is_missing(an, monkeypatch):
    monkeypatch.setattr(an, "_latest_incomplete_run", lambda: {
        "campaign_id": "c9", "run_no": 9, "run_tag": "Run9", "target_size": 2.5,
        "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 3, "fitter": "no_such_fitter"})
    monkeypatch.setattr(an, "_load_fit_records_for_campaign", lambda cid, tag: [])
    with pytest.raises(RuntimeError, match="no_such_fitter"):
        an._continue_run()
    assert an._campaign is None and an._active_fitter == "ls_sphere"


# ── which batch trains the model (audit AN2 / O7) ────────────────────────────
def _settle_campaign(an, settle=60.0):
    an._BATCH_SETTLE_S = settle
    cl = an.app.test_client()
    assert cl.post("/api/campaign/start", json={
        "target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 20, "n_init": 3}).get_json()["ok"]
    return cl, next(iter(an._pending))


def _res(r, conf, model="sphere"):
    return {"size": {"radius": r}, "pdi": 0.08, "confidence": conf, "model": model}


def test_batches_settle_then_the_best_one_trains_the_model(an, tmp_path):
    import time as _t
    cl, rid = _settle_campaign(an)
    for i, (r, c) in enumerate(((3.0, 0.5), (3.4, 0.9), (3.2, 0.7)), 1):
        an._feed_campaign(f"{rid}_batch{i:03d}_SAXS_subtracted.dat", _res(r, c))
    assert len(an._campaign.history) == 0 and len(an._batches[rid]["items"]) == 3
    st = cl.get("/api/campaign").get_json()
    assert st["collecting"][0]["recipe_id"] == rid and st["collecting"][0]["n"] == 3
    an._settle_tick()                                   # run not ended yet: keep waiting
    assert len(an._campaign.history) == 0
    fb = tmp_path / "reactor" / "feedback"
    fb.mkdir(parents=True)
    (fb / f"{rid}.done.json").write_text("{}")
    an._settle_tick()                                   # ended, but a batch came just now
    assert len(an._campaign.history) == 0
    an._batches[rid]["last"] = _t.time() - 61
    an._settle_tick()
    h = an._campaign.history[-1]
    assert len(an._campaign.history) == 1 and h["size"] == 3.4 and h["recipe_id"] == rid
    assert h["source_file"].endswith("batch002_SAXS_subtracted.dat")
    assert rid not in an._pending and rid not in an._batches and len(an._pending) == 1
    # a straggler batch of a recipe already learned is recorded, not used
    an._feed_campaign(f"{rid}_batch004_SAXS_subtracted.dat", _res(9.9, 0.99))
    assert len(an._campaign.history) == 1


def test_a_tie_goes_to_the_latest_batch_and_no_marker_means_max_wait(an):
    import time as _t
    _cl, rid = _settle_campaign(an)
    an._feed_campaign(f"{rid}_batch001_SAXS_subtracted.dat", _res(3.0, 0.8))
    an._feed_campaign(f"{rid}_batch002_SAXS_subtracted.dat", _res(3.3, 0.8))
    an._batches[rid]["items"][f"{rid}_batch001_SAXS_subtracted.dat"]["t"] -= 5
    an._settle_tick()
    assert len(an._campaign.history) == 0               # no done marker, not yet max wait
    an._batches[rid]["first"] = _t.time() - an._BATCH_MAX_WAIT_S - 1
    an._settle_tick()
    assert an._campaign.history[-1]["size"] == 3.3


def test_a_timed_out_condition_with_batches_is_told_not_failed(an):
    import time as _t
    _cl, rid = _settle_campaign(an)
    an._feed_campaign(f"{rid}_batch001_SAXS_subtracted.dat", _res(3.1, 0.7))
    an._pending_at[rid] = _t.time() - an._PENDING_TIMEOUT_S - 1
    an._expire_pending()
    h = an._campaign.history[-1]
    assert h["size"] == 3.1 and not h.get("expired")


def test_collected_batches_survive_a_restart(tmp_path, monkeypatch):
    pytest.importorskip("scipy")
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    monkeypatch.setenv("SWAXS_RESUME", "1")
    monkeypatch.setenv("SWAXS_BATCH_SETTLE_S", "60")
    a1 = _load("an_batch_r1")
    _cl, rid = _settle_campaign(a1)
    a1._feed_campaign(f"{rid}_batch001_SAXS_subtracted.dat", _res(3.6, 0.85))
    a2 = _load("an_batch_r2")
    a2._restore_campaign()
    assert list(a2._batches[rid]["items"].values())[0]["size"] == 3.6


def test_settle_time_from_setup_and_zero_keeps_the_first_batch_rule(an):
    cl = an.app.test_client()
    bad = cl.post("/api/campaign/start", json={"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1,
                                               "budget": 20, "n_init": 3, "batch_settle_s": 99999})
    assert bad.status_code == 400 and "settle" in bad.get_json()["error"]
    assert cl.post("/api/campaign/start", json={"target_size": 2.5, "tolerance": 0.3, "pdi_cap": 0.1,
                                                "budget": 20, "n_init": 3, "batch_settle_s": 0}).get_json()["ok"]
    an._BATCH_SETTLE_S = 600                             # the campaign's own 0 wins
    rid = next(iter(an._pending))
    an._feed_campaign(f"{rid}_batch001_SAXS_subtracted.dat", _res(3.0, 0.4))
    assert len(an._campaign.history) == 1


# ── AN3: a fast fit drives the loop, the AI refines in the background ────────
def test_the_ai_fit_refines_in_the_background(an, tmp_path, monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    _cl, rid = _settle_campaign(an)
    an._active_fitter = "llm_assisted"
    seen = []

    def fake_get(fid):
        def fit(q, I, sigma=None, **kw):
            seen.append(fid)
            ai = fid == "llm_assisted"
            return {"size": {"radius": 4.0 if ai else 3.0, "diameter": 8.0 if ai else 6.0},
                    "pdi": 0.07, "confidence": 0.9 if ai else 0.6, "model": "sphere",
                    "llm_structure": {"status": "ai"} if ai else {}, "llm_assist": {"summary": "", "flags": []}}
        return SimpleNamespace(fit=fit)
    monkeypatch.setattr(an.fitting, "get", fake_get)
    sub = tmp_path / "1D" / "SAXS" / "Subtracted"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / f"{rid}_batch001_SAXS_subtracted.dat"
    q = np.geomspace(0.05, 5, 80)
    f.write_text("\n".join(f"{a:.6g} {1 / (1 + (a * 3) ** 4):.6g} 0.01" for a in q))
    an._analyze_file(f)
    assert seen[0] == "catalogue_bic"                  # the loop never waited on the AI
    an._ai_fit_wait(30)
    assert seen == ["catalogue_bic", "llm_assisted"]
    item = an._batches[rid]["items"][f.name]
    assert item["confidence"] == 0.9 and item["refined"] is True and len(an._batches[rid]["items"]) == 1
    assert an._results[f.name]["summary"]["confidence"] == 0.9


# ── AN4: knowledge tests wait for the campaign ───────────────────────────────
def test_knowledge_tests_are_refused_while_a_campaign_runs(an, tmp_path, monkeypatch):
    monkeypatch.setenv("SWAXS_DESIGN_KNOWLEDGE_DIR", str(tmp_path / "design"))
    from src.design_knowledge import store
    sid = store.create_set("Pd")["id"]
    _cl, _rid = _settle_campaign(an)
    cl = an.app.test_client()
    r = cl.post(f"/api/knowledge/set/{sid}/fitter/evaluate")
    assert r.status_code == 409 and "after" in r.get_json()["error"]
    assert cl.post(f"/api/knowledge/set/{sid}/evaluate", json={}).status_code == 409
