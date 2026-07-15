"""
tools/tare_probe.py — discover the flow-sensor-tare command for one pump.

paws only ever used 'R0' (the pump *pressure* tare). The Dolomite GUI also has
a separate "Tare Flow Sensor", but its serial command isn't documented. This
probe tries a few candidate tare commands and reports which ones make the pump
enter TARE state (state 2) and return to IDLE — that identifies a valid tare.

Tare is a sensor-zeroing operation (no flow, no pressurisation), so it's safe
to run with the AIR SUPPLY DISCONNECTED and NO FLOW through the sensor — which
are exactly the conditions a tare needs anyway. Close the Dolomite GUI first.

Usage:
    python tools/tare_probe.py COM5
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_root = next(p for p in Path(__file__).resolve().parents
             if (p / "src" / "reactor" / "drivers").is_dir())
sys.path.insert(0, str(_root / "src" / "reactor" / "drivers"))
import Py_P_Pump  # noqa: E402

CANDIDATES = ["R0", "R1", "R2"]   # R0 = known pressure tare; R1/R2 = guesses


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/tare_probe.py COM5")
        return
    port = sys.argv[1]
    print(f"\nOpening {port} (air should be DISCONNECTED, no flow)…\n")
    p = Py_P_Pump.P_pump(port, name=port, verbose=False)
    try:
        for cmd in CANDIDATES:
            st0 = p.read_status()
            print(f"→ sending {cmd!r}  (state before={st0['state_code']}, "
                  f"Qc={st0['flow_rate_ulmin']:.3f} uL/min)")
            resp = p._cmd(cmd)
            print(f"   reply: {resp!r}")
            entered_tare = False
            for _ in range(12):          # watch up to ~6 s
                time.sleep(0.5)
                st = p.read_status()
                if st["state_code"] == 2:
                    entered_tare = True
            st1 = p.read_status()
            verdict = ("TARE triggered (entered state 2)" if entered_tare
                       else "no tare (state never became 2)")
            print(f"   result: {verdict}; state now={st1['state_code']}, "
                  f"Qc={st1['flow_rate_ulmin']:.3f} uL/min\n")
            time.sleep(1.0)
    finally:
        try:
            p.set_idle(); p.release()
        except Exception:
            pass
        p.close()
    print("Report which command(s) said 'TARE triggered'. R0 is the pressure "
          "tare; whichever OTHER command triggers a tare is the flow-sensor "
          "tare — I'll wire it to a button.\n")


if __name__ == "__main__":
    main()
