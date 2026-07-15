"""
tools/map_pumps.py — interactively map each COM port to a pump role.

Goes port-by-port. For each pump it flashes the front-panel control indicator
(toggles REMOTE <-> manual a few times — NO flow, NO pressure, safe), you watch
which physical pump changes, then type the role that pump is plumbed to. At the
end it prints a ready-to-paste reactor/config.yml 'pumps:' block.

Run with the Dolomite GUI CLOSED:
    python tools/map_pumps.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from serial.tools import list_ports

_root = next(p for p in Path(__file__).resolve().parents
             if (p / "src" / "reactor" / "drivers").is_dir())
sys.path.insert(0, str(_root / "src" / "reactor" / "drivers"))
import Py_P_Pump  # noqa: E402

ROLES = ["pd_top_precursor", "oleylamine", "top", "ode_dilution", "ode_flush"]

# Typical sensor limits per role (edit if your sensors differ).
SENSOR_DEFAULTS = {
    "pd_top_precursor": ("LG16-1000", 30.0, 1000.0),
    "ode_flush":        ("LG16-1000", 30.0, 1000.0),
    "oleylamine":       ("LG16-0480", 1.0, 50.0),
    "top":              ("LG16-0480", 1.0, 50.0),
    "ode_dilution":     ("LG16-0480", 1.0, 50.0),
}


def flash(pump, cycles=6, dt=0.6):
    """Toggle REMOTE control a few times so the pump's display flickers."""
    for _ in range(cycles):
        pump._cmd("A1")
        time.sleep(dt)
        pump._cmd("A0")
        time.sleep(dt)


def find_pumps():
    found = []
    for p in list_ports.comports():
        try:
            pump = Py_P_Pump.P_pump(p.device, name=p.device, verbose=False,
                                    enter_remote=False)
            pump.read_status()          # confirms it's a responding pump
            found.append((p.device, p.serial_number, pump))
        except Exception:
            pass
    return found


def main():
    print("\nLooking for pumps (GUI must be closed)…")
    pumps = find_pumps()
    if not pumps:
        print("No responding pumps found. Check the GUI is closed and cables are in.")
        return
    print(f"Found {len(pumps)} pump(s): " + ", ".join(d for d, _, _ in pumps) + "\n")

    mapping = {}   # role -> (com, serial)
    remaining = list(pumps)
    for com, serial, pump in pumps:
        while True:
            print(f"\n=== Identifying {com}  (serial {serial}) ===")
            input("  Press Enter — watch the pumps; one will flash REMOTE/PC control…")
            flash(pump)
            print(f"  Roles left: {[r for r in ROLES if r not in mapping]}")
            ans = input("  Which role is THIS pump? (type role, 'r' to flash again, 's' to skip): ").strip()
            if ans.lower() == "r":
                continue
            if ans.lower() == "s":
                break
            if ans not in ROLES:
                print(f"  '{ans}' is not one of {ROLES} — try again.")
                continue
            if ans in mapping:
                print(f"  {ans} already assigned to {mapping[ans][0]} — pick another or reassign.")
            mapping[ans] = (com, serial)
            break
        try:
            pump.set_idle(); pump.release(); pump.close()
        except Exception:
            pass

    # ── print the config block ───────────────────────────────────────────────
    print("\n\n──────── paste into reactor/config.yml ────────\n")
    print("pumps:")
    for role in ROLES:
        if role in mapping:
            com, serial = mapping[role]
            sensor, smin, smax = SENSOR_DEFAULTS[role]
            print(f"  {role}:")
            print(f"    serial:     \"{serial}\"       # matched first (portable across PCs)")
            print(f"    address:    \"{com}\"          # fallback only")
            print(f"    sensor:     \"{sensor}\"")
            print(f"    sensor_min: {smin}")
            print(f"    max_flow:   {smax}")
            print(f"    max_pressure: 10000.0")
        else:
            print(f"  # {role}: NOT MAPPED")
    print("\n(Confirm sensor_min/max match each pump's actual flow sensor.)\n")


if __name__ == "__main__":
    main()
