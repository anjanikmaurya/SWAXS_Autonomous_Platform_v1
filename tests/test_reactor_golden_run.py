"""
tests/test_reactor_golden_run.py — the reactor still does exactly what it did.

Five Simulation scenarios (normal run, Stop, E-stop + Reset, rejected recipe,
manual flush) are run and their log lines (numbers masked), bus events, state
sequence, run records and final setpoints are compared with a recording made
from the code BEFORE controller.py was split into controller / sequence /
supervisor / collection (October 2026).

If you change the run behaviour ON PURPOSE (a new log line, a new step), check
the difference, then re-record:

    python tests/test_reactor_golden_run.py --record
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.reactor.controller import ReactorController  # noqa: E402

GOLDEN = ROOT / "tests" / "data" / "reactor_golden_run.json"
R = {"T_reac": 240, "F_tot": 80, "x_ODE": 0.3, "x_TOP": 0.15, "x_oley": 0.1, "recipe_id": "g1"}
N = lambda s: re.sub(r"\d+(\.\d+)?", "#", str(s))


def _cfg(data_dir):
    c = yaml.safe_load((ROOT / "reactor" / "config.yml").read_text())
    c["spec"]["data_dir"] = str(data_dir)
    c["spec"]["simulator"].update(enabled=False)
    c["spec"].update(exposure_s=0.1, frames=1, spec_lead_s=0.3, enabled=False)
    c["flush"]["duration"] = 0.6
    c["flush"]["blank_rinse_s"] = 0.3
    c["arming"] = {**c["arming"], "default_mode": "timed", "default_wait_s": 0.2}
    return c


def _track(ctl, states, until, t=10):
    t0 = time.time()
    while time.time() - t0 < t:
        s = ctl.state
        if not states or states[-1] != s:
            states.append(s)
        if until(s):
            return
        time.sleep(0.02)


def _normal(ctl, st):
    ctl.submit({**R, "run_duration": 0.8}); ctl.start()
    _track(ctl, st, lambda s: len(ctl.history) >= 1 and s in ("ready", "idle"))


def _abort(ctl, st):
    ctl.submit({**R, "run_duration": 30}); ctl.start()
    _track(ctl, st, lambda s: s == "running"); ctl.abort()
    _track(ctl, st, lambda s: s in ("ready", "idle"))


def _estop(ctl, st):
    ctl.submit({**R, "run_duration": 30}); ctl.start()
    _track(ctl, st, lambda s: s == "running"); ctl.estop()
    _track(ctl, st, lambda s: s == "estop"); ctl.reset(); _track(ctl, st, lambda s: s == "idle", 2)


def _bad(ctl, st):
    try:
        ctl.submit({**R, "T_reac": 999})
    except Exception as e:
        st.append("rejected:" + type(e).__name__)


def _flush(ctl, st):
    ctl.flush_now(); _track(ctl, st, lambda s: s in ("ready", "idle") and len(st) > 1)


SCENARIOS = [("normal", _normal), ("abort", _abort), ("estop", _estop),
             ("bad_recipe", _bad), ("manual_flush", _flush)]


def record() -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as d:
        for name, script in SCENARIOS:
            logs, events, states = [], [], []
            ctl = ReactorController(_cfg(Path(d) / name), backend="mock",
                                    log_cb=lambda m, t="info": logs.append((t, N(m))),
                                    event_cb=lambda typ, data=None: events.append(typ))
            try:
                script(ctl, states)
            finally:
                ctl.shutdown()
            out[name] = {"logs": logs, "events": events, "states": states,
                         "history": [{k: N(v) for k, v in h.items()
                                      if k in ("recipe_id", "reason", "status", "backend")}
                                     for h in ctl.history],
                         "setpoints": {k: round(v, 3) for k, v in (ctl.setpoints or {}).items()}}
    return json.loads(json.dumps(out))          # tuples → lists, like the stored file


def test_reactor_behaves_exactly_as_recorded():
    want = json.loads(GOLDEN.read_text())
    got = record()
    for name in want:
        for part in ("states", "events", "history", "setpoints", "logs"):
            assert got[name][part] == want[name][part], (
                f"scenario {name!r}, {part} differ from the golden run.\n"
                f"  expected: {want[name][part]}\n  got:      {got[name][part]}\n"
                "If this change is intended, re-record: python tests/test_reactor_golden_run.py --record")


if __name__ == "__main__" and "--record" in sys.argv:
    GOLDEN.write_text(json.dumps(record(), indent=1, sort_keys=True))
    print("re-recorded", GOLDEN)
