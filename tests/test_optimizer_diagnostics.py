"""
tests/test_optimizer_diagnostics.py — the parameter-space views must be honest.

A diagnostics panel that lies is worse than no panel: it is read during beamtime
to decide whether to keep spending beam. The properties locked down here:

  1. READ-ONLY. Rendering a figure or fetching diagnostics must not advance the
     campaign. `peek()` must not consume the proposal `ask()` would return.
  2. THE SAME SURROGATE. The plotted GP must be the one the proposal came from,
     not a lookalike refitted with different noise or length scale.
  3. INFEASIBLE MEANS BLANK. Grid cells violating Σx ≤ x_sum_max are NaN, never a
     smooth surrogate value — the optimizer can never propose them.
  4. THE TARGET CONTOUR IS REAL. Points reported as "predicted size = target"
     must actually sit where the size surrogate crosses the target.
  5. NORMALISED UNCERTAINTY. Raw posterior sd is not comparable across refits
     because GP.fit sets the signal variance to var(y), which grows with the
     campaign. The reported traces must be prior-normalised and in [0, 1].
  6. TRUTH IS MOCK-ONLY. The hidden optimum may never be drawn when the reactor
     backend is real.
"""
from __future__ import annotations

import importlib.util as u
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("scipy")
pytest.importorskip("matplotlib")

from src.optimizer import diagnostics as dg                       # noqa: E402
from src.optimizer import plots as opl                            # noqa: E402
from src.optimizer.campaign import CampaignController, _FAIL_LOSS  # noqa: E402
from src.optimizer.space import ParameterSpace                    # noqa: E402
from src.simulator.ground_truth import DEFAULTS, truth_from_recipe  # noqa: E402

PNG = b"\x89PNG"


def _campaign(n=14, *, budget=60, n_init=8, tol=0.10, cap=0.05, seed=1,
              fail_at=None, **kw):
    """A campaign fed by the real hidden landscape, stopped after n runs."""
    c = CampaignController(ParameterSpace.from_config({}), target_size=4.0,
                           tolerance=tol, pdi_cap=cap, budget=budget,
                           n_init=n_init, seed=seed, **kw).start()
    for i in range(n):
        p = c.ask()
        if p is None:
            break
        if fail_at is not None and i == fail_at:
            c.tell(p, None, None, 0.2)              # unsized profile
            continue
        t = truth_from_recipe(p, None, seed_key=f"t-{seed}-{i}")
        c.tell(p, t["R_nm"], t["pdi"], float(np.clip(0.97 - 1.5 * t["pdi"], 0.15, 0.97)))
        c.status_str = "running"                    # keep going for the fixture
    return c


# ── 1. read-only ─────────────────────────────────────────────────────────────
def test_peek_does_not_consume_the_proposal():
    c = _campaign(12)
    n_asked, n_hist = c._n_asked, len(c.history)
    first = c.peek()
    assert c.peek() == first, "peek is not deterministic"
    assert c._n_asked == n_asked and len(c.history) == n_hist, "peek mutated state"
    assert c.ask() == first, "ask() returned something other than what peek showed"


def test_rendering_every_view_leaves_the_campaign_untouched():
    c = _campaign(12)
    before = (c._n_asked, len(c.history), c.status_str,
              [dict(h["params"]) for h in c.history])
    for view in ("slice", "convergence", "trajectory"):
        assert opl.figure(view, c)[:4] == PNG
    dg.summary(c); dg.convergence(c); dg.trajectory(c); dg.slice_surfaces(c)
    after = (c._n_asked, len(c.history), c.status_str,
             [dict(h["params"]) for h in c.history])
    assert before == after, "a diagnostics call changed the campaign"


def test_peek_is_none_once_the_campaign_stops():
    c = _campaign(6)
    c.abort()
    assert c.peek() is None
    s = dg.slice_surfaces(c)
    assert s["proposal"] is None


# ── 2. the same surrogate the decision used ──────────────────────────────────
def test_slice_uses_the_proposal_surrogate():
    """If diagnostics refitted its own GP, the picture could disagree with the
    decision. Same inputs must give bit-identical predictions."""
    c = _campaign(12)
    gp, X, y = c.fit_surrogate()
    s = dg.slice_surfaces(c, "T_reac", "x_TOP", n=12)
    fixed = dg.anchor(c, "best")
    xs, ys = s["x"], s["y"]
    p = dict(fixed); p["T_reac"] = xs[3]; p["x_TOP"] = ys[5]
    mu, var = gp.predict(np.array([c.space.to_unit(p)]))
    assert np.isclose(np.array(s["loss_mean"])[5, 3], mu[0], rtol=1e-12)
    assert np.isclose(np.array(s["loss_sd"])[5, 3], np.sqrt(var[0]), rtol=1e-12)


def test_grid_orientation_is_row_y_column_x():
    """An off-by-one transpose here would silently mirror every figure."""
    c = _campaign(10)
    s = dg.slice_surfaces(c, "T_reac", "x_TOP", n=9)
    assert np.array(s["loss_mean"]).shape == (9, 9)
    assert s["x"][0] == c.space.bounds["T_reac"][0]
    assert s["y"][-1] == c.space.bounds["x_TOP"][1]


def test_loss_scale_is_reported_so_the_colour_bar_cannot_mislead():
    c = _campaign(10, loss_transform="log1p")
    assert dg.slice_surfaces(c)["loss_scale"] == "log1p"
    assert dg.slice_surfaces(_campaign(10))["loss_scale"] == "none"


# ── 3. the constraint ────────────────────────────────────────────────────────
def test_infeasible_cells_are_nan_not_extrapolated():
    """Σx ≤ x_sum_max. On an (x_ODE, x_TOP) slice anchored at a large x_oley the
    top-right corner is unreachable, and must be blank rather than inviting a
    recipe the reactor would reject."""
    space = ParameterSpace(t_reac=(180, 300), f_tot=(40, 120),
                           x_each=(0.0, 0.5), x_sum_max=0.6)
    c = CampaignController(space, target_size=4.0, tolerance=0.1, pdi_cap=0.05,
                           budget=40, n_init=6, seed=0).start()
    for i in range(8):
        p = c.ask()
        t = truth_from_recipe(p, None, seed_key=f"c-{i}")
        c.tell(p, t["R_nm"], t["pdi"], 0.9)
        c.status_str = "running"
    s = dg.slice_surfaces(c, "x_ODE", "x_TOP", n=24, anchor_mode="mid")
    Z = np.array(s["loss_mean"])
    assert np.isnan(Z).any(), "no cell was masked despite the sum constraint"
    assert 0.0 < s["feasible_frac"] < 1.0
    # every masked cell must genuinely be invalid, and vice versa
    fixed = dg.anchor(c, "mid")
    for i, yv in enumerate(s["y"]):
        for j, xv in enumerate(s["x"]):
            p = dict(fixed); p["x_ODE"] = xv; p["x_TOP"] = yv
            assert np.isnan(Z[i, j]) == (not space.valid(p)), (i, j, xv, yv)


def test_bad_slice_axes_are_rejected():
    c = _campaign(6)
    for bad in (("T_reac", "T_reac"), ("nope", "x_TOP")):
        with pytest.raises(ValueError):
            dg.slice_surfaces(c, *bad)


# ── 4. the target iso-contour ────────────────────────────────────────────────
def test_target_contour_points_lie_on_the_crossing():
    c = _campaign(16)
    s = dg.slice_surfaces(c, "T_reac", "x_TOP", n=40)
    pts = dg.target_contour_points(s)
    assert pts, "no crossing found even though the samples straddle the target"
    sgp, _, _ = dg.fit_size_surrogate(c)
    fixed = dg.anchor(c, "best")
    for x, y in pts[:25]:
        p = dict(fixed); p["T_reac"] = x; p["x_TOP"] = y
        mu, _ = sgp.predict(np.array([c.space.to_unit(p)]))
        # tolerance is one grid step's worth of curvature, not machine precision
        assert abs(mu[0] - s["target_size"]) < 0.15, (x, y, mu[0])


def test_no_size_surrogate_until_there_are_enough_sized_profiles():
    c = CampaignController(ParameterSpace.from_config({}), target_size=4.0,
                           tolerance=0.1, pdi_cap=0.05, budget=20, n_init=6,
                           seed=0).start()
    p = c.ask(); c.tell(p, 4.1, 0.03, 0.9)
    assert dg.fit_size_surrogate(c) is None
    assert dg.slice_surfaces(c)["size_mean"] is None
    assert dg.target_contour_points(dg.slice_surfaces(c)) == []


# ── 5. convergence traces ────────────────────────────────────────────────────
def test_best_so_far_is_monotone_non_increasing():
    d = dg.convergence(_campaign(18))
    b = d["best_loss"]
    assert all(b[i + 1] <= b[i] + 1e-12 for i in range(len(b) - 1)), b


def test_uncertainty_traces_are_prior_normalised_and_bounded():
    d = dg.convergence(_campaign(16))
    for key in ("max_sd_rel", "mean_sd_rel"):
        v = np.array(d[key], float)
        assert v.size == len(d["runs"])
        assert np.all((v >= 0) & (v <= 1.0 + 1e-9)), f"{key} out of [0,1]: {v}"
    # coverage must improve; the worst corner is allowed not to
    m = np.array(d["mean_sd_rel"], float)
    assert m[-1] < m[0], "mean posterior sd did not fall at all"
    assert np.all(np.array(d["max_sd_rel"]) >= np.array(d["mean_sd_rel"]) - 1e-9)


def test_uncertainty_can_be_skipped_for_speed():
    d = dg.convergence(_campaign(10), uncertainty=False)
    assert d["max_sd"] == [] and d["mean_sd_rel"] == []


def test_unsized_profiles_are_flagged_not_plotted_as_a_loss():
    """A 1e3 sentinel drawn on the same colour scale as real losses would flatten
    every map in the figure."""
    c = _campaign(10, fail_at=3)
    d = dg.convergence(c)
    assert sum(d["failed"]) == 1
    assert d["size"][3] is None
    s = dg.slice_surfaces(c)
    flagged = [p for p in s["samples"] if p["failed"]]
    assert len(flagged) == 1 and flagged[0]["loss"] >= _FAIL_LOSS
    assert opl.figure("convergence", c)[:4] == PNG      # must still render


def test_step_length_has_one_fewer_entry_than_runs():
    d = dg.convergence(_campaign(12))
    assert len(d["step"]) == len(d["runs"]) - 1
    assert all(v >= 0 for v in d["step"])


# ── trajectory ───────────────────────────────────────────────────────────────
def test_trajectory_labels_the_sobol_prefix():
    c = _campaign(14, n_init=8)
    t = dg.trajectory(c)
    assert [r["phase"] for r in t["rows"][:8]] == ["sobol"] * 8
    assert all(r["phase"] == "bo" for r in t["rows"][8:])
    assert all(0.0 - 1e-9 <= v <= 1.0 + 1e-9 for r in t["rows"] for v in r["unit"])


# ── empty and broken states ──────────────────────────────────────────────────
def test_everything_degrades_gracefully_before_the_first_result():
    c = CampaignController(ParameterSpace.from_config({}), target_size=4.0,
                           tolerance=0.1, pdi_cap=0.05, budget=10, n_init=4,
                           seed=0).start()
    assert dg.slice_surfaces(c) is None
    assert dg.convergence(c)["runs"] == []
    assert dg.trajectory(c)["rows"] == []
    for view in ("slice", "convergence", "trajectory"):
        assert opl.figure(view, c)[:4] == PNG, f"{view} did not render an empty state"


def test_an_unknown_view_returns_a_png_not_an_exception():
    assert opl.figure("nonsense", _campaign(8))[:4] == PNG


def test_a_renderer_exception_cannot_kill_a_run(monkeypatch):
    """A broken plot during an overnight campaign must degrade to a placeholder,
    never propagate into the Flask worker."""
    monkeypatch.setattr(opl, "convergence_figure",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setitem(opl.FIGURES, "convergence", opl.convergence_figure)
    assert opl.figure("convergence", _campaign(8))[:4] == PNG


# ── 6. the hidden optimum is mock-only ───────────────────────────────────────
def _load_analyzer(tag, tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("SWAXS_PROJECT", str(tmp_path))
    (tmp_path / "1D" / "SAXS" / "Conditions").mkdir(parents=True, exist_ok=True)
    spec = u.spec_from_file_location(tag, str(ROOT / "analyzer" / "app.py"))
    m = u.module_from_spec(spec); sys.modules[tag] = m; spec.loader.exec_module(m)
    monkeypatch.setattr(m, "load_config", lambda *a, **k: cfg)
    return m


@pytest.mark.parametrize("backend,enabled,expect", [
    ("mock", True, True),
    ("real", True, False),          # never reveal the optimum with real beam
    ("REAL", True, False),          # case must not defeat the gate
    ("mock", False, False),         # simulator off → there is no ground truth
])
def test_truth_marker_is_gated_on_the_mock_backend(tmp_path, monkeypatch,
                                                   backend, enabled, expect):
    cfg = {"spec": {"backend": backend}, "simulator": {"enabled": enabled}}
    m = _load_analyzer(f"az_truth_{backend}_{enabled}", tmp_path, monkeypatch, cfg)
    assert (m._truth_for_plots() is not None) is expect


def test_truth_marker_survives_a_broken_config(tmp_path, monkeypatch):
    m = _load_analyzer("az_truth_bad", tmp_path, monkeypatch, None)
    monkeypatch.setattr(m, "load_config",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no config")))
    assert m._truth_for_plots() is None


def test_truth_overrides_from_config_are_honoured(tmp_path, monkeypatch):
    cfg = {"spec": {"backend": "mock"},
           "simulator": {"enabled": True, "truth": {"T_opt": 275.0, "bogus": 1}}}
    m = _load_analyzer("az_truth_ovr", tmp_path, monkeypatch, cfg)
    t = m._truth_for_plots()
    assert t["T_opt"] == 275.0
    assert t["x_TOP_opt"] == DEFAULTS["x_TOP_opt"]
    assert "bogus" not in t, "an unknown truth key leaked into the landscape"


# ── the endpoints ────────────────────────────────────────────────────────────
def test_endpoints_serve_pngs_and_json(tmp_path, monkeypatch):
    cfg = {"spec": {"backend": "mock"}, "simulator": {"enabled": True}}
    m = _load_analyzer("az_ep", tmp_path, monkeypatch, cfg)
    cl = m.app.test_client()

    # before any campaign: a placeholder image, and an honest 404 on the JSON
    assert cl.get("/api/campaign/diagnostics").status_code == 404
    r = cl.get("/api/campaign/plot/slice.png")
    assert r.status_code == 200 and r.mimetype == "image/png" and r.data[:4] == PNG

    m._campaign = _campaign(12)
    m._campaign_cfg = {}
    d = cl.get("/api/campaign/diagnostics").get_json()
    assert d["ok"] and d["names"][0] == "T_reac" and d["has_truth"] is True
    assert 0.0 <= d["summary"]["uncertainty_late"] <= 1.0

    for view in ("slice", "convergence", "trajectory"):
        r = cl.get(f"/api/campaign/plot/{view}.png")
        assert r.status_code == 200 and r.data[:4] == PNG, view
        assert r.headers["Cache-Control"] == "no-store"
    # a custom slice, and a degenerate one that must not 500
    assert cl.get("/api/campaign/plot/slice.png?x=F_tot&y=x_oley&anchor=mid"
                  ).data[:4] == PNG
    assert cl.get("/api/campaign/plot/slice.png?x=F_tot&y=F_tot").data[:4] == PNG


def test_the_panel_cannot_advance_the_campaign_through_the_api(tmp_path, monkeypatch):
    m = _load_analyzer("az_ro", tmp_path, monkeypatch,
                       {"spec": {"backend": "mock"}, "simulator": {"enabled": True}})
    cl = m.app.test_client()
    m._campaign = _campaign(12); m._campaign_cfg = {}
    before = (m._campaign._n_asked, len(m._campaign.history))
    for _ in range(3):
        cl.get("/api/campaign/plot/slice.png")
        cl.get("/api/campaign/plot/convergence.png")
        cl.get("/api/campaign/diagnostics")
    assert (m._campaign._n_asked, len(m._campaign.history)) == before


# ── the opt-in loss transform must not change live behaviour ──────────────────
def test_loss_transform_defaults_to_off():
    c = CampaignController(ParameterSpace.from_config({}), target_size=4.0,
                           tolerance=0.1, pdi_cap=0.05)
    assert c.loss_transform == "none"
    y = np.array([0.5, 3.0, 1000.0])
    assert np.allclose(c._transform(y), y), "default transform is not the identity"


def test_log1p_transform_is_monotone_and_tames_the_sentinel():
    c = CampaignController(ParameterSpace.from_config({}), target_size=4.0,
                           tolerance=0.1, pdi_cap=0.05, loss_transform="log1p")
    y = np.array([0.0, 0.5, 3.0, _FAIL_LOSS])
    t = c._transform(y)
    assert np.all(np.diff(t) > 0), "transform is not monotone"
    assert t[-1] < 10, "the failure sentinel still dominates the scale"
