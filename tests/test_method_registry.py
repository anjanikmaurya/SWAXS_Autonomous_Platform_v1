"""The pluggable fitting + optimising method registries (docs/AUTOFIT_REDESIGN.md).

Locks in: the current code is registered as the default method in each family,
the defaults reproduce today's behaviour, and the /api/methods endpoint lists
and switches methods (refusing an optimiser swap mid-campaign)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_fitting_registry_defaults_to_ls_sphere():
    import src.fitting as F
    assert F.DEFAULT_ID == "ls_sphere"
    ids = {m["id"] for m in F.available()}
    assert "ls_sphere" in ids
    # all three fitters are implemented now — nothing left as "soon"
    ready = {m["id"] for m in F.available() if m["ready"]}
    assert {"ls_sphere", "ml_regressor", "llm_assisted"} <= ready
    assert [m for m in F.available() if not m["ready"]] == []
    assert F.get("nonexistent").id == "ls_sphere"          # unknown falls back to default


def test_all_fitters_return_the_result_contract():
    """Every fitter returns the shape _analyze_file / the plot / the manifest
    consume: size{radius,diameter,source}, pdi, confidence, distribution."""
    import numpy as np
    import src.fitting as F
    from src.analysis.nanoparticle import model_intensity
    q = np.logspace(np.log10(0.08), np.log10(3.5), 150)
    I = model_intensity(q, 4.0, 0.12, 1000.0, 0.0, "schulz")
    for mid in ("ls_sphere", "ml_regressor", "llm_assisted"):
        r = F.get(mid).fit(q, I, None)
        assert r.get("distribution")
        assert 0.0 <= r.get("confidence", -1) <= 1.0
        sz = r.get("size") or {}
        assert sz.get("radius") is not None and sz.get("diameter") is not None
        assert r.get("pdi") is not None
        assert 2.0 < sz["radius"] < 7.0                    # all recover the ~4 nm input


def test_ls_sphere_matches_analyze_profile():
    import numpy as np
    import src.fitting as F
    from src.analysis.nanoparticle import analyze_profile
    q = np.linspace(0.1, 3.0, 120)
    I = 1e3 * np.exp(-(q * 4.0) ** 2 / 3.0) + 1.0
    a = analyze_profile(q, I, None, dist="auto")
    b = F.get("ls_sphere").fit(q, I, None, dist="auto")
    assert a.get("distribution") == b.get("distribution")
    assert a.get("size") == b.get("size")          # byte-identical fit


def test_optimiser_registry_builds_gp_ei():
    import src.optimizer.methods as M
    from src.optimizer import ParameterSpace, CampaignController
    from src.reactor import load_config
    assert M.DEFAULT_ID == "gp_ei"
    ready = {m["id"] for m in M.available() if m["ready"]}
    assert "gp_ei" in ready
    sp = ParameterSpace.from_config(load_config())
    camp = M.make("gp_ei", sp, target_size=5.0, tolerance=0.3, pdi_cap=0.15, budget=25)
    assert isinstance(camp, CampaignController)
    assert camp.budget == 25
    # unknown id falls back to the default factory
    assert isinstance(M.make("nope", sp, target_size=5.0, tolerance=0.3,
                             pdi_cap=0.15, budget=25), CampaignController)


def test_all_optimisers_are_ready():
    import src.optimizer.methods as M
    ready = {m["id"] for m in M.available() if m["ready"]}
    assert {"gp_ei", "gp_ucb", "random", "turbo", "bax", "pareto_ehvi"} <= ready
    # every advertised method is implemented now — nothing left as "soon"
    assert [m for m in M.available() if not m["ready"]] == []


def test_goal_pins_the_acquisition():
    """Two of the four goals ARE acquisition choices: level-set → BAX, pareto →
    EHVI. target-hit / benchmark keep the operator's dropdown choice."""
    import src.optimizer.methods as M
    assert M.for_goal("level-set") == "bax"
    assert M.for_goal("pareto") == "pareto_ehvi"
    # target-hit / benchmark / unknown fall back to the requested optimiser …
    assert M.for_goal("target-hit", "gp_ucb") == "gp_ucb"
    assert M.for_goal("benchmark", "turbo") == "turbo"
    assert M.for_goal(None) == M.DEFAULT_ID
    # … and a bad fallback still resolves to the default, never crashes
    assert M.for_goal("target-hit", "nonexistent") == M.DEFAULT_ID


def test_pareto_ehvi_runs_and_spreads_across_the_range():
    """EHVI drives a full ask/tell loop, proposes valid recipes, respects the
    budget, and ends with a non-None best — and, given a real size range, it
    samples a SPREAD of diameters rather than piling onto one size."""
    import numpy as np
    import src.optimizer.methods as M
    from src.optimizer import ParameterSpace
    from src.reactor import load_config
    from src.simulator.ground_truth import truth_from_recipe, DEFAULTS
    sp = ParameterSpace.from_config(load_config())
    R = DEFAULTS["R_opt"]
    camp = M.make("pareto_ehvi", sp, target_size=R, tolerance=0.3, pdi_cap=0.2,
                  budget=30, n_init=8, size_lo=R - 1.0, size_hi=R + 1.0)
    assert camp.size_lo < camp.size_hi                 # range accepted
    camp.start()
    sizes, n = [], 0
    while n <= 40:
        rec = camp.ask()
        if rec is None:
            break
        assert sp.valid(rec), "EHVI proposed an invalid recipe"
        t = truth_from_recipe(rec, None, seed_key=f"ehvi_{n}")
        camp.tell(rec, t["R_nm"], t["pdi"], 0.8, recipe_id=f"ehvi_r{n:03d}")
        if t["R_nm"] is not None:
            sizes.append(t["R_nm"])
        n += 1
    assert camp.status_str in ("converged", "exhausted")
    assert len(camp.history) <= camp.budget
    assert camp.best is not None and camp.best.get("size") is not None
    # with no size range the method is single-objective and degrades to EI (no crash)
    degen = M.make("pareto_ehvi", sp, target_size=R, tolerance=0.3, pdi_cap=0.15,
                   budget=12, n_init=6)        # size_lo==size_hi==target → range 0
    degen.start()
    for k in range(14):
        rr = degen.ask()
        if rr is None:
            break
        tt = truth_from_recipe(rr, None, seed_key=f"deg_{k}")
        degen.tell(rr, tt["R_nm"], tt["pdi"], 0.8, recipe_id=f"d{k}")
    assert degen.status_str in ("converged", "exhausted")


def test_hv2d_hypervolume_is_correct():
    """The 2-D dominated-hypervolume sweep the EHVI acquisition relies on."""
    from src.optimizer.methods.pareto_ehvi import _hv2d
    ref = (1.0, 1.0)
    # single point: rectangle area toward the reference
    assert abs(_hv2d([(0.4, 0.6)], ref) - (0.6 * 0.4)) < 1e-12
    # a dominated point adds nothing
    assert abs(_hv2d([(0.4, 0.6), (0.5, 0.7)], ref)
               - _hv2d([(0.4, 0.6)], ref)) < 1e-12
    # two non-dominated points: union area = (1-.2)(1-.8) + (1-.6)(.8-.3)
    got = _hv2d([(0.2, 0.8), (0.6, 0.3)], ref)
    assert abs(got - ((1 - .2) * (1 - .8) + (1 - .6) * (.8 - .3))) < 1e-12
    # points outside the reference box contribute nothing
    assert _hv2d([(1.5, 0.2), (0.3, 2.0)], ref) == 0.0


def test_each_optimiser_runs_a_campaign_against_ground_truth():
    """Every optimiser drives a full ask/tell loop against the hidden simulator,
    proposes valid recipes, respects the budget, and finishes (converge or
    exhaust) with a non-None best."""
    import src.optimizer.methods as M
    from src.optimizer import ParameterSpace
    from src.reactor import load_config
    from src.simulator.ground_truth import truth_from_recipe, DEFAULTS
    sp = ParameterSpace.from_config(load_config())
    for mid in ("random", "gp_ei", "gp_ucb", "turbo", "bax"):
        camp = M.make(mid, sp, target_size=DEFAULTS["R_opt"], tolerance=0.3,
                      pdi_cap=0.15, budget=40, n_init=8)
        camp.start()
        n = 0
        while n <= 60:
            rec = camp.ask()
            if rec is None:
                break
            assert sp.valid(rec), f"{mid} proposed an invalid recipe"
            t = truth_from_recipe(rec, None, seed_key=f"{mid}_{n}")
            camp.tell(rec, t["R_nm"], t["pdi"], 0.8, recipe_id=f"{mid}_r{n:03d}")
            n += 1
        assert camp.status_str in ("converged", "exhausted"), f"{mid}: {camp.status_str}"
        assert len(camp.history) <= camp.budget
        assert camp.best is not None and camp.best.get("size") is not None


def test_loss_matches_the_explainer_formula():
    """The Setup 'maths' panel shows score = ((R-Rt)/tol)^2 + w*(PDI/cap).
    Verify CampaignController.loss computes exactly that, so the UI and the code
    agree (correctness/clarity check)."""
    from src.optimizer import ParameterSpace, CampaignController
    from src.reactor import load_config
    sp = ParameterSpace.from_config(load_config())
    Rt, tol, cap, w = 4.0, 0.3, 0.1, 1.0
    camp = CampaignController(sp, target_size=Rt, tolerance=tol, pdi_cap=cap,
                             budget=25, weight_pdi=w)
    for R, pdi in [(4.0, 0.05), (4.3, 0.10), (3.7, 0.20), (5.0, 0.02)]:
        expect = ((R - Rt) / tol) ** 2 + w * (pdi / cap)
        assert abs(camp.loss(R, pdi) - expect) < 1e-9


def test_start_treats_target_as_radius_and_records_goal():
    """The Setup sends radius (= diameter/2); the backend must use it verbatim as
    the radius target and record the goal + spec units with the run."""
    import analyzer.app as az
    az.app.config["TESTING"] = True
    c = az.app.test_client()
    # diameter 8.0 -> radius 4.0, tolerance diameter 0.6 -> radius 0.3
    r = c.post("/api/campaign/start", json={
        "target_size": 4.0, "tolerance": 0.3, "pdi_cap": 0.1, "budget": 30,
        "goal": "level-set", "spec_units": "diameter"}).get_json()
    assert r.get("ok") is True
    assert az._campaign.target_size == 4.0 and az._campaign.tolerance == 0.3
    assert az._campaign_meta.get("goal") == "level-set"
    assert az._campaign_meta.get("spec_units") == "diameter"
    # level-set goal pinned the BAX acquisition regardless of the dropdown default
    assert az._campaign_meta.get("optimiser") == "bax"
    from src.optimizer.methods.bax import BAX
    assert isinstance(az._campaign, BAX)
    c.post("/api/campaign/abort")


def test_pareto_goal_pins_ehvi_and_maps_the_range():
    """Starting a Pareto campaign builds the EHVI optimiser (not the dropdown
    default), records the diameter size-range, and hands the acquisition the
    range in RADIUS (diameter/2)."""
    import analyzer.app as az
    from src.optimizer.methods.pareto_ehvi import ParetoEHVI
    az.app.config["TESTING"] = True
    c = az.app.test_client()
    r = c.post("/api/campaign/start", json={
        "target_size": 4.0, "tolerance": 0.5, "pdi_cap": 0.2, "budget": 25,
        "goal": "pareto", "spec_units": "diameter",
        "size_range": [6.0, 10.0]}).get_json()
    assert r.get("ok") is True
    assert az._campaign_meta.get("optimiser") == "pareto_ehvi"
    assert az._campaign_meta.get("size_range") == [6.0, 10.0]
    assert isinstance(az._campaign, ParetoEHVI)
    # diameter range [6,10] → radius range [3,5] inside the acquisition
    assert az._campaign.size_lo == 3.0 and az._campaign.size_hi == 5.0
    c.post("/api/campaign/abort")


def test_api_methods_lists_and_switches(monkeypatch):
    import analyzer.app as az
    az.app.config["TESTING"] = True
    c = az.app.test_client()

    r = c.get("/api/methods").get_json()
    assert r["active"]["fitter"] == "ls_sphere"
    assert r["active"]["optimiser"] == "gp_ei"
    assert any(m["id"] == "bax" and m["ready"] for m in r["optimisers"])

    # goal → {methods, default} drives the Setup strategy filter: the goal decides
    # which strategies are even applicable (goal-first UI).
    g = r["goals"]
    assert g["target-hit"]["methods"] == ["gp_ei", "gp_ucb", "turbo", "random"]
    assert g["target-hit"]["default"] == "gp_ei"
    assert g["level-set"]["methods"] == ["bax"]
    assert g["pareto"]["methods"] == ["pareto_ehvi"]

    # switching to an unavailable method is refused
    bad = c.post("/api/methods", json={"fitter": "nonexistent"})
    assert bad.status_code == 400

    # switching the optimiser while a campaign runs is refused
    monkeypatch.setattr(az, "_campaign", object())
    busy = c.post("/api/methods", json={"optimiser": "gp_ei"})
    assert busy.status_code == 409
    monkeypatch.setattr(az, "_campaign", None)
