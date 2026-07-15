"""
tools/pump_flow_test.py — command ONE pump to a flow and watch it respond.

Uses the app's own driver (enters REMOTE control, sends an F setpoint, polls
status, then idles). This checks whether the pump actually enters CONTROLLING,
whether the setpoint (Qt) and measured flow (Qc) track, and whether chamber
pressure rises.

⚠ THIS COMMANDS REAL FLOW. Only run with the compressed-air supply connected
and the pump outlet routed safely (into waste/beaker). Close the Dolomite GUI.

Usage:
    python tools/pump_flow_test.py COM5           # default 50 uL/min
    python tools/pump_flow_test.py COM6 10        # 10 uL/min on a low-flow pump
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_root = next(p for p in Path(__file__).resolve().parents
             if (p / "src" / "reactor" / "drivers").is_dir())
sys.path.insert(0, str(_root / "src" / "reactor" / "drivers"))
import Py_P_Pump  # noqa: E402


def show(tag, st):
    print(f"  [{tag:6}] state={st['state_code']} mode={st['mode']} "
          f"Qt={Py_P_Pump.pls_to_ulmin(st['target_flow_rate_pls']):.2f} "
          f"Qc={st['flow_rate_ulmin']:.2f} uL/min  "
          f"chamber={st['chamber_pressure']:.0f} supply={st['supply_pressure']:.0f} mbar")


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/pump_flow_test.py COM5 [uL_min]")
        return
    port = sys.argv[1]
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0

    print(f"\nOpening {port} (enters REMOTE control)…")
    p = Py_P_Pump.P_pump(port, name=port, verbose=False)   # __init__ sends A1 + C
    try:
        print(f"remote entered: {p.remote}")
        show("before", p.read_status())
        print(f"\nCommanding F = {rate} uL/min  (= {Py_P_Pump.ulmin_to_pls(rate)} pl/s)…")
        p.set_flowrate_ulmin(rate)
        for i in range(6):
            time.sleep(2.0)
            show(f"t+{2*(i+1)}s", p.read_status())
        print("\nSetting idle (P0)…")
        p.set_idle()
        time.sleep(1.0)
        show("idle", p.read_status())
    finally:
        try:
            p.set_idle(); p.release()
        except Exception:
            pass
        p.close()
        print(f"\nClosed {port}.\n")
    print("Read the rows: state should reach 1 (CONTROLLING); Qt should equal your\n"
          "setpoint; Qc and chamber pressure should rise if air is connected. If Qt\n"
          "is 0 or state stays 0, the F command isn't engaging; if Qt is right but\n"
          "Qc/chamber stay ~0, check the air supply.\n")


if __name__ == "__main__":
    main()
