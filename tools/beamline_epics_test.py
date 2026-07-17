#!/usr/bin/env python
"""
beamline_epics_test.py — read temperature / i0 / bstop straight from EPICS.

Verifies the live monitors can be read via channel access (caget) WITHOUT SPEC —
no remote control, no `ct`, and it keeps working while SPEC is collecting. This is
the read path the reactor uses when `spec.read_source: epics`. Read-only: sends
nothing, controls nothing.

    python tools/beamline_epics_test.py                 # poll the config PVs ~1/s
    python tools/beamline_epics_test.py --count 1       # one read then exit
    python tools/beamline_epics_test.py --temp BL01-5:Aux1Temp.G \
           --i0 BL01-5:AuxInput.A --bstop BL01-5:AuxInput.B   # override PVs

PVs come from reactor/config.yml (spec.epics_pvs) unless overridden. Needs pyepics
(`pip install pyepics`) and channel access to the beamline (EPICS_CA_ADDR_LIST).
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.reactor import load_config              # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between reads")
    ap.add_argument("--count", type=int, default=0, help="number of reads (0 = forever)")
    ap.add_argument("--timeout", type=float, default=2.0, help="caget timeout (s)")
    ap.add_argument("--temp", default=None, help="override temperature PV")
    ap.add_argument("--i0", default=None, help="override i0 PV")
    ap.add_argument("--bstop", default=None, help="override bstop PV")
    args = ap.parse_args()

    pvs = dict((load_config().get("spec", {}) or {}).get("epics_pvs", {}) or {})
    if args.temp:  pvs["temperature"] = args.temp
    if args.i0:    pvs["i0"] = args.i0
    if args.bstop: pvs["bstop"] = args.bstop
    if not pvs:
        print("!! no PVs — set spec.epics_pvs in reactor/config.yml or pass --temp/--i0/--bstop")
        return 1

    try:
        from epics import caget
    except Exception as exc:
        print(f"!! pyepics not available: {exc}\n   pip install pyepics")
        return 1

    print("# PVs -> " + "  ".join(f"{k}={v}" for k, v in pvs.items()))
    print("# reading from EPICS (no SPEC). Ctrl-C to stop.")
    i = 0
    try:
        while True:
            i += 1
            vals = {}
            for key, name in pvs.items():
                try:
                    vals[key] = caget(name, timeout=args.timeout)
                except Exception as exc:
                    vals[key] = f"ERR({exc})"
            ts = time.strftime("%H:%M:%S")
            print(f"{ts}  temperature={vals.get('temperature')}  "
                  f"i0={vals.get('i0')}  bstop={vals.get('bstop')}")
            if any(v is None for v in vals.values()):
                print("   ⚠ a PV returned None — name wrong or not reachable "
                      "(check EPICS_CA_ADDR_LIST / VPN).")
            if args.count and i >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
