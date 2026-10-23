"""Structure fitter, Phase 1 (docs/design/LLM_FITTER_PLAN.md): curve features,
pipeline context, BIC in sasmodels_fit, the model catalogue."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.features import compute_features, oscillation_minima  # noqa: E402
from src.analysis.fit_context import collect, write_chemistry_template  # noqa: E402
from src.analysis.nanoparticle import model_intensity  # noqa: E402

Q = np.geomspace(0.05, 5, 400)


def _noisy(I, seed=1):
    rng = np.random.default_rng(seed)
    s = 0.02 * I + 1e-5
    return np.abs(I + rng.normal(0, s)), s


# ── features ──────────────────────────────────────────────────────────────────

def test_sphere_minima_give_the_radius():
    I, s = _noisy(model_intensity(Q, 5.0, 0.05, 1, 1e-4, "schulz"))
    f = compute_features(Q, I, s)
    assert f["minima"]["sphere_R_from_first_min"] == pytest.approx(5.0, rel=0.05)
    assert len(f["minima"]["q_min"]) >= 3
    assert f["guinier"]["valid"] and f["guinier"]["Rg"] == pytest.approx(np.sqrt(0.6) * 5, rel=0.08)
    assert f["porod"]["n"] == pytest.approx(-4, abs=0.4)
    assert not f["warnings"]


def test_polydispersity_smears_minima():
    sharp = oscillation_minima(Q, model_intensity(Q, 5.0, 0.05, 1, 1e-4, "schulz"))
    broad = oscillation_minima(Q, model_intensity(Q, 5.0, 0.30, 1, 1e-4, "schulz"))
    assert len(broad["q_min"]) < len(sharp["q_min"])


def test_flat_noise_is_not_a_guinier_region_and_has_no_minima():
    I, s = _noisy(np.ones_like(Q))
    f = compute_features(Q, I, s)
    assert f["guinier"]["valid"] is False
    assert f["minima"]["q_min"] == []


def test_aggregation_shows_in_the_low_q_slope():
    base = model_intensity(Q, 5.0, 0.05, 1, 1e-4, "schulz")
    agg = base + base[0] * 5 * (Q / Q[0]) ** -3
    assert compute_features(Q, *_noisy(agg))["lowq"]["slope"] < -1.5
    assert abs(compute_features(Q, *_noisy(base))["lowq"]["slope"]) < 0.3


def test_features_never_raise_and_are_json_safe():
    json.dumps(compute_features(Q[:5], np.ones(5)))
    json.dumps(compute_features(Q, *_noisy(model_intensity(Q, 3, 0.1, 1, 1e-3, "schulz"))))


# ── sasmodels_fit reports model-comparison statistics ────────────────────────

def test_sasmodels_fit_reports_bic_aic_and_counts():
    pytest.importorskip("sasmodels")
    from src.analysis.core import sasmodels_fit
    I, s = _noisy(model_intensity(Q, 5.0, 0.05, 1, 1e-4, "schulz"))
    r = sasmodels_fit(Q, I, s, "sphere", {"radius": 50, "scale": 0.01, "background": 1e-4},
                      free=["radius"])
    assert "error" not in r
    assert r["n_free"] == 1 and r["n_points"] == len(Q)
    assert r["bic"] == pytest.approx(r["chi2_total"] + np.log(len(Q)), rel=1e-6)
    assert r["aic"] == pytest.approx(r["chi2_total"] + 2, rel=1e-6)
    assert isinstance(r["errors"], dict)


# ── catalogue ─────────────────────────────────────────────────────────────────

def test_catalogue_names_real_sasmodels_parameters():
    pytest.importorskip("sasmodels")
    from sasmodels.core import load_model_info
    cat = yaml.safe_load((ROOT / "ai_knowledge" / "rules" / "saxs_models.yml").read_text())
    for sect in ("models", "structure_factors"):
        for name, m in cat[sect].items():
            have = {p.name for p in load_model_info(name).parameters.kernel_parameters}
            need = set(m.get("fit", [])) | set(m.get("pd", [])) | set(m.get("bounds", {})) \
                | set(m.get("fixed") or {})
            assert need <= have, (name, need - have)
    assert set(cat["models"]) == {"sphere", "core_shell_sphere", "ellipsoid",
                                  "cylinder", "mass_fractal"}


def test_reduction_records_the_normalisation_mode():
    src = (ROOT / "reduction" / "app.py").read_text()
    assert '"normalization",' in src and '"absolute_calibration_factor"' in src


# ── pipeline context ──────────────────────────────────────────────────────────

def _project(tmp_path):
    root = tmp_path / "proj"
    sub = root / "1D" / "SAXS" / "Subtracted"
    (sub / "Good").mkdir(parents=True)
    flat = sub / "r007_sample_sub.dat"
    flat.write_text("# q I sigma\n")
    (sub / "Good" / flat.name).write_text("# q I sigma\n")
    (root / "reactor" / "feedback").mkdir(parents=True)
    (root / "reactor" / "feedback" / "r007.done.json").write_text(json.dumps(
        {"recipe": {"T_reac": 240, "F_tot": 30}, "measured_flows": {"top": 5}}))
    (root / "config.yml").write_text(yaml.safe_dump(
        {"normalization": ["absolute"], "absolute_calibration_factor": 2.5,
         "unit": "q_nm^-1", "compound": "C18H36"}))
    (root / "chemistry.yml").write_text(yaml.safe_dump(
        {"product": "PbS quantum dots", "expected_shape": "spheres", "solvent": ""}))
    key = str(flat.resolve())
    manifest = {"background": {key: {"scale": 0.97, "qc_verdict": "ok",
                                     "qc": {"pct_negative": 0.4, "lowq_slope": -0.2}}},
                "quality": {key: {"score": 82, "verdict": "good", "flags": ["low_snr"]}}}
    return root, flat, manifest


def test_context_collects_every_step(tmp_path):
    root, flat, manifest = _project(tmp_path)
    ctx = collect(flat.parent / "Good" / flat.name, manifest=manifest)
    assert ctx["quality_folder"] == "Good"
    assert ctx["recipe_id"] == "r007" and ctx["recipe"]["T_reac"] == 240
    assert ctx["chemistry"] == {"product": "PbS quantum dots", "expected_shape": "spheres"}
    assert ctx["reduction"]["absolute_units"] is True
    assert ctx["background"]["scale"] == 0.97 and ctx["background"]["pct_negative"] == 0.4
    assert ctx["quality"]["flags"] == ["low_snr"]
    json.dumps(ctx)


def test_context_is_partial_not_failing_when_pieces_are_missing(tmp_path):
    p = tmp_path / "loose.dat"
    p.write_text("")
    ctx = collect(p, manifest={})
    assert ctx["file"] == "loose.dat"


def test_chemistry_template_is_created_once(tmp_path):
    p = write_chemistry_template(tmp_path)
    assert "expected_shape" in p.read_text()
    p.write_text("product: x\n")
    write_chemistry_template(tmp_path)
    assert p.read_text() == "product: x\n"
