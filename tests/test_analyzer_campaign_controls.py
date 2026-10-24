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
