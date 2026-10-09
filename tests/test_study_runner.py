"""In-silico method-comparison study runner + figure (docs/AUTOFIT_REDESIGN.md, Step 5)."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_run_study_returns_curves_per_method():
    from src.optimizer import study, ParameterSpace
    from src.reactor import load_config
    sp = ParameterSpace.from_config(load_config())
    res = study.run_study(sp, ["random", "gp_ei", "gp_ucb"],
                          study.default_cfg(), repeats=2)
    assert set(res) == {"random", "gp_ei", "gp_ucb"}
    for mid, r in res.items():
        assert len(r["repeats"]) == 2
        for run in r["repeats"]:
            assert run["status"] in ("converged", "exhausted")
            # best-loss curve is monotnon-increasing (running minimum)
            c = run["best_loss_curve"]
            assert c == sorted(c, reverse=True) or all(
                c[i] >= c[i + 1] for i in range(len(c) - 1))
            assert run["n"] <= study.default_cfg()["budget"]


def test_goal_figures_render_png():
    """The level-set and Pareto final-optimisation figures render from a campaign
    with history (goal-specific Report plots)."""
    from src.optimizer import ParameterSpace, plots
    import src.optimizer.methods as M
    from src.reactor import load_config
    from src.simulator.ground_truth import truth_from_recipe, DEFAULTS
    sp = ParameterSpace.from_config(load_config())
    camp = M.make("bax", sp, target_size=DEFAULTS["R_opt"], tolerance=0.4,
                  pdi_cap=0.15, budget=20, n_init=6)
    camp.start()
    n = 0
    while n <= 25:
        r = camp.ask()
        if r is None:
            break
        t = truth_from_recipe(r, None, seed_key=f"g{n}")
        camp.tell(r, t["R_nm"], t["pdi"], 0.8, recipe_id=f"r{n}")
        n += 1
    for view in ("levelset", "pareto"):
        png = plots.figure(view, camp)
        assert png[:4] == b"\x89PNG" and len(png) > 1000, view
    # both degrade to a placeholder (not a crash) with an empty campaign
    empty = M.make("gp_ei", sp, target_size=4.0, tolerance=0.3, pdi_cap=0.15, budget=10)
    empty.start()
    for view in ("levelset", "pareto"):
        assert plots.figure(view, empty)[:4] == b"\x89PNG"


def test_study_figure_renders_png():
    from src.optimizer import study, plots, ParameterSpace
    from src.reactor import load_config
    sp = ParameterSpace.from_config(load_config())
    res = study.run_study(sp, ["random", "gp_ei"], study.default_cfg(), repeats=1)
    png = plots.study_figure(res)
    assert png[:4] == b"\x89PNG" and len(png) > 1000
    # empty input degrades to a placeholder PNG, not a crash
    assert plots.study_figure({})[:4] == b"\x89PNG"


def test_api_study_endpoints():
    import analyzer.app as az
    az.app.config["TESTING"] = True
    c = az.app.test_client()
    # an unavailable method is rejected (unknown id filtered out -> empty -> 400)
    assert c.post("/api/study", json={"methods": ["nonexistent"]}).status_code == 400
    r = c.post("/api/study", json={"methods": ["random", "gp_ei"], "repeats": 2}).get_json()
    assert r["ok"] and set(r["summary"]) == {"random", "gp_ei"}
    img = c.get("/api/study/plot.png")
    assert img.status_code == 200 and img.data[:4] == b"\x89PNG"
