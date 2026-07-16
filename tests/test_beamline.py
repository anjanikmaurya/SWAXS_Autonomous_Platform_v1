"""
tests/test_beamline.py — beamline driver + the reactor's SPEC-collect trigger.

Mock-only (no SPEC server): checks the driver simulates a temperature ramp and
counters, and that the reactor fires a single 2D acquisition ~lead seconds before
the run ends, tagged with the recipe_id (the traceability handle the analyzer
matches on).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.beamline import make_beamline                      # noqa: E402
from src.reactor.config import PUMP_NAMES                   # noqa: E402
from src.reactor.controller import ReactorController        # noqa: E402


def test_mock_driver_ramps_and_reads():
    bl = make_beamline({"spec": {"backend": "mock", "mock_ramp_c_per_s": 100.0}})
    bl.take_control()
    bl.set_temperature(240)
    time.sleep(0.3)
    st = bl.read_state()
    assert st["temperature"] > 25 and st["bstop"] is not None
    bl.collect(path="/proj/2D/auto_1", exposure=0.5, frames=3)
    assert bl.collections and bl.collections[-1]["path"] == "/proj/2D/auto_1"


def test_macro_template_is_filled(tmp_path):
    macro = tmp_path / "collect.txt"
    macro.write_text('newfile "{path}"\ncsettemp {temperature}\ncollect_swaxs {recipe_id}\n')
    bl = make_beamline({"spec": {"backend": "mock", "macro_file": str(macro)}})
    bl.collect(path="/data/2D/auto_42", recipe_id="auto_42", temperature=245,
               exposure=1.0, frames=30)
    rendered = bl.collections[-1]["rendered"]
    assert 'newfile "/data/2D/auto_42"' in rendered
    assert "csettemp 245" in rendered and "collect_swaxs auto_42" in rendered


def _controller(spec):
    cfg = {"pumps": {n: {"max_flow": 1000.0} for n in PUMP_NAMES},
           "bounds": {"T_reac": [180, 300], "F_tot": [40, 120],
                      "x_each": [0, 0.3], "x_sum_max": 0.9},
           "run": {"default_duration": 2.0}, "spec": spec}
    return ReactorController(cfg, backend="mock")


def test_reactor_fires_sample_then_background_tagged():
    ctl = _controller({"backend": "mock", "spec_lead_s": 0.4, "exposure_s": 0.2,
                       "frames": 2, "data_dir": "/proj/2D", "mock_ramp_c_per_s": 300.0})
    try:
        ctl.set_run_settings({"arm_mode": "timed", "arm_wait_s": "0",
                              "run_duration": "1.2", "flush_rate": "100", "flush_duration": "1.2"})
        ctl.submit({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
        rid = ctl.queue[0][0].recipe_id
        ctl.start()
        time.sleep(4.0)                                  # run (~1.2) + flush (~1.2) + margin
        cols = ctl.beamline.collections
        roles = {c["role"]: c for c in cols}
        assert "sample" in roles and "background" in roles      # both fired
        assert roles["sample"]["path"].endswith(f"{rid}_sample")
        assert roles["background"]["path"].endswith(f"{rid}_bkg")
        assert ctl.temp.target == 240                    # temperature was commanded
    finally:
        ctl.shutdown()


def test_estop_is_pumps_only_leaves_beamline_untouched():
    from src.reactor.config import REAGENT_PUMPS, FLUSH_PUMP
    ctl = _controller({"backend": "mock", "spec_lead_s": 999, "mock_ramp_c_per_s": 500.0})
    try:
        ctl.set_run_settings({"arm_mode": "timed", "arm_wait_s": "0", "run_duration": "10"})
        ctl.submit({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
        ctl.start(); time.sleep(0.6)                       # running; temp commanded to 240
        assert ctl.temp.target == 240 and ctl.beamline._target == 240
        ctl.estop()
        assert ctl.state == "estop"
        for p in REAGENT_PUMPS + [FLUSH_PUMP]:
            assert ctl.pumps.pumps[p].target == 0          # pumps idled
        assert ctl.beamline._target == 240                 # beamline NOT commanded (no csettemp 0)
        assert ctl.temp.target == 240                      # temperature setpoint left as-is
    finally:
        ctl.shutdown()


def test_collection_blocks_commands_and_reads_skip():
    import threading
    bl = make_beamline({"spec": {"backend": "mock", "mock_collect_s": 0.4}})
    done = threading.Event()
    threading.Thread(target=lambda: (bl.collect(recipe_id="r1", path="/2D/r1_sample",
                                                 exposure=0.1, frames=1), done.set())).start()
    time.sleep(0.05)
    assert bl.is_collecting() is True
    t0 = time.time(); st = bl.read_state()                 # must NOT block on the collection
    assert time.time() - t0 < 0.2 and st == {}
    t0 = time.time(); bl.set_temperature(200)              # must WAIT for the collection to finish
    assert time.time() - t0 >= 0.25
    done.wait(2)
    assert bl.is_collecting() is False
    assert bl.collections and bl.collections[-1]["recipe_id"] == "r1"   # X-ray data kept


def test_read_during_collect_keeps_polling():
    import threading
    bl = make_beamline({"spec": {"backend": "mock", "mock_collect_s": 0.4,
                                 "read_during_collect": True}})
    threading.Thread(target=lambda: bl.collect(recipe_id="r1", path="/2D/r1_sample",
                                               exposure=0.1, frames=1)).start()
    time.sleep(0.05)
    assert bl.is_collecting() is True
    st = bl.read_state()                                # reads live DURING the collection
    assert st != {} and st.get("temperature") is not None
    time.sleep(0.5)


def test_backend_switch_keeps_beamline_wired(monkeypatch):
    from src.reactor import controller as C
    from src.reactor.hardware import PumpBank
    ctl = _controller({"backend": "mock"})
    try:
        bl = ctl.beamline
        # make a 'real' switch succeed with no hardware (build a mock bank instead)
        monkeypatch.setattr(C, "PumpBank", lambda cfg, backend="mock": PumpBank(cfg, backend="mock"))
        ok, _ = ctl.switch_backend("real")
        assert ok is True and ctl.backend == "real"
        assert ctl.temp.beamline is bl          # temperature still commands/reads via the beamline
        ctl.temp.set_temperature(210)
        assert bl._target == 210                 # proves the wiring survived the switch
    finally:
        ctl.shutdown()


def test_spec_can_be_disabled():
    ctl = _controller({"backend": "mock", "enabled": False, "spec_lead_s": 1.0,
                       "mock_ramp_c_per_s": 200.0})
    try:
        ctl.set_run_settings({"arm_mode": "timed", "arm_wait_s": "0", "run_duration": "1.5"})
        ctl.submit({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
        ctl.start()
        time.sleep(2.5)
        assert ctl.beamline.collections == []            # no acquisition when disabled
    finally:
        ctl.shutdown()
