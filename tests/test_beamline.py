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
    macro.write_text('newfile "{{path}}"\ncsettemp {{temperature}}\ncollect_swaxs {{recipe_id}}\n')
    bl = make_beamline({"spec": {"backend": "mock", "macro_file": str(macro)}})
    bl.collect(path="/data/2D/auto_42", recipe_id="auto_42", temperature=245,
               exposure=1.0, frames=30)
    rendered = bl.collections[-1]["rendered"]
    assert 'newfile "/data/2D/auto_42"' in rendered
    assert "csettemp 245" in rendered and "collect_swaxs auto_42" in rendered


def test_singlesnapshot_template_renders_and_preserves_spec_syntax():
    from src.beamline.driver import render_macro
    tmpl = (Path(__file__).resolve().parent.parent
            / "reactor" / "macros" / "Singlesnapshot.template.txt").read_text()
    out = render_macro(tmpl, {"sample": "auto_9_sample", "frames": 2, "exposure": 30,
                              "main_folder": "/msd/AutoSynth/run"})
    assert 'sample = "auto_9_sample"' in out
    assert "n_images = 2" in out and "exposure_time = 30" in out
    assert 'main_folder = "/msd/AutoSynth/run"' in out
    assert "%s/2D/SAXS" in out           # SPEC sprintf syntax passed through untouched
    assert "{{" not in out               # every marker filled


def test_commands_mode_splits_macro_into_spec_lines():
    from src.beamline.driver import macro_command_lines, render_macro
    macro = (
        '# header comment\n'
        '\n'
        'sample = "{{sample}}"   # inline comment stripped\n'
        'n_images = {{frames}}\n'
        'eval(sprintf("mkdir %s", "#notacomment_in_quotes"))\n'
        'loopscan n_images exposure_time\n'
    )
    lines = macro_command_lines(render_macro(macro, {"sample": "auto_3_sample", "frames": 5}))
    assert lines == [
        'sample = "auto_3_sample"',
        'n_images = 5',
        'eval(sprintf("mkdir %s", "#notacomment_in_quotes"))',
        'loopscan n_images exposure_time',
    ]


def test_commands_mode_streams_lines_no_file(tmp_path, monkeypatch):
    # A SpecBeamline that records _cmd calls instead of hitting HTTP, to prove
    # "commands" mode sends the macro lines and writes no file.
    from src.beamline.driver import SpecBeamline
    macro = tmp_path / "m.txt"
    macro.write_text('newfile "{{path}}"\nloopscan {{frames}} {{exposure}}\n')
    bl = object.__new__(SpecBeamline)
    BeamlineDriver = SpecBeamline.__mro__[1]
    BeamlineDriver.__init__(bl, {"macro_file": str(macro), "collect_mode": "commands"})
    sent = []
    bl._do_take_control = lambda: None
    bl._cmd = lambda c: sent.append(c)
    bl._wait = lambda *a, **k: None
    bl.collect(path="/data/2D/auto_7_sample", frames=3, exposure=30)
    assert sent == ['newfile "/data/2D/auto_7_sample"', "loopscan 3 30"]
    assert not (macro.parent / "_autopilot_run.mac").exists()   # nothing written


def test_epics_read_source_bypasses_spec_lock(monkeypatch):
    # EPICS reads must be independent of SPEC: they should return values even while
    # the SPEC lock is held (i.e. during a collection), with no remote control / ct.
    import types
    fake = types.ModuleType("epics")
    _vals = {"BL:T": 250.0, "BL:I0": 1.23, "BL:BS": 4.56}
    fake.caget = lambda name, timeout=None: _vals[name]
    monkeypatch.setitem(sys.modules, "epics", fake)

    from src.beamline.driver import SpecBeamline
    bl = object.__new__(SpecBeamline)
    BeamlineDriver = SpecBeamline.__mro__[1]
    BeamlineDriver.__init__(bl, {"read_source": "epics",
                                 "epics_pvs": {"temperature": "BL:T", "i0": "BL:I0", "bstop": "BL:BS"}})
    bl._to = 2.0
    bl._caget = None

    bl._lock.acquire()                      # simulate SPEC busy (a collection holds it)
    try:
        st = bl.read_state()                # must NOT block and must return live values
    finally:
        bl._lock.release()
    assert st == {"temperature": 250.0, "i0": 1.23, "bstop": 4.56}


def test_hub_to_spec_dir_translation():
    from src.reactor.config import hub_to_spec_dir
    m = {"from": "X:\\bl1-5", "to": "/msd_data/checkout/bl1-5"}
    assert hub_to_spec_dir(r"X:\bl1-5\staff_data\Partha\AutoSynth\Auto_Test", m) \
        == "/msd_data/checkout/bl1-5/staff_data/Partha/AutoSynth/Auto_Test"
    assert hub_to_spec_dir("x:/BL1-5/foo/bar", m) == "/msd_data/checkout/bl1-5/foo/bar"  # case-insensitive
    assert hub_to_spec_dir(r"D:\other\exp", m) is None       # prefix mismatch → don't map
    assert hub_to_spec_dir("", m) is None
    assert hub_to_spec_dir(r"X:\bl1-5\exp", None) is None     # no mapping


def test_set_data_dir_forces_and_default_only_if_blank():
    ctl = _controller({"backend": "mock", "data_dir": "/orig"})
    try:
        assert ctl.status()["spec"]["data_dir"] == "/orig"
        ctl.default_data_dir("/should_be_ignored")           # only-if-blank: ignored
        assert ctl.status()["spec"]["data_dir"] == "/orig"
        ctl.set_data_dir("/new/from/hub")                    # force: follows hub
        assert ctl.status()["spec"]["data_dir"] == "/new/from/hub"
    finally:
        ctl.shutdown()


def test_flush_pump_switch_to_reagent_and_clamp():
    from src.reactor.config import PUMP_NAMES, REAGENT_PUMPS, FLUSH_PUMP
    cfg = {"pumps": {n: {"max_flow": (50.0 if n == "ode_dilution" else 1000.0)} for n in PUMP_NAMES},
           "bounds": {"T_reac": [180, 300], "F_tot": [40, 120], "x_each": [0, 0.3], "x_sum_max": 0.9},
           "run": {"default_duration": 2.0}, "flush": {"rate": 100.0, "duration": 1.0},
           "spec": {"backend": "mock", "enabled": False}}
    ctl = ReactorController(cfg, backend="mock")
    try:
        # default: dedicated ode_flush at full rate
        ctl._enter_flush(kind="flush")
        assert ctl.pumps.pumps[FLUSH_PUMP].target == 100.0
        assert ctl.pumps.pumps["ode_dilution"].target == 0.0

        # switch to the reagent pump: rate clamps to its 50 µL/min max, ode_flush idled
        ctl.set_run_settings({"flush_pump": "ode_dilution"})
        ctl._enter_flush(kind="flush")
        assert ctl.pumps.pumps["ode_dilution"].target == 50.0     # clamped from 100
        assert ctl.pumps.pumps[FLUSH_PUMP].target == 0.0
        for p in ("pd_top_precursor", "oleylamine", "top"):
            assert ctl.pumps.pumps[p].target == 0.0
    finally:
        ctl.shutdown()


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


def test_backend_switch_covers_pumps_and_beamline(monkeypatch):
    from src.reactor import controller as C
    from src.reactor.hardware import PumpBank
    from src.beamline.driver import MockBeamline, SpecBeamline
    ctl = _controller({"backend": "mock"})
    try:
        assert isinstance(ctl.beamline, MockBeamline)
        # make a 'real' switch succeed with no hardware (build a mock pump bank)
        monkeypatch.setattr(C, "PumpBank", lambda cfg, backend="mock": PumpBank(cfg, backend="mock"))
        ok, _ = ctl.switch_backend("real")
        assert ok and ctl.backend == "real"
        assert isinstance(ctl.beamline, SpecBeamline)      # beamline switched to real too
        assert ctl.temp.beamline is ctl.beamline           # temperature re-wired to it
        ok, _ = ctl.switch_backend("mock")
        assert ok and isinstance(ctl.beamline, MockBeamline)
        assert ctl.temp.beamline is ctl.beamline
        ctl.temp.set_temperature(210)
        assert ctl.beamline._target == 210                 # wiring intact after switching back
    finally:
        ctl.shutdown()


def test_collect_now_manual_and_guarded():
    ctl = _controller({"backend": "mock", "frames": 2, "exposure_s": 0.1,
                       "data_dir": "/proj/2D", "mock_ramp_c_per_s": 300.0})
    try:
        ok, rid = ctl.collect_now("sample")               # idle → allowed
        assert ok and rid.startswith("manual_")
        time.sleep(0.3)
        c = ctl.beamline.collections[-1]
        assert c["role"] == "sample" and rid in c["path"]
        # during a run it must refuse (the loop owns collection then)
        ctl.set_run_settings({"arm_mode": "timed", "arm_wait_s": "0", "run_duration": "10"})
        ctl.submit({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1})
        ctl.start(); time.sleep(0.4)
        ok2, msg = ctl.collect_now()
        assert ok2 is False and "run" in msg.lower()
    finally:
        ctl.shutdown()


def test_app_run_settings_win_over_recipe_and_persist():
    ctl = _controller({"backend": "mock", "enabled": False})
    try:
        ctl.set_run_settings({"run_duration": "3"})          # APP value
        ctl.submit({"T_reac": 240, "F_tot": 80, "x_ODE": 0.2, "x_TOP": 0.1, "x_oley": 0.1,
                    "run_duration": 99})                     # recipe-embedded value
        ctl._begin_next(); ctl._enter_running()
        assert abs((ctl._run_deadline - ctl._run_started) - 3.0) < 0.2   # app wins, not 99
        # a blank push must NOT clear a set value (sticky until app restart)
        ctl.set_run_settings({"run_duration": ""})
        assert ctl.live_duration == 3.0
        # survives estop + reset (only a process restart clears it)
        ctl.estop(); assert ctl.live_duration == 3.0
        ctl.reset(); assert ctl.live_duration == 3.0
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
