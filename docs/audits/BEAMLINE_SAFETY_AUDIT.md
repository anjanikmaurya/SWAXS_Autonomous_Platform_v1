# Beamline safety audit — what the platform does (and does NOT) to SPEC/bServer

Goal: confirm the reactor app and the data pipeline only **send commands and read**
from the beamline, and never permanently reconfigure SPEC or the bServer — so
beamline staff have nothing to undo after this beamtime.

## Summary

**Safe to run.** Every SPEC interaction is the same set of commands the group's own
`MSD.py` / `Singlesnapshot.txt` already use. The platform sends **no** configuration
changes, motor moves, limit changes, or file writes to beamline-owned locations.
One gap was found and fixed: the app now **releases SPEC remote control on exit**.

## Every SPEC/bServer call the platform makes

All beamline I/O lives in exactly one file: `src/beamline/driver.py` (class
`SpecBeamline`). Nothing else in the repo talks to the bServer.

Read-only (no side effects):
- `get_all_counter_mnemonics`, `get_all_counters` — read counters (temp/bstop/I0)
- `is_busy` — poll whether SPEC is busy
- `are_we_in_control` (indirect) / `get_remote_control` / `release_remote_control`

Commands (`execute_command`), all identical to the group's existing scripts:
- `csettemp <T>` — set temperature (same as `MSD.set_temperature`)
- `ct 0.1` — short count to refresh counters (same as `MSD.execute_and_read_count`)
- `sopen` / `sclose` — open/close shutter (same as `MSD.open_shutter`/`close_shutter`)
- the collection macro (your `Singlesnapshot.txt`): `mkdir -p`, `cd`, `pd savepath`,
  `newfile`, `pd save`, `sopen`, `loopscan`, `sclose`, `pd nosave`

## What the platform does NOT do

- **No configuration changes** — never `config save`, `savstate`, `reconfig`,
  `set_lm` (limits), `chg_dial`, or `caput`. Nothing edits SPEC config files.
- **No motor moves** — never `mv`/`mvr`/`umv`. (The group's `MSD.py` can move motors;
  the reactor driver deliberately does not.)
- **No writes to beamline/bServer files** — the default collect mode is `commands`,
  which streams the macro's lines over the bServer and writes **no file**. SPEC
  itself writes the `.raw` frames into your own `data_dir` (AutoSynth folder). The
  alternative `qdo` mode (off by default) would write one filled macro file, and
  only into a path you configure.
- **Pipeline apps never touch the beamline** — reduction / viewer / background /
  analysis / quality / assistant / hub only read & write files inside the selected
  project folder. `make_beamline` is imported only by the reactor and the test tools.

## Transient SPEC session state the platform touches (resets on SPEC restart)

These are normal beamline-operation states, not persistent config — and they are
exactly what your own single-shot macro already sets:
- current directory (`cd`), detector save path (`pd savepath`), file prefix
  (`newfile`), and the SPEC globals `sample` / `n_images` / `exposure_time` /
  `main_folder`. `pd save` is turned back off (`pd nosave`) at the end of each shot.
- Temperature setpoint (`csettemp`) — left as-is by Stop/E-stop by design. If you
  ramped the sample, cool it down manually (`csettemp <ambient>`) before leaving.
- Shutter — closed by the macro after every shot, and closed again on app exit.

## Gap found and fixed

`ReactorController.shutdown()` previously only stopped the control loop and never
released remote control — so quitting the app could leave SPEC "held" by the
reactor. Fixed: `shutdown()` now idles the pumps, closes the shutter (unless a
collection is mid-flight), and calls `release_remote_control`; the reactor app
registers this via `atexit` so it runs on exit/Ctrl-C.

## Operator notes

- **While the reactor app is open in Real mode it holds SPEC remote control**
  (needed for the `ct` live-plot refresh). To hand control back to staff, either
  **close the app** or flip its backend toggle to **Mock** (both release control).
- Run **one SPEC client at a time**: don't run the standalone test tools and the
  reactor app against Real simultaneously.
- If you don't want `ct` opening the shutter during a ramp, set `sauto off` first;
  collection still works because the macro opens/closes the shutter itself.
