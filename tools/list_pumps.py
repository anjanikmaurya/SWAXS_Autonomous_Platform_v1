"""
tools/list_pumps.py — find and identify the Dolomite Mitos P-pumps.

Helps you map each USB serial port (COM* on Windows, /dev/ttyUSB* on Linux/mac)
to a pump role in reactor/config.yml.

  python tools/list_pumps.py           # just list every serial port
  python tools/list_pumps.py --probe   # ALSO open each port and read the pump's
                                       # installed flow sensor + firmware

IMPORTANT: --probe opens the serial ports, so the Dolomite GUI (or any other
program talking to the pumps) must be CLOSED first — a serial port can only be
open in one program at a time.

The installed sensor tells you what each pump is for:
  • LG16-1000 (30–1000 µL/min) -> a HIGH-flow pump  -> pd_top_precursor or ode_flush
  • LG16-0480 (1–50 µL/min)    -> a LOW-flow pump   -> oleylamine / top / ode_dilution
Combine that with which fluid line is plumbed to each pump to fill in config.yml.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from serial.tools import list_ports


def main() -> None:
    ap = argparse.ArgumentParser(description="List / identify Dolomite P-pumps.")
    ap.add_argument("--probe", action="store_true",
                    help="open each port and read its installed sensor "
                         "(CLOSE the Dolomite GUI first)")
    args = ap.parse_args()

    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Check the USB hub / cables and drivers.")
        return

    print(f"\nFound {len(ports)} serial port(s):\n")
    for p in ports:
        print(f"  {p.device:12}  serial={str(p.serial_number or '?'):18}  "
              f"{(p.manufacturer or '')} {(p.description or '')}".rstrip())

    if not args.probe:
        print("\nRe-run with  --probe  to read each pump's flow sensor "
              "(close the Dolomite GUI first).\n")
        return

    # Make the vendored driver importable.
    _root = next(p for p in Path(__file__).resolve().parents
                 if (p / "src" / "reactor" / "drivers").is_dir())
    sys.path.insert(0, str(_root / "src" / "reactor" / "drivers"))
    import Py_P_Pump  # noqa: E402

    print("\nProbing each port (the Dolomite GUI must be CLOSED)…\n")
    for p in ports:
        try:
            # enter_remote=False: just read status, don't grab control.
            pump = Py_P_Pump.P_pump(p.device, name=p.device, verbose=False,
                                    enter_remote=False)
            try:
                st = pump.read_status()
            finally:
                pump.close()
            print(f"  {p.device:12}  state={st['state_code']} "
                  f"flow={st['flow_rate_ulmin']:.3g} uL/min "
                  f"chamber={st['chamber_pressure']:.0f} mbar "
                  f"supply={st['supply_pressure']:.0f} mbar Ft={st['ft']}")
        except Exception as exc:
            print(f"  {p.device:12}  (not a P-pump, or port busy: {exc})")
    print()


if __name__ == "__main__":
    main()
