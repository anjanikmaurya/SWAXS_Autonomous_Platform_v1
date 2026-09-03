# Pre-beamtime readiness checklist — autonomous reactor + beamline

Verification that every decision made during development is implemented and covered.
Test suite: `python -m pytest -q` — run it and expect zero failures.

## Bench-validated on the rig (this beamtime)

- [x] Counter reads live (CTEMP / bstop / I0) via `ct 0.1` refresh — `beamline_read_test.py`
- [x] Temperature set + readback (`csettemp`) — `beamline_temp_test.py`
- [x] Shutter open/close (`sopen`/`sclose`) — `beamline_shutter_test.py`
- [x] 2D collection end-to-end (flat macro, commands mode) — `beamline_collect_test.py --fire`

## Safety (staff hand-back + no interruption)

| Decision | Implemented | Test |
|---|---|---|
| Stop / E-stop act on PUMPS ONLY; beamline untouched | `controller.estop()` sends nothing to SPEC | `test_estop_is_pumps_only_leaves_beamline_untouched` |
| Never interrupt an in-progress collection | hard RLock; `collect` holds it whole acquisition | `test_collection_blocks_commands_and_reads_skip` |
| Live reads stay responsive during collection | `read_state` non-blocking try-lock → `{}` | same test |
| Optional read-during-collect | `read_during_collect` flag | `test_read_during_collect_keeps_polling` |
| Auto-acquire SPEC remote control before commands | `SpecBeamline._ensure_control` | (real-path) |
| Release control + close shutter + idle pumps on exit | `controller.shutdown()` + `atexit` in app | code-verified |
| No config changes / motor moves / beamline file writes | only csettemp/ct/sopen/sclose/collect macro | `BEAMLINE_SAFETY_AUDIT.md` |
| Pipeline apps never touch the bServer | `make_beamline` only in reactor + tools | grep-verified |

## Autonomous run

| Decision | Implemented | Test |
|---|---|---|
| Arming: temperature / timed / ramp | `_arm_mode`; `ramp_wait_seconds(T_final, rate, 25°C)` | ramp math + `test_reactor_fires…` (timed) |
| Temperature set before pumps, gates start | arming sends `csettemp`, waits | code-verified |
| Sample shot at run_end − lead; background at flush_end − lead | run loop `_spec_fired`/`_bkg_fired` | `test_reactor_fires_sample_then_background_tagged` |
| recipe_id filename tagging (`<id>_sample` / `<id>_bkg`) | `_fire_spec_collection` | same test |
| Manual "Collect now" (idle only, refuses during run) | `collect_now()` | `test_collect_now_manual_and_guarded` |
| Mock/Real toggle covers pumps AND beamline | `switch_backend` rebuilds both + rewires temp | `test_backend_switch_covers_pumps_and_beamline` |
| SPEC can be disabled | `spec.enabled` | `test_spec_can_be_disabled` |
| Flat macro streamed as action commands (commands mode) | `macro_command_lines` + wait between lines | `test_commands_mode_streams_lines_no_file`, `test_commands_mode_splits_macro_into_spec_lines` |
| data_dir defaults from hub folder; editable in UI | `default_data_dir` + Data-collection field | code-verified |
| Live plot: temp 2 dp, bstop/I0 4 dp, 📷 markers | `index.html` | code-verified |

## Pump safety (from earlier audit)

- [x] E-stop idles every pump with per-pump guards (one failure can't block the rest)
- [x] Lost/hung pump detected during tick
- [x] Exact serial matching — refuses on missing/ambiguous port (no wrong-pump)

## Before you press "Run autonomously"

0. **The Mock/Real pill toggles PUMPS only.** The beamline (SPEC) backend is set
   separately in `reactor/config.yml → spec.backend`. For a real run set **both**
   the pill to Real **and** `spec.backend: real`. Checking one and assuming the
   other is the single most common pre-run mistake.
1. Backend toggle = **Real** (pumps + beamline).
2. `reactor/config.yml` spec: `temp_counter: CTEMP`, `read_refresh_cmd: "ct 0.1"`,
   `collect_mode: commands`, `macro_file: …flat.template.txt`, `data_dir` = the SPEC
   `/msd_data/...` folder. (`sauto off` if you don't want `ct` pulsing the shutter.)
3. One SPEC client only — no standalone test tool running alongside the app.
4. Do one **📷 Collect now** from the app to confirm the app path (not just the CLI).
5. Confirm the reduction pipeline sees the new `.raw` under `data_dir/2D/SAXS` via `X:\`.

## Open items (not code — rig/ops)

- Cool the sample manually (`csettemp <ambient>`) at the end — Stop/E-stop leave temp as-is by design.
- Confirm the optimizer campaign bounds/target match the chemistry before the loop drives conditions.

## Reactor checks — verify, do not assume

1. **`spec.simulator.enabled: false`** — *verify it.* The committed default is
   `true`, and the simulator writes synthetic frames that look entirely real. Also
   check `spec.simulator.poni`, which is committed as a machine-specific absolute
   path.
2. **Confirm the temperature reading is changing.** A frozen value means the
   over-temperature interlock is blind. With the shipped `spec.read_source: "spec"`
   the reading also stops for the whole ~100 s of every acquisition — expected, but
   you must be able to tell that from a dead sensor. `spec.read_source: "epics"`
   keeps reading during a collection.
3. **`resume_auto_run: false`** — auto-run is deliberately not resumed after a
   restart, because resuming moves pumps. After any restart, re-arm
   **▶ Run autonomously** by hand. E-stop also clears it.
4. **Pump limits.** `sensor_min` ships as `0.0` on all five pumps, which disables
   the below-range rejection. Set each pump's real sensor minimum or a sub-range
   setpoint is accepted with unknown delivered flow.
5. **Flush pump.** The shipped `flush.pump` is `ode_dilution` (a reagent pump,
   capped at 50 µL/min), not `ode_flush`. Confirm that is what you want.
6. **A calibration edited on disk needs an app restart.** Flipping the Mock↔Real
   toggle does *not* reload `reactor/config.yml`.

## Data-path checks — where a run silently goes nowhere

- **`spec.data_dir`.** If the `hub_path_map` prefix is wrong, the translation
  fails and `data_dir` stays at the hardcoded `/msd_data/.../Auto_Test` — SPEC then
  writes where nothing is reduced. Confirm the resolved path in the app, not the
  config.
- **A stale `<project_root>/config.yml`.** Reduction derives its root from
  `data_directory.parent`, so a leftover config pointing elsewhere silently
  relocates the 1D outputs away from every other app.
- **A not-yet-mounted project folder.** Background and analysis guard on
  `is_dir()` and drop `/api/set_project` if the path is not there yet. If `X:` is
  slow to mount, re-select the folder once it appears.
- **`spec.frames` vs the average app's frames/batch.** If `spec.frames` (10 shipped) is
  smaller than `frames_per_average` (30 shipped), **no average is ever written and
  the loop stops silently.** The average app now reports this at monitor start — read
  that line.

## Open items (not code — rig/ops)

- Cool the sample manually (`csettemp <ambient>`) at the end — Stop/E-stop leave temp as-is by design.
- Confirm the optimizer campaign bounds/target match the chemistry before the loop drives conditions.
