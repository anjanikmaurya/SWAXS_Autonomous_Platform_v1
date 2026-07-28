"""
tests/test_simulator_reduction_metadata.py — does the simulator produce enough
METADATA for the real reduction pipeline to succeed?

Runs the actual ``run_pipeline`` over simulated frames and checks that the CSV
sidecar is discovered, i0/bstop/temperature are parsed, the transmission comes
out as configured, and the .dat carries the provenance downstream apps rely on.

Needs pyFAI + xraydb + pandas; skipped otherwise.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyFAI")
pytest.importorskip("pandas")
pytest.importorskip("fabio")

from src.beamline import make_beamline                              # noqa: E402
from src.reduction.core import run_pipeline                         # noqa: E402
from src.utils.read_dat_metadata import read_dat_data_metadata      # noqa: E402

SHAPE = [200, 200]
TRANSMISSION = 0.62

PONI = ('poni_version: 2\nDetector: Detector\n'
        'Detector_config: {"pixel1": 1.72e-4, "pixel2": 1.72e-4, "max_shape": [200, 200]}\n'
        'Distance: 1.5\nPoni1: 0.0172\nPoni2: 0.0172\n'
        'Rot1: 0.0\nRot2: 0.0\nRot3: 0.0\nWavelength: 1.033e-10\n')


@pytest.fixture(scope="module")
def reduced(tmp_path_factory):
    """Simulate an acquisition, then run the REAL reduction pipeline on it."""
    proj = tmp_path_factory.mktemp("proj")
    two_d, poni_dir = proj / "2D", proj / "poni"
    poni_dir.mkdir()
    (poni_dir / "s.poni").write_text(PONI)

    cfg = {"data_directory": str(two_d), "poni_directory": str(poni_dir),
           "compound": "C2H4", "energy_keV": 12.0, "density_g_cm3": 0.92,
           "thickness": 0.001, "mode": "SAXS", "metadata_format": "csv",
           "detector_shapes": {"saxs": SHAPE},
           "poni_files": {"saxs": "s.poni"}, "mask_files": {"saxs": None},
           "i0_offset": 0.0, "bstop_offset": 0.0, "i0_air": 0.0, "bstop_air": 0.0,
           "npt_radial": 300, "error_model": "poisson", "unit": "q_nm^-1",
           "normalization": ["bstop"],
           "beamline": {"type": "1-5", "data_format": "raw"}}

    bl = make_beamline({"spec": {"backend": "mock", "simulator": {
        "enabled": True, "speed_factor": 0, "shape": SHAPE,
        "poni": str(poni_dir / "s.poni"), "metadata_format": "csv",
        "transmission": TRANSMISSION}}})
    bl.set_recipe({"T_reac": 240.0, "x_TOP": 0.15, "F_tot": 80.0, "recipe_id": "r001"})
    for role, tag in (("sample", "r001_sample"), ("background", "r001_bkg")):
        bl.collect(recipe_id="r001", role=role, sample=tag, main_folder=str(two_d),
                   temperature=240.0, exposure=5.0, frames=2)

    logs = []
    res = run_pipeline(cfg, log_callback=lambda m, t="info": logs.append((t, m)))
    dats = sorted((proj / "1D" / "SAXS" / "Reduction").glob("*.dat"))
    return {"proj": proj, "res": res, "logs": logs, "dats": dats,
            "truth": bl.simulator.history[0]["truth"]}


def test_reduction_processes_every_simulated_frame(reduced):
    assert reduced["res"]["saxs_count"] == 4          # 2 sample + 2 background
    assert len(reduced["dats"]) == 4
    assert not reduced["res"]["stopped"]


def test_no_metadata_errors_were_logged(reduced):
    bad = [m for t, m in reduced["logs"]
           if t == "error" or "No matching CSV" in m or "not found in metadata" in m]
    assert not bad, bad


def test_csv_sidecar_is_discovered_and_counters_parsed(reduced):
    _, _, _, _, meta = read_dat_data_metadata(reduced["dats"][0])
    assert float(meta["i0"]) > 0
    assert float(meta["bstop"]) > 0
    assert "temp" in meta                              # CTEMP path for the reactor


def test_transmission_matches_the_configured_value(reduced):
    """bstop/i0 must reproduce simulator.transmission — this is what makes the
    reduction app's transmission and Beer-Lambert thickness meaningful."""
    _, _, _, _, meta = read_dat_data_metadata(reduced["dats"][0])
    assert float(meta["bstop"]) / float(meta["i0"]) == pytest.approx(TRANSMISSION, abs=1e-3)


def test_transmission_stays_below_one_so_no_spurious_warning(reduced):
    """Regression against the T_sample > 1 warning: a simulated bstop/i0 above 1
    would make thickness derivation meaningless."""
    warns = [m for t, m in reduced["logs"] if "T_sample" in m and "> 1.0" in m]
    assert not warns, warns


def test_dat_carries_geometry_and_normalisation_provenance(reduced):
    text = reduced["dats"][0].read_text()
    assert "Normalization factor" in text
    assert "wavelength" in text and "dist" in text     # poni geometry echoed
    assert "q_nm^-1" in text                           # unit the analyzer expects


def test_dat_q_and_intensity_are_usable(reduced):
    _, q, I, sigma, _ = read_dat_data_metadata(reduced["dats"][0])
    q, I = np.asarray(q, float), np.asarray(I, float)
    assert q.size > 50 and np.all(np.diff(q) > 0)      # sorted, non-trivial
    assert np.isfinite(I).all() and (I > 0).any()


def test_recipe_id_survives_into_the_filename_for_optimizer_feedback(reduced):
    """The optimizer maps results back to the recipe purely by filename, so the
    simulator must reproduce the reactor's {recipe_id}_{tag} convention."""
    from src.optimizer.io import match_recipe_id
    names = [p.name for p in reduced["dats"]]
    assert all(match_recipe_id(n, ["r001"]) == "r001" for n in names), names


def test_sample_and_background_are_both_present_and_pairable(reduced):
    names = [p.name for p in reduced["dats"]]
    assert any("r001_sample" in n for n in names)
    assert any("r001_bkg" in n for n in names)
