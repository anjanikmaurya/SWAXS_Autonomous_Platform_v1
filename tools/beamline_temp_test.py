#!/usr/bin/env python
"""
beamline_temp_test.py — temperature SET + READBACK test.

Sends ONE temperature setpoint to SPEC (csettemp, via the driver) after asking
you to confirm, then monitors the readback until it reaches the target or a
timeout. Touches ONLY the temperature — no pumps, no shutter, no detector.

    uv run tools/beamline_temp_test.py 60            # ramp to 60 C, monitor
    uv run tools/beamline_temp_test.py 60 --mock     # dry-run (simulator)
    uv run tools/beamline_temp_test.py --read-only   # just read current temp
    uv run tools/beamline_temp_test.py 60 --yes      # skip the confirm prompt

Counter names / bServer URL / the csettemp command come from reactor/config.yml
(spec:). Start with beamline_read_test.py to confirm reads work first.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.reactor import load_config              # noqa: E402
from src.beamline import make_beamline           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", nargs="?", type=float, help="target temperature (°C)")
    ap.add_argument("--mock", action="store_true", help="use the simulator, not real SPEC")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between readbacks")
    ap.add_argument("--duration", type=float, default=180.0, help="max seconds to monitor")
    ap.add_argument("--tol", type=float, default=2.0, help="±°C considered 'reached'")
    ap.add_argument("--read-only", action="store_true", help="only read, never set")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    cfg = load_config()
    spec = cfg.setdefault("spec", {})
    spec["backend"] = "mock" if args.mock else "real"
    bl = make_beamline(cfg)

    def read_temp():
        try:
            return bl.read_temperature()
        except Exception as exc:
            print(f"!! temperature read failed: {exc}")
            return None

    print(f"# backend={spec['backend']}  set_cmd='{spec.get('set_temp_cmd','csettemp {T}')}'  "
          f"temp_counter='{spec.get('temp_counter','temp')}'")
    print(f"# current temperature = {read_temp()}")

    if args.read_only or args.target is None:
        print("read-only (no target given).")
        return 0

    if not args.yes:
        ans = input(f"Send SPEC command to ramp to {args.target:g} °C? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted — nothing sent.")
            return 0

    bl.set_temperature(args.target)
    print(f"# sent setpoint {args.target:g} °C — monitoring readback (Ctrl-C to stop)…")
    t0 = time.time()
    try:
        while time.time() - t0 < args.duration:
            cur = read_temp()
            reached = cur is not None and abs(cur - args.target) <= args.tol
            print(f"{time.strftime('%H:%M:%S')}  T={cur}  target={args.target:g}"
                  f"{'   ✓ reached' if reached else ''}")
            if reached:
                break
            time.sleep(args.interval)
        else:
            print(f"# timed out after {args.duration:g}s without reaching target.")
    except KeyboardInterrupt:
        print("\nstopped monitoring (setpoint left as-is).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
