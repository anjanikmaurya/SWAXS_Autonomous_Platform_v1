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
