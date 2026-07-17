# Pre-beamtime readiness checklist — autonomous reactor + beamline

Verification that every decision made during development is implemented and covered.
Test suite: **118 passed** (`python -m pytest -q`).

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
