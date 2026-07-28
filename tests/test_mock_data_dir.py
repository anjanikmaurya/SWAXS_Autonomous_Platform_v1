"""
tests/test_mock_data_dir.py — the SPEC save folder must be LOCAL in mock mode.

On the rig the hub's Windows folder is translated to the beamline Linux path via
spec.hub_path_map. In mock mode no SPEC is involved and the 2D simulator writes
with ordinary file I/O, so that translation must be skipped — otherwise the
simulator is handed /msd_data/... and can't write anything on a laptop.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.reactor.config import hub_to_spec_dir

HUB_WIN = r"X:\bl1-5\staff_data\Partha\AutoSynth\Auto_Test"
SPEC_LINUX = "/msd_data/checkout/bl1-5/staff_data/Partha/AutoSynth/Auto_Test"
MAP = {"from": "X:\\bl1-5", "to": "/msd_data/checkout/bl1-5"}


def _resolve(folder: str, backend: str, spec: dict) -> str:
    """Mirror of reactor/app.py::_sync_data_dir_from_hub's target selection."""
    if not folder:
        return ""
    if str(backend).lower() != "real":
        return str(spec.get("mock_data_dir", "") or "").strip() or folder
    if spec.get("data_dir_from_hub", True):
        mapped = hub_to_spec_dir(folder, spec.get("hub_path_map"))
        if mapped:
            return mapped
    return folder


BASE = {"data_dir_from_hub": True, "hub_path_map": MAP, "mock_data_dir": ""}


def test_real_backend_translates_to_the_beamline_path():
    assert _resolve(HUB_WIN, "real", BASE) == SPEC_LINUX


def test_mock_backend_keeps_the_local_hub_folder():
    """The whole point: no /msd_data/... path on a laptop."""
    assert _resolve(HUB_WIN, "mock", BASE) == HUB_WIN
    assert not _resolve(HUB_WIN, "mock", BASE).startswith("/msd_data")


def test_mock_backend_passes_through_a_posix_hub_folder(tmp_path):
    local = str(tmp_path / "project")
    assert _resolve(local, "mock", BASE) == local


def test_mock_data_dir_override_pins_the_output_folder(tmp_path):
    spec = {**BASE, "mock_data_dir": str(tmp_path / "sim_out")}
    assert _resolve(HUB_WIN, "mock", spec) == str(tmp_path / "sim_out")


def test_mock_data_dir_override_is_ignored_on_real_hardware(tmp_path):
    """A leftover testing override must never redirect real beamtime data."""
    spec = {**BASE, "mock_data_dir": str(tmp_path / "sim_out")}
    assert _resolve(HUB_WIN, "real", spec) == SPEC_LINUX


def test_blank_folder_is_a_noop():
    assert _resolve("", "mock", BASE) == ""


# ── the 2D/SAXS tree is created on demand ────────────────────────────────────
def _collect(folder, **sim_over):
    from src.beamline import make_beamline
    sim = {"enabled": True, "speed_factor": 0, "shape": [64, 64]}
    sim.update(sim_over)
    bl = make_beamline({"spec": {"backend": "mock", "simulator": sim}})
    bl.set_recipe({"T_reac": 240.0, "x_TOP": 0.15, "F_tot": 80.0, "recipe_id": "r1"})
    bl.collect(recipe_id="r1", role="sample", sample="r1_sample",
               main_folder=str(folder), temperature=240.0, exposure=1.0, frames=2)
    return bl.simulator.history[0]


def test_creates_2d_saxs_tree_when_nothing_exists(tmp_path):
    """The headline request: a bare project folder must get 2D/SAXS made for it."""
    pytest.importorskip("pyFAI")
    proj = tmp_path / "hub_project"
    proj.mkdir()
    assert not (proj / "2D").exists()

    rec = _collect(proj)

    assert (proj / "2D" / "SAXS").is_dir(), "2D/SAXS was not created"
    assert rec["created_dirs"] is True
    assert len(sorted((proj / "2D" / "SAXS").glob("*.raw"))) == 2
    assert (proj / "2D" / "r1_sample.csv").is_file()   # CSV one level above SAXS/


def test_missing_intermediate_folders_are_created(tmp_path):
    """Save folder itself doesn't exist yet — build the whole path."""
    pytest.importorskip("pyFAI")
    proj = tmp_path / "does" / "not" / "exist"
    rec = _collect(proj)
    assert (proj / "2D" / "SAXS").is_dir()
    assert rec["n_frames"] == 2


def test_existing_2d_folder_is_reused_not_nested(tmp_path):
    pytest.importorskip("pyFAI")
    proj = tmp_path / "hub_project"
    (proj / "2D").mkdir(parents=True)
    _collect(proj)
    assert (proj / "2D" / "SAXS").is_dir()
    assert not (proj / "2D" / "2D").exists()


def test_folder_named_2d_does_not_get_a_nested_2d(tmp_path):
    pytest.importorskip("pyFAI")
    two_d = tmp_path / "2D"
    two_d.mkdir()
    _collect(two_d)
    assert (two_d / "SAXS").is_dir() and not (two_d / "2D").exists()


def test_rig_style_folder_holding_saxs_is_used_as_the_2d_base(tmp_path):
    """On the rig data_dir IS the 2D base — don't bury frames in a new 2D/."""
    pytest.importorskip("pyFAI")
    base = tmp_path / "Auto_Test"
    (base / "SAXS").mkdir(parents=True)
    rec = _collect(base)
    assert rec["two_d_dir"] == str(base)
    assert len(sorted((base / "SAXS").glob("*.raw"))) == 2
    assert not (base / "2D").exists()


def test_two_d_subdir_can_be_forced_flat(tmp_path):
    pytest.importorskip("pyFAI")
    proj = tmp_path / "flat"
    rec = _collect(proj, two_d_subdir="")
    assert rec["two_d_dir"] == str(proj)
    assert (proj / "SAXS").is_dir() and not (proj / "2D").exists()


def test_second_collection_reports_dirs_already_present(tmp_path):
    pytest.importorskip("pyFAI")
    proj = tmp_path / "hub_project"
    assert _collect(proj)["created_dirs"] is True
    assert _collect(proj)["created_dirs"] is False     # idempotent
