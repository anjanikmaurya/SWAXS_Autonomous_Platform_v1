"""
tests/test_simulator.py — synthetic 2D SAXS generator (src/simulator).

Covers the ground-truth landscape (interior optimum, clamps, reproducibility),
the scattering physics (form factor limits, polydispersity, background), and the
file output (.raw readable by the REAL reduction reader, CSV/PDI metadata,
sample-vs-background pairing).

The closed-loop check — injected radius recovered by the real analyzer — lives
in test_simulator_closed_loop.py, which needs scipy.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.simulator import (truth_from_recipe, sphere_form_factor, schulz_weights,
                           iq_curve, background_curve, synthetic_q_map,
                           beamstop_mask, simulate_frame, frame_name, write_raw,
                           counters, write_csv_metadata, write_pdi_metadata,
                           SimulatedCollector)

NO_NOISE = {"noise_R_frac": 0.0, "noise_pdi": 0.0}


# ── ground truth: the landscape the optimizer must discover ───────────────────
def test_optimum_reproduces_configured_size_and_pdi():
    # optimum defaults sit interior to the platform's recipe bounds:
    # T_reac 180-300, x_each 0-0.3, F_tot 40-120 (F_ref = 80)
    t = truth_from_recipe({"T_reac": 240.0, "x_TOP": 0.15, "F_tot": 80.0}, NO_NOISE)
    assert t["R_nm"] == pytest.approx(4.0, abs=1e-6)
    assert t["pdi"] == pytest.approx(0.02, abs=1e-6)
    assert t["distance_from_optimum"] == pytest.approx(0.0, abs=1e-9)


def test_pdi_has_strict_interior_minimum_in_T():
    pdi = lambda T: truth_from_recipe({"T_reac": T, "x_TOP": 0.15, "F_tot": 80.0},
                                      NO_NOISE)["pdi"]
    best = min(range(200, 281, 2), key=pdi)
    assert best == pytest.approx(240, abs=2)
    assert pdi(240) < pdi(220) and pdi(240) < pdi(260)


def test_pdi_has_strict_interior_minimum_in_x_TOP():
    pdi = lambda x: truth_from_recipe({"T_reac": 240.0, "x_TOP": x, "F_tot": 80.0},
                                      NO_NOISE)["pdi"]
    assert pdi(0.15) < pdi(0.05) and pdi(0.15) < pdi(0.28)


def test_size_responds_monotonically_to_temperature_and_ligand():
    R = lambda T, x: truth_from_recipe({"T_reac": T, "x_TOP": x, "F_tot": 80.0},
                                       NO_NOISE)["R_nm"]
    assert R(260, 0.15) > R(240, 0.15) > R(220, 0.15)      # hotter → bigger
    assert R(240, 0.05) > R(240, 0.15) > R(240, 0.25)      # more ligand → smaller


def test_slower_flow_grows_particles():
    R = lambda F: truth_from_recipe({"T_reac": 240.0, "x_TOP": 0.15, "F_tot": F},
                                    NO_NOISE)["R_nm"]
    assert R(40.0) > R(80.0) > R(120.0)                   # longer residence → bigger


def test_values_are_clamped_to_the_interview_ranges():
    hot = truth_from_recipe({"T_reac": 600.0, "x_TOP": 0.0, "F_tot": 10.0}, NO_NOISE)
    cold = truth_from_recipe({"T_reac": 0.0, "x_TOP": 1.0, "F_tot": 5000.0}, NO_NOISE)
    assert 1.0 <= hot["R_nm"] <= 10.0 and 1.0 <= cold["R_nm"] <= 10.0
    assert 0.001 <= hot["pdi"] <= 0.5 and 0.001 <= cold["pdi"] <= 0.5
    assert hot["clamped"] and cold["clamped"]


def test_same_recipe_id_is_reproducible_but_ids_differ():
    a = truth_from_recipe({"T_reac": 245.0, "x_TOP": 0.16, "recipe_id": "r1"})
    b = truth_from_recipe({"T_reac": 245.0, "x_TOP": 0.16, "recipe_id": "r1"})
    c = truth_from_recipe({"T_reac": 245.0, "x_TOP": 0.16, "recipe_id": "r2"})
    assert a["R_nm"] == b["R_nm"]                    # reruns reproduce exactly
    assert a["R_nm"] != c["R_nm"]                    # different run → new scatter


def test_missing_recipe_fields_fall_back_to_the_optimum():
    t = truth_from_recipe({}, NO_NOISE)
    assert t["R_nm"] == pytest.approx(4.0, abs=1e-6)


# ── scattering physics ────────────────────────────────────────────────────────
def test_form_factor_is_unity_at_zero_q_and_decays():
    assert sphere_form_factor([0.0], 4.0)[0] == pytest.approx(1.0)
    p = sphere_form_factor(np.array([0.01, 0.5, 2.0]), 4.0)
    assert p[0] > p[1] > p[2]


def test_form_factor_first_minimum_is_near_qR_4_493():
    # Window around the FIRST zero only: the sphere form factor also vanishes at
    # qR = 7.725, and both are ~0, so a global argmin flips between them.
    R = 4.0
    q = np.linspace(3.0 / R, 6.0 / R, 4000)
    qR = q[np.argmin(sphere_form_factor(q, R))] * R
    assert qR == pytest.approx(4.493, abs=0.05)      # known sphere zero


def test_schulz_weights_normalise_and_widen_with_pdi():
    _, w = schulz_weights(4.0, 0.2)
    assert w.sum() == pytest.approx(1.0)
    r_narrow, _ = schulz_weights(4.0, 0.05)
    r_wide, _ = schulz_weights(4.0, 0.35)
    assert np.ptp(r_wide) > np.ptp(r_narrow)
    assert len(schulz_weights(4.0, 0.001)[0]) == 1   # monodisperse → single radius


def test_polydispersity_smears_the_form_factor_minimum():
    q = np.linspace(0.05, 3.0, 900)
    sharp = iq_curve(q, 4.0, 0.001, scale=100.0)
    smeared = iq_curve(q, 4.0, 0.30, scale=100.0)
    # the deep first minimum fills in when the sample is polydisperse
    assert smeared.min() / smeared.max() > sharp.min() / sharp.max()


def test_background_curve_is_finite_at_low_q_and_decays():
    b = background_curve(np.array([1e-4, 0.02, 0.1, 1.0]))
    assert np.all(np.isfinite(b)) and b[0] > b[1] > b[2] > b[3]


def test_sample_contains_the_same_background_so_subtraction_isolates_particles():
    q = np.linspace(0.03, 2.0, 600)
    bkg = background_curve(q)
    sample = iq_curve(q, 4.0, 0.05, scale=800.0, bkg=bkg)
    particles_only = sample - bkg
    expected = iq_curve(q, 4.0, 0.05, scale=800.0, bkg=0.0)
    assert np.allclose(particles_only, expected)


# ── frame synthesis ───────────────────────────────────────────────────────────
def test_frame_is_int32_with_beamstop_and_mask_zeroed():
    shape = (128, 128)
    q = synthetic_q_map(shape, dist_m=1.0)
    bs = beamstop_mask(shape, q, q_beamstop=float(np.median(q)))
    mask = np.zeros(shape, bool); mask[0:4, :] = True
    img = simulate_frame(q, 4.0, 0.1, exposure_s=1.0, beamstop=bs, mask=mask,
                         rng=np.random.default_rng(0))
    assert img.dtype == np.int32 and img.shape == shape
    assert img[bs].max() == 0 and img[mask].max() == 0
    assert img[~(bs | mask)].sum() > 0


def test_background_frame_is_weaker_than_the_sample_frame():
    shape = (96, 96)
    q = synthetic_q_map(shape)
    kw = dict(exposure_s=5.0, scale=800.0, rng=np.random.default_rng(1))
    sample = simulate_frame(q, 4.0, 0.1, particles=True, **kw)
    bkg = simulate_frame(q, 4.0, 0.1, particles=False,
                         rng=np.random.default_rng(1), exposure_s=5.0, scale=800.0)
    assert sample.sum() > bkg.sum()


def test_longer_exposure_gives_more_counts():
    # A beamstop is always present in practice (the collector passes one). Without
    # it the q→0 centre pixel saturates the count clip and swamps the total.
    q = synthetic_q_map((64, 64))
    bs = beamstop_mask((64, 64), q, q_beamstop=0.05)
    kw = dict(beamstop=bs, scale=800.0)
    short = simulate_frame(q, 4.0, 0.1, exposure_s=1.0,
                           rng=np.random.default_rng(2), **kw)
    long = simulate_frame(q, 4.0, 0.1, exposure_s=10.0,
                          rng=np.random.default_rng(2), **kw)
    assert long.sum() > 5 * short.sum()


def test_beamstop_prevents_centre_saturation():
    """Regression: the unmasked q→0 pixel hits the count clip; the beamstop
    (always applied by the collector) must remove it."""
    shape = (64, 64)
    q = synthetic_q_map(shape)
    bs = beamstop_mask(shape, q, q_beamstop=0.05)
    img = simulate_frame(q, 4.0, 0.1, exposure_s=1.0, beamstop=bs,
                         rng=np.random.default_rng(3))
    assert img.max() < 2_000_000 and bs.sum() > 0


# ── file output ───────────────────────────────────────────────────────────────
def test_frame_name_matches_the_beamline_convention():
    assert frame_name("r001_sample", 0) == "r001_sample_scan1_0000.raw"
    assert frame_name("r001_sample", 12) == "r001_sample_scan1_0012.raw"


def test_write_raw_roundtrips_and_leaves_no_part_file(tmp_path):
    img = (np.arange(64, dtype=np.int32) * 3).reshape(8, 8)
    p = write_raw(tmp_path / "SAXS" / "x_scan1_0000.raw", img)
    back = np.fromfile(str(p), dtype=np.int32).reshape(8, 8)
    assert np.array_equal(img, back)
    assert not list(tmp_path.rglob("*.part"))


def test_counters_encode_the_requested_transmission():
    c = counters(i0=1.0e6, transmission=0.62)
    assert c["bstop"] / c["i0"] == pytest.approx(0.62, abs=1e-6)


def test_csv_metadata_has_one_row_per_frame(tmp_path):
    rows = [counters(temperature=240.0) for _ in range(4)]
    out = write_csv_metadata(tmp_path, "r001_sample", rows)
    assert out.name == "r001_sample.csv"
    lines = out.read_text().strip().splitlines()
    assert lines[0].split(",") == ["i0", "bstop", "temp"] and len(lines) == 5


def test_pdi_sidecar_is_parseable_by_the_reduction_parser(tmp_path):
    raw = tmp_path / "a_scan1_0000.raw"; raw.write_bytes(b"")
    p = write_pdi_metadata(raw, counters())
    assert p.name == "a_scan1_0000.raw.pdi"
    from src.reduction.process_metadata import get_meta_from_pdi
    ctr, motors, _ = get_meta_from_pdi(str(p))
    assert ctr["i0"] > 0 and ctr["bstop"] > 0


# ── collector orchestration ───────────────────────────────────────────────────
def _collector(tmp_path, **over):
    cfg = {"enabled": True, "speed_factor": 0, "metadata_format": "csv",
           "shape": [96, 96]}
    cfg.update(over)
    return SimulatedCollector(cfg, project_root=str(tmp_path))


def test_collect_writes_frames_metadata_and_reports_truth(tmp_path):
    sim = _collector(tmp_path)
    sim.set_recipe({"T_reac": 240.0, "x_TOP": 0.15, "F_tot": 80.0, "recipe_id": "r1"})
    rec = sim.collect(prefix="r1_sample", role="sample", data_dir=str(tmp_path / "2D"),
                      exposure=1.0, frames=3, recipe_id="r1")
    assert rec["n_frames"] == 3
    assert len(list((tmp_path / "2D" / "SAXS").glob("r1_sample_scan1_*.raw"))) == 3
    assert (tmp_path / "2D" / "r1_sample.csv").is_file()
    assert rec["truth"]["R_nm"] == pytest.approx(4.0, abs=0.3)


def test_background_role_reports_no_truth_and_pairs_by_recipe_id(tmp_path):
    sim = _collector(tmp_path)
    sim.set_recipe({"T_reac": 240.0, "x_TOP": 0.15, "recipe_id": "r1"})
    s = sim.collect(prefix="r1_sample", role="sample", data_dir=str(tmp_path / "2D"),
                    exposure=1.0, frames=2, recipe_id="r1")
    b = sim.collect(prefix="r1_bkg", role="background", data_dir=str(tmp_path / "2D"),
                    exposure=1.0, frames=2, recipe_id="r1")
    assert s["truth"] is not None and b["truth"] is None
    assert s["recipe_id"] == b["recipe_id"] == "r1"   # shared id → auto-pairing


def test_collector_works_without_a_poni(tmp_path):
    """Must stay usable before calibration has been done."""
    rec = _collector(tmp_path)
    rec.set_recipe({"T_reac": 240.0, "x_TOP": 0.15, "recipe_id": "r1"})
    out = rec.collect(prefix="p", role="sample", data_dir=str(tmp_path / "2D"),
                      exposure=1.0, frames=1)
    assert "synthetic geometry" in out["geometry"] and out["n_frames"] == 1


def test_recipe_changes_change_the_generated_particles(tmp_path):
    sim = _collector(tmp_path)
    sizes = []
    for i, T in enumerate((220.0, 260.0)):
        sim.set_recipe({"T_reac": T, "x_TOP": 0.15, "F_tot": 80.0, "recipe_id": f"r{i}"})
        r = sim.collect(prefix=f"r{i}", role="sample", data_dir=str(tmp_path / "2D"),
                        exposure=1.0, frames=1, recipe_id=f"r{i}")
        sizes.append(r["truth"]["R_nm"])
    assert sizes[1] > sizes[0]      # the optimizer will see a real landscape
