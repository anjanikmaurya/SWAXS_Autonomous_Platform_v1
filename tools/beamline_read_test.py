#!/usr/bin/env python
"""
beamline_read_test.py — READ-ONLY beamline counter test (the live-plot data).

Prints temperature / bstop / I0 (and optionally every counter) polled from SPEC
via the bServer, using the SAME driver the reactor uses. Sends NO control
commands — completely safe to run anytime, including on the live beamline.

Run the real thing (default):
    uv run tools/beamline_read_test.py                 # poll ~1/s, Ctrl-C to stop
    uv run tools/beamline_read_test.py --all           # dump every counter each poll
    uv run tools/beamline_read_test.py --count 1       # single read then exit
Dry-run with the simulator (no hardware):
    uv run tools/beamline_read_test.py --mock

Counter names / bServer URL come from reactor/config.yml (spec:). Override the
temperature counter on the fly with --temp-counter if you're still discovering it.
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
    ap.add_argument("--mock", action="store_true", help="use the simulator, not real SPEC")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between reads")
    ap.add_argument("--count", type=int, default=0, help="number of reads (0 = forever)")
    ap.add_argument("--all", action="store_true", help="print every counter each poll")
    ap.add_argument("--temp-counter", default=None, help="override the temperature counter name")
    ap.add_argument("--refresh", default=None,
                    help='SPEC cmd to refresh counters before each read, e.g. "ct 0.1" '
                         '(get_all_counters is otherwise the LAST count — stale). '
                         '⚠ "ct" may open the shutter.')
    args = ap.parse_args()

    cfg = load_config()
    spec = cfg.setdefault("spec", {})
    spec["backend"] = "mock" if args.mock else "real"
    if args.temp_counter:
        spec["temp_counter"] = args.temp_counter
    if args.refresh is not None:
        spec["read_refresh_cmd"] = args.refresh
    bl = make_beamline(cfg)

    # Refreshing means sending a SPEC command (ct), which the bServer only runs
    # while we hold remote control — grab it up front so the refresh isn't ignored.
    if spec.get("read_refresh_cmd") and not args.mock:
        try:
            bl.take_control()
            print(f"# took remote control to send refresh {spec['read_refresh_cmd']!r} "
                  f"(if the SPEC GUI has control, this may fail or take it over)")
        except Exception as exc:
            print(f"!! could not take remote control: {exc} — refresh will be ignored")

    tc = spec.get("temp_counter", "temp")
    print(f"# backend={spec['backend']}  base_url={spec.get('base_url')}")
    print(f"# counters -> temperature='{tc}'  bstop='{spec.get('bstop_counter','bstop')}'  "
          f"i0='{spec.get('i0_counter','i0')}'")
    rc = spec.get("read_refresh_cmd")
    print(f"# refresh before read = {rc!r}" if rc
          else "# refresh before read = (none — showing LAST-count values; use --refresh to make live)")

    # one-time discovery dump so you can SEE the real counter names
    try:
        allc = bl.read_counters()
        print("# available counters:", ", ".join(sorted(allc)) if allc else "(none returned)")
        if tc not in allc and not args.mock:
            print(f"#   ⚠ '{tc}' not found — set spec.temp_counter (or use --temp-counter) "
                  f"to one of the names above.")
    except Exception as exc:
        print(f"!! could not reach the bServer / read counters: {exc}")
        print("   Check the bServer is running and spec.base_url is correct.")
        return 1

    i = 0
    try:
        while True:
            i += 1
            ts = time.strftime("%H:%M:%S")
            if args.all:
                print(ts, bl.read_counters())
            else:
                st = bl.read_state()
                print(f"{ts}  temperature={st.get('temperature')}  "
                      f"bstop={st.get('bstop')}  i0={st.get('i0')}")
            if args.count and i >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
