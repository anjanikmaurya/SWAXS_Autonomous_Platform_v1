#!/usr/bin/env python
"""
beamline_shutter_test.py — the simplest possible beamline action test.

Opens the fast shutter, holds it briefly, then closes it — using the SAME driver
the reactor uses. Touches ONLY the shutter (sopen/sclose): no temperature, no
pumps, no detector, no data. Confirmation-gated because opening the shutter lets
X-rays onto the sample.

    python tools/beamline_shutter_test.py            # asks y/N, opens, holds 2s, closes
    python tools/beamline_shutter_test.py --hold 5   # hold open 5 s
    python tools/beamline_shutter_test.py --close-only   # just make sure it's CLOSED (safe)
    python tools/beamline_shutter_test.py --mock     # dry-run against the simulator
    python tools/beamline_shutter_test.py --yes      # skip the confirm prompt

Commands / bServer URL come from reactor/config.yml (spec: open_shutter_cmd,
close_shutter_cmd, base_url). Run beamline_read_test.py first to confirm the link.
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
    ap.add_argument("--hold", type=float, default=2.0, help="seconds to hold the shutter open")
    ap.add_argument("--close-only", action="store_true", help="only CLOSE the shutter (no opening)")
    ap.add_argument("--mock", action="store_true", help="use the simulator, not real SPEC")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    cfg = load_config()
    spec = cfg.setdefault("spec", {})
    spec["backend"] = "mock" if args.mock else "real"
    bl = make_beamline(cfg)

    print(f"# backend={spec['backend']}  open='{spec.get('open_shutter_cmd','sopen')}'  "
          f"close='{spec.get('close_shutter_cmd','sclose')}'")

    # take control up front so the commands actually land
    if not args.mock:
        try:
            bl.take_control()
            print("# took remote control (if the SPEC GUI holds it, this may fail / take over)")
        except Exception as exc:
            print(f"!! could not take remote control: {exc}")
            return 1

    if args.close_only:
        if not args.yes:
            ans = input("Close the shutter now? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("aborted."); return 0
        bl.close_shutter()
        print("# shutter CLOSED.")
        return 0

    if not args.yes:
        warn = "" if args.mock else "  ⚠ THIS OPENS THE SHUTTER — X-rays onto the sample."
        ans = input(f"Open shutter for {args.hold:g}s, then close?{warn} [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("aborted — shutter untouched."); return 0

    try:
        bl.open_shutter()
        print(f"# shutter OPEN — holding {args.hold:g}s…")
        time.sleep(max(0.0, args.hold))
    finally:
        bl.close_shutter()                 # always close, even on Ctrl-C / error
        print("# shutter CLOSED.")
    if args.mock:
        print(f"# (mock) final shutter state: {getattr(bl, 'shutter', '?')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
