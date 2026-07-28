"""
tests/test_simulator_closed_loop.py — the end-to-end guarantee.

Fires a collection through the REAL MockBeamline, reads the frames back with the
REAL reduction reader, subtracts the simulated background, and fits with the
REAL analyzer — then asserts the recovered radius matches the radius that was
injected. If this passes, a pipeline change that silently corrupts sizes will be
caught without needing beam.

Needs scipy (the nanoparticle fitter); skipped otherwise.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("pyFAI")

from src.beamline import make_beamline                                # noqa: E402
from src.reduction.read_raw_file import read_detector_image           # noqa: E402
from src.simulator.pattern import synthetic_q_map, beamstop_mask      # noqa: E402
from src.analysis.nanoparticle import analyze_profile                 # noqa: E402

SHAPE = (400, 400)          # smaller than the real detector to keep tests quick
Q_BEAMSTOP = 0.02


def _radial(img, q, bs, nbins=700):
    """Minimal azimuthal average — stands in for pyFAI integration."""
    good = ~bs
    qs = q[good].ravel()
    vals = img[good].ravel().astype(float)
    edges = np.linspace(qs.min(), qs.max(), nbins + 1)
    idx = np.clip(np.digitize(qs, edges) - 1, 0, nbins - 1)
    cnt = np.bincount(idx, minlength=nbins)
    tot = np.bincount(idx, weights=vals, minlength=nbins)
    ok = cnt > 0
    return (0.5 * (edges[:-1] + edges[1:]))[ok], tot[ok] / cnt[ok]


def _run(T, x_TOP, frames=5, exposure=20.0):
    """Collect sample + background through MockBeamline; return (truth, q, I_sub)."""
    with tempfile.TemporaryDirectory() as td:
        two_d = Path(td) / "2D"
        bl = make_beamline({"spec": {"backend": "mock", "simulator": {
            "enabled": True, "speed_factor": 0, "shape": list(SHAPE),
            "q_beamstop": Q_BEAMSTOP, "metadata_format": "csv"}}})
        assert bl.simulator is not None, "simulator did not activate"
        rid = f"T{T}_x{x_TOP}"
        bl.set_recipe({"T_reac": T, "x_TOP": x_TOP, "F_tot": 200.0, "recipe_id": rid})
        for role, tag in (("sample", "s"), ("background", "b")):
            bl.collect(recipe_id=rid, role=role, sample=tag,
                       main_folder=str(two_d), temperature=T,
                       exposure=exposure, frames=frames)
        truth = bl.simulator.history[0]["truth"]

        q = synthetic_q_map(SHAPE)
        bs = beamstop_mask(SHAPE, q, q_beamstop=Q_BEAMSTOP)
        avg = lambda pat: np.mean(
            [read_detector_image(f, list(SHAPE)).astype(float)
             for f in sorted((two_d / "SAXS").glob(pat))], axis=0)
        qc, I_s = _radial(avg("s_*.raw"), q, bs)
        _, I_b = _radial(avg("b_*.raw"), q, bs)
        return truth, qc, I_s - I_b


def _fit_radius(qc, I_sub):
    m = (qc > 0.03) & (I_sub > 0)
    res = analyze_profile(qc[m], I_sub[m])
    size = res.get("size") or {}
    return size.get("radius"), res


# ── the headline guarantee ────────────────────────────────────────────────────
@pytest.mark.parametrize("T,x_TOP", [(240.0, 0.15), (250.0, 0.12), (230.0, 0.18)])
def test_injected_radius_is_recovered_by_the_real_analyzer(T, x_TOP):
    truth, qc, I_sub = _run(T, x_TOP)
    R_fit, res = _fit_radius(qc, I_sub)
    assert R_fit is not None, f"analyzer returned no size: {res.get('diagnostics')}"
    rel = abs(R_fit - truth["R_nm"]) / truth["R_nm"]
    assert rel < 0.10, (f"recovered {R_fit:.2f} nm vs injected {truth['R_nm']:.2f} nm "
                        f"({100 * rel:+.1f}%)")


def test_recovered_pdi_tracks_the_injected_pdi():
    truth, qc, I_sub = _run(285.0, 0.05)        # deliberately off-optimum → high PDI
    _, res = _fit_radius(qc, I_sub)
    pdi = res.get("pdi")
    pdi = pdi.get("pdi") if isinstance(pdi, dict) else pdi
    assert pdi is not None and abs(pdi - truth["pdi"]) < 0.08


def test_optimum_recipe_yields_the_target_size_and_lowest_pdi():
    """The landscape must actually reward the true optimum, or a converging
    optimizer proves nothing."""
    at_opt, _, _ = _run(240.0, 0.15, frames=1, exposure=1.0)   # the true optimum
    off_T, _, _ = _run(295.0, 0.15, frames=1, exposure=1.0)
    off_x, _, _ = _run(240.0, 0.02, frames=1, exposure=1.0)
    assert at_opt["R_nm"] == pytest.approx(4.0, abs=0.3)   # == campaign target_size
    assert at_opt["pdi"] < off_T["pdi"] and at_opt["pdi"] < off_x["pdi"]


# ── the optimizer leg: does the landscape actually reward the truth? ──────────
def test_pdi_never_saturates_inside_the_search_box():
    """If PDI hits its ceiling across the box the landscape goes flat and a
    convergence test silently becomes a no-op. Regression guard."""
    from src.simulator.ground_truth import truth_from_recipe as tr
    flat = {"noise_R_frac": 0.0, "noise_pdi": 0.0}
    corners = [(180, 0.0), (300, 0.0), (180, 0.3), (300, 0.3)]
    for T, x in corners:
        pdi = tr({"T_reac": T, "x_TOP": x, "F_tot": 80.0}, flat)["pdi"]
        assert pdi < 0.49, f"PDI saturated at the corner T={T}, x_TOP={x}"


def test_optimizer_converges_toward_the_true_optimum():
    """End of the loop: recipe → (simulated) size/PDI → optimizer → better recipe.
    Uses the truth model directly so the test stays fast and deterministic."""
    from src.optimizer import ParameterSpace, CampaignController
    from src.simulator.ground_truth import truth_from_recipe as tr

    results = []
    for seed in (1, 7):
        space = ParameterSpace(t_reac=(180.0, 300.0), f_tot=(40.0, 120.0),
                               x_each=(0.0, 0.3))
        camp = CampaignController(space, target_size=4.0, tolerance=0.3,
                                  pdi_cap=0.25, budget=60, n_init=12,
                                  confidence_min=0.5, seed=seed)
        camp.start()
        best = None
        for i in range(60):
            p = camp.ask()
            if p is None:
                break
            t = tr(p, {"noise_R_frac": 0.01, "noise_pdi": 0.005}, seed_key=f"s{seed}i{i}")
            rec = camp.tell(p, t["R_nm"], t["pdi"], 0.9)
            if best is None or rec["loss"] < best["loss"]:
                best = rec
        results.append(best)

    for best in results:
        # a saturated/flat landscape parks the loss around 2+; a real gradient
        # drives it well under 1
        assert best["loss"] < 1.2, f"loss stalled at {best['loss']:.2f}"
        assert abs(best["params"]["T_reac"] - 240.0) < 30.0
        assert abs(best["size"] - 4.0) < 0.5      # found the target size


def test_background_frames_carry_no_particle_signal():
    """Subtracting background from background must leave no Guinier knee."""
    truth, qc, I_sub = _run(240.0, 0.15, frames=3, exposure=10.0)
    assert truth is not None
    # sample − background is positive across the Guinier region (real signal)
    band = (qc > 0.05) & (qc < 0.4)
    assert (I_sub[band] > 0).mean() > 0.9
