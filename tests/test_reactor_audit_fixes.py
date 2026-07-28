"""
tests/test_reactor_audit_fixes.py — regressions for the safety audit findings.

Each test corresponds to a defect that was CONFIRMED by reproduction before the
fix. They exist so the failure mode cannot silently return.
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from src.reactor.config import PUMP_NAMES
from src.reactor.controller import ReactorController
from src.reactor.hardware import PumpBank

BASE_CFG = {
    "pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES},
    "bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
               "x_each": [0, 0.3], "x_sum_max": 0.9},
    "run": {"default_duration": 5.0},
    # These tests exercise the control loop / E-stop, not 2D collection. With
    # collection enabled, background_when="before" inserts a blank flush ahead of
    # every run, which would delay the states they assert on.
    "spec": {"enabled": False},
}
RECIPE = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1,
          "x_oley": 0.1, "recipe_id": "r1"}


def _ctl(**over):
    cfg = {**BASE_CFG, **over}
    return ReactorController(cfg, backend="mock")


# ── backend string normalisation ──────────────────────────────────────────────
# Was: pumps compared `== "real"` but the beamline compared `.lower() == "real"`,
# so SWAXS_REACTOR_BACKEND=REAL gave a LIVE beamline with SIMULATED pumps.
@pytest.mark.parametrize("value", ["REAL", "Real", "real ", " real"])
def test_backend_case_variants_are_normalised_not_split(value):
    from src.beamline.driver import make_beamline
    bl = make_beamline({"spec": {"backend": value}})
    beamline_is_real = type(bl).__name__ == "SpecBeamline"
    # PumpBank must agree — it now normalises, so constructing it in the sandbox
    # attempts REAL serial ports and raises. Agreement is what we assert.
    try:
        PumpBank(BASE_CFG, backend=value)
        pumps_is_real = False            # mock pumps constructed
    except Exception as exc:
        pumps_is_real = "could not open" in str(exc) or "serial" in str(exc).lower()
    assert beamline_is_real == pumps_is_real, (
        f"{value!r} split the layers: beamline_real={beamline_is_real} "
        f"pumps_real={pumps_is_real}")


def test_unknown_backend_is_rejected_loudly():
    for bad in ("bogus", "", "simulate"):
        with pytest.raises(ValueError):
            ReactorController(BASE_CFG, backend=bad)


# ── the control loop must never die ───────────────────────────────────────────
# Was: an unguarded set_flow raise killed the loop thread; _safety_check stopped
# running while already-commanded pumps kept flowing, with no run deadline.
def test_pump_failure_does_not_kill_the_control_loop():
    ctl = _ctl()
    try:
        ctl.set_run_settings({"arm_mode": "timed", "arm_wait_s": "0", "run_duration": "5"})
        bad = list(ctl.pumps.pumps)[2]
        ctl.pumps.pumps[bad].set_flow = lambda rate: (_ for _ in ()).throw(
            RuntimeError("serial down"))
        ctl.submit(RECIPE)
        ctl.start()
        time.sleep(1.2)
        assert ctl._thread.is_alive(), "control loop died — no safety supervision"
        assert ctl.state == "estop", f"expected estop, got {ctl.state}"
        assert all(p.target == 0.0 for p in ctl.pumps.pumps.values()), \
            "pumps left commanded after the fault"
    finally:
        ctl.shutdown()


def test_set_all_is_guarded_per_pump_and_reports_failures():
    pb = PumpBank(BASE_CFG, backend="mock")
    bad = list(pb.pumps)[1]
    pb.pumps[bad].set_flow = lambda rate: (_ for _ in ()).throw(RuntimeError("x"))
    failed = pb.set_all({n: 10.0 for n in pb.pumps})
    assert failed == [bad]
    others = [n for n in pb.pumps if n != bad]
    assert all(pb.pumps[n].target == 10.0 for n in others), "other pumps not commanded"


# ── E-stop is latched ─────────────────────────────────────────────────────────
# Was: vent_all() unconditionally set state="idle", silently clearing a latched
# E-stop; with auto-run on, the folder watcher restarted into the live fault.
def test_vent_does_not_clear_a_latched_estop():
    ctl = _ctl()
    try:
        ctl.estop()
        assert ctl.state == "estop"
        ctl.vent_all()
        assert ctl.state == "estop", "vent cleared the E-stop"
        ctl.reset()
        assert ctl.state == "idle", "reset should be the deliberate way out"
    finally:
        ctl.shutdown()


def test_estop_disables_auto_run():
    ctl = _ctl()
    try:
        ctl.set_auto_run(True) if hasattr(ctl, "set_auto_run") else setattr(ctl, "auto_run", True)
        ctl.estop()
        assert ctl.auto_run is False, "auto-run would resubmit into the fault"
    finally:
        ctl.shutdown()


def test_estop_returns_the_pumps_it_could_not_idle():
    ctl = _ctl()
    try:
        bad = list(ctl.pumps.pumps)[0]
        # idle_all() calls idle_now() on RealPump and set_flow(0) on MockPump
        ctl.pumps.pumps[bad].set_flow = lambda rate: (_ for _ in ()).throw(RuntimeError("dead"))
        failed = ctl.estop()
        assert bad in (failed or []), "API would have reported a false success"
    finally:
        ctl.shutdown()


# ── no fabricated run records ─────────────────────────────────────────────────
# Was: aborting during `arming` emitted a .done.json with status="ran" carrying
# the PREVIOUS run's measured_flows — the optimizer would train on it.
def test_abort_during_arming_writes_no_run_record():
    captured: dict = {}
    cfg = {**BASE_CFG, "arming": {"default_mode": "timed", "default_wait_s": 30.0}}
    ctl = ReactorController(cfg, backend="mock",
                            feedback_cb=lambda rid, p: captured.update({rid: p}))
    try:
        ctl.submit({**RECIPE, "recipe_id": "never_ran"})
        ctl.start()
        time.sleep(0.4)
        assert ctl.state == "arming"
        ctl.abort()
        time.sleep(0.4)
        assert "never_ran" not in captured, "fabricated done-file for a run that never started"
        assert not any(h.get("recipe_id") == "never_ran" for h in ctl.history)
    finally:
        ctl.shutdown()


# ── temperature staleness ─────────────────────────────────────────────────────
# Was: a failing read left `current` frozen at the 25 °C default, so the T_max
# comparison could never be true — the thermal interlock was silently disabled.
def test_stale_temperature_is_detected():
    from src.reactor.hardware import TempController

    class DeadBeamline:
        def read_state(self): raise RuntimeError("bServer unreachable")
        def set_temperature(self, T): pass

    tc = TempController({"temperature": {"read_interval_s": 0.01}},
                        backend="real", beamline=DeadBeamline())
    assert not tc.stale                       # fresh at construction
    tc._last_read_ok = time.time() - 3600     # simulate a long dead period
    assert tc.stale, "a dead temperature source was not flagged"
    assert tc.age_s() > 1000


def test_temperature_staleness_is_reported_during_a_run():
    class DeadBeamline:
        def read_state(self): return {}
        def read_counters(self): return {}
        def set_temperature(self, T): pass
        def is_collecting(self): return False
        def collect(self, **kw): pass
        def close(self): pass
        def take_control(self): pass
        def open_shutter(self): pass
        def close_shutter(self): pass

    logs: list = []
    cfg = {**BASE_CFG, "arming": {"default_mode": "timed", "default_wait_s": 0.1}}
    ctl = ReactorController(cfg, backend="mock", log_cb=lambda m, t="info": logs.append((t, m)))
    try:
        ctl.beamline = DeadBeamline()
        ctl.temp.beamline = DeadBeamline()
        ctl.temp._last_read_ok = time.time() - 3600
        ctl.submit(RECIPE)
        ctl.start()
        time.sleep(1.0)
        assert any("STALE" in m for _, m in logs), \
            "stale temperature not reported — T_max interlock silently disabled"
    finally:
        ctl.shutdown()


# ── simulator / real-data isolation ───────────────────────────────────────────
def test_simulator_refuses_to_write_where_real_raw_files_exist():
    from src.simulator import SimulatedCollector
    with tempfile.TemporaryDirectory() as td:
        two_d = Path(td) / "2D"
        (two_d / "SAXS").mkdir(parents=True)
        (two_d / "SAXS" / "out_RealSample_scan1_0000.raw").write_bytes(b"\x00" * 16)
        sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [32, 32]})
        sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "r1"})
        with pytest.raises(RuntimeError, match="refusing to write synthetic frames"):
            sim.collect(prefix="r1_sample", role="sample", data_dir=str(two_d),
                        exposure=0.1, frames=1)


def test_simulator_marks_the_folders_it_writes():
    from src.simulator import SimulatedCollector, MARKER
    with tempfile.TemporaryDirectory() as td:
        sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [32, 32]})
        sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "r1"})
        rec = sim.collect(prefix="r1_sample", role="sample", data_dir=str(Path(td) / "p"),
                          exposure=0.1, frames=1)
        assert (Path(rec["detector_dir"]) / MARKER).is_file()
        assert (Path(rec["two_d_dir"]) / MARKER).is_file()
        # and a second acquisition into the marked folder is allowed
        sim.collect(prefix="r2_sample", role="sample", data_dir=str(Path(td) / "p"),
                    exposure=0.1, frames=1)


def test_simulated_csv_carries_a_simulated_flag():
    from src.simulator import write_csv_metadata, counters
    with tempfile.TemporaryDirectory() as td:
        out = write_csv_metadata(Path(td), "r1_sample", [counters(), counters()])
        head, first = out.read_text().splitlines()[:2]
        assert "simulated" in head
        assert first.strip().endswith("1"), "rows not flagged as synthetic"


def test_spec_beamline_has_no_simulator_and_inert_hooks():
    """The real beamline must never grow a simulator by inheritance."""
    from src.beamline.driver import SpecBeamline, BeamlineDriver
    assert not hasattr(SpecBeamline, "simulator")
    # the forwarding hooks exist on the base class as no-ops
    assert BeamlineDriver.set_recipe(object(), {"T_reac": 1}) is None
    assert BeamlineDriver.set_project_root(object(), "/tmp") is None


def test_mock_beamline_close_stops_the_simulator():
    from src.beamline import make_beamline
    bl = make_beamline({"spec": {"backend": "mock", "simulator": {
        "enabled": True, "speed_factor": 0, "shape": [32, 32]}}})
    assert bl.simulator is not None
    bl.close()
    assert bl.simulator._stop.is_set(), \
        "an in-flight simulated acquisition would keep writing after a backend switch"


# ── simulator is bound to the BACKEND, not to a free-standing flag ───────────
def test_simulator_exists_only_on_the_mock_backend():
    from src.beamline import make_beamline
    mock = make_beamline({"spec": {"backend": "mock"}})
    assert mock.simulator is not None, "mock should simulate by default"
    for real_value in ("real", "REAL", "Real", " real "):
        bl = make_beamline({"spec": {"backend": real_value,
                                     "simulator": {"enabled": True}}})
        assert type(bl).__name__ == "SpecBeamline"
        assert not hasattr(bl, "simulator"), \
            "no config value may put a simulator on real hardware"


def test_simulator_can_still_be_turned_off_within_mock():
    from src.beamline import make_beamline
    bl = make_beamline({"spec": {"backend": "mock", "simulator": {"enabled": False}}})
    assert bl.simulator is None


# ── empty / short frames must fail loudly, never land on disk ────────────────
def test_empty_frame_is_refused_and_leaves_no_stub(tmp_path):
    import numpy as np
    from src.simulator import write_raw
    with pytest.raises(ValueError, match="EMPTY frame"):
        write_raw(tmp_path / "a.raw", np.zeros((0, 0), dtype=np.int32))
    assert not list(tmp_path.glob("*")), "a stub file was left behind"


def test_simulator_never_writes_into_the_current_working_directory():
    """With no save folder set it used to fall back to Path('.') and scatter
    4 MB frames into whatever directory the app was started from."""
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [32, 32]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "r"})
    with pytest.raises(ValueError, match="no save folder"):
        sim.collect(prefix="p", role="sample", data_dir="", exposure=0.1, frames=1)


def test_mock_collect_duration_is_honoured_when_no_frames_are_produced():
    """collect() must still take mock_collect_s so is_collecting() is observable."""
    import threading
    from src.beamline import make_beamline
    bl = make_beamline({"spec": {"backend": "mock", "mock_collect_s": 0.4,
                                 "simulator": {"enabled": False}}})
    threading.Thread(target=lambda: bl.collect(recipe_id="r1", exposure=0.1,
                                               frames=1), daemon=True).start()
    time.sleep(0.05)
    assert bl.is_collecting() is True
    time.sleep(0.6)
    assert bl.is_collecting() is False


def test_zero_detector_shape_is_refused():
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [0, 0]})
    with pytest.raises(ValueError, match="detector shape"):
        sim.collect(prefix="p", role="sample", data_dir="/tmp/none", exposure=1, frames=1)


def test_written_frames_have_the_expected_byte_count(tmp_path):
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [32, 32]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "r"})
    rec = sim.collect(prefix="p", role="sample", data_dir=str(tmp_path),
                      exposure=0.1, frames=2)
    sizes = [Path(f).stat().st_size for f in rec["files"]]
    assert sizes == [32 * 32 * 4] * 2, f"frames are not full int32 images: {sizes}"


def test_stale_stop_event_does_not_silence_later_acquisitions(tmp_path):
    """A close()/backend switch used to leave _stop set forever, so every later
    acquisition wrote nothing."""
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [32, 32]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "r"})
    sim.stop()
    rec = sim.collect(prefix="after_stop", role="sample", data_dir=str(tmp_path),
                      exposure=0.1, frames=2)
    assert rec["n_frames"] == 2


# ── the simulator must use the REAL .poni, not a synthetic fallback ──────────
PONI = ('poni_version: 2\nDetector: Detector\n'
        'Detector_config: {"pixel1": 1.72e-4, "pixel2": 1.72e-4, "max_shape": [128, 128]}\n'
        'Distance: 2.5\nPoni1: 0.011\nPoni2: 0.011\n'
        'Rot1: 0.0\nRot2: 0.0\nRot3: 0.0\nWavelength: 1.033e-10\n')


def _project(tmp_path, *, name="atT_SAXS.poni"):
    import yaml
    proj = tmp_path / "MyProject"
    (proj / "poni").mkdir(parents=True)
    (proj / "poni" / name).write_text(PONI)
    (proj / "config.yml").write_text(yaml.safe_dump({
        "poni_directory": str(proj / "poni"),
        "poni_files": {"saxs": name},
        "detector_shapes": {"saxs": [128, 128]},
        "metadata_format": "csv"}))
    return proj


def test_simulator_uses_the_project_poni_when_project_root_is_set(tmp_path):
    """Regression: the controller passed the 2D DATA folder as the project root,
    so config.yml was never found and every frame used synthetic geometry."""
    pytest.importorskip("pyFAI")
    from src.beamline import make_beamline
    proj = _project(tmp_path)
    bl = make_beamline({"spec": {"backend": "mock", "simulator": {"speed_factor": 0}}})
    bl.set_project_root(str(proj))
    bl.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "g"})
    bl.collect(recipe_id="g", role="sample", sample="g", main_folder=str(proj),
               temperature=240.0, exposure=0.1, frames=1)
    assert "atT_SAXS.poni" in bl.simulator.last["geometry"]
    assert "SYNTHETIC" not in bl.simulator.last["geometry"]


def test_poni_is_found_by_walking_up_from_the_data_dir(tmp_path):
    """Even when only the save folder was wired, the geometry must be found."""
    pytest.importorskip("pyFAI")
    from src.simulator import SimulatedCollector
    proj = _project(tmp_path)
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "g"})
    rec = sim.collect(prefix="g", role="sample", data_dir=str(proj / "2D"),
                      exposure=0.1, frames=1)
    assert "atT_SAXS.poni" in rec["geometry"]


def test_simulator_poni_config_accepts_a_folder_and_picks_the_detector(tmp_path):
    """You can point `simulator.poni` at the poni/ folder; it must pick the SAXS
    file, not whatever sorts first (a WAXS poni has a very different distance)."""
    pytest.importorskip("pyFAI")
    from src.simulator import SimulatedCollector
    pdir = tmp_path / "poni"
    pdir.mkdir()
    (pdir / "atT_WAXS.poni").write_text(PONI.replace("Distance: 2.5", "Distance: 0.2"))
    (pdir / "atT_SAXS.poni").write_text(PONI)
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0,
                              "poni": str(pdir), "shape": [128, 128]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "g"})
    rec = sim.collect(prefix="g", role="sample", data_dir=str(tmp_path / "p"),
                      exposure=0.1, frames=1)
    assert "atT_SAXS.poni" in rec["geometry"], rec["geometry"]


def test_bad_poni_path_degrades_loudly(tmp_path):
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0,
                              "poni": str(tmp_path / "nope"), "shape": [64, 64]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "g"})
    rec = sim.collect(prefix="g", role="sample", data_dir=str(tmp_path / "p"),
                      exposure=0.1, frames=1)
    assert rec["geometry"].startswith("SYNTHETIC") and "not found" in rec["geometry"]


def test_controller_forwards_the_project_root_not_the_data_dir():
    ctl = _ctl()
    try:
        ctl.set_project_root("/some/project")
        assert ctl._project_root == "/some/project"
        assert ctl.beamline.simulator.project_root == "/some/project"
    finally:
        ctl.shutdown()


def test_synthetic_fallback_is_flagged_in_caps_with_a_reason(tmp_path):
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [32, 32]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "g"})
    rec = sim.collect(prefix="g", role="sample", data_dir=str(tmp_path / "bare"),
                      exposure=0.1, frames=1)
    assert rec["geometry"].startswith("SYNTHETIC"), rec["geometry"]


def test_frames_are_bright_enough_to_see_on_a_linear_display(tmp_path):
    """A 1 s frame peaked at ~900 counts, which renders black on any linear
    scale — the reason simulated data 'looked empty'."""
    import numpy as np
    from src.simulator import SimulatedCollector
    sim = SimulatedCollector({"enabled": True, "speed_factor": 0, "shape": [256, 256]})
    sim.set_recipe({"T_reac": 240, "x_TOP": 0.15, "recipe_id": "g"})
    rec = sim.collect(prefix="g", role="sample", data_dir=str(tmp_path / "p"),
                      exposure=1.0, frames=1)
    d = np.fromfile(rec["files"][0], dtype=np.int32)
    assert d.max() > 5000, f"peak only {d.max()} counts — will look blank"
    assert (d > 0).mean() > 0.5, "most of the detector has no signal"


def test_switch_backend_refuses_while_collecting():
    from src.beamline import make_beamline
    ctl = _ctl()
    try:
        bl = make_beamline({"spec": {"backend": "mock"}})
        bl._collecting = True
        ctl.beamline = bl
        ok, msg = ctl.switch_backend("real")
        assert not ok and "collection is in progress" in msg
    finally:
        ctl.shutdown()
