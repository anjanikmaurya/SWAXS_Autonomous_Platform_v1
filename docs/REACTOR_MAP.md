# Reactor app — code map & troubleshooting guide

Where to look when something misbehaves. The reactor app is a thin Flask shell
(`reactor/app.py` + `templates/index.html`); all logic lives in `src/reactor/` and
`src/beamline/`. Function/method names are used instead of line numbers (stable
across edits) — search the file for the name.

---

## Symptom → where to look

| Symptom | File · function |
|---|---|
| Live plot frozen / stale temp·bstop·I0 | `src/beamline/driver.py` · `SpecBeamline._do_read_counters` (needs `read_refresh_cmd`, remote control) |
| Temperature wrong counter / value | `config.yml spec.temp_counter`; driver `_do_read_state` |
| `csettemp` / any command does nothing (silently) | `SpecBeamline._ensure_control` (must hold remote control) |
| Collection 500 error on a line | `driver._do_collect` (commands mode); use the FLAT macro (no `var=` lines) |
| Collection writes to wrong folder | `config.yml spec.data_dir`; `controller._fire_spec_collection` (builds path) |
| Files not named `<id>_sample`/`<id>_bkg` | `controller._fire_spec_collection`; `config.yml spec.sample_tag/bkg_tag` |
| Sample/background shot fires at wrong time | `controller._loop` (uses `spec_lead`, `_spec_fired`/`_bkg_fired`) |
| Pumps won't start / arming stuck | `controller._begin_next`, `_loop` (arming block); `config.yml arming` |
| E-stop / Stop behaviour | `controller.estop` (pumps only), `abort` |
| Reactor won't cool after run | `controller._end_run` (cooldown); `config.yml temperature.cooldown_c` |
| Mock/Real toggle issues | `controller.switch_backend`; `app.py /api/backend` |
| SPEC still "held" after quitting | `controller.shutdown` + `atexit` in `app.py` |
| Pump on wrong COM / not found | `src/reactor/hardware.py RealPump`, `PumpBank`; serial matching |
| UI field not applying | `templates/index.html` `pushSpec()` / `pushRun()` → matching `/api/*` route |

---

## reactor/app.py — HTTP layer (nothing scientific here)

- `_CFG = load_config()` — reads `reactor/config.yml`.
- `_ctrl = ReactorController(...)` — the one controller instance (created at import).
- `atexit.register(_ctrl.shutdown)` — releases SPEC control on exit.
- Routes (all thin wrappers around controller methods):
  - `/api/recipe` → `submit`; `/api/start` → `start`; `/api/start_now` → `start_now`
  - `/api/abort` → `abort`; `/api/estop` → `estop`; `/api/reset` → `reset`; `/api/vent` → `vent_all`
  - `/api/flush` → `flush_now`; `/api/queue/clear` → `clear_queue`; `/api/auto_run` → `set_auto_run`
  - `/api/backend` → `switch_backend`; `/api/spec_settings` → `set_spec_settings`
  - `/api/collect_now` → `collect_now`; `/api/run_settings` → `set_run_settings`
  - `/api/pumps` → pump limits; `/api/tare` → `tare_pump`; `/api/set_project` → project root (+ `default_data_dir`)
  - `/api/status`, `/api/stream` (SSE, calls `_ctrl.status()` every 0.5 s)

## reactor/templates/index.html — UI (one file: HTML + JS)

- `pushSpec()` → POSTs the Data-collection card (exposure/frames/tags/lead/**data_dir**).
- `pushRun()` / run-settings inputs → `/api/run_settings` (arm mode, durations, ramp rate).
- `collectNow()` → `/api/collect_now`. Arming UI: `armModeChange()`.
- Live plot: `BL` buffer, `drawBeamline()`, `blLegendHTML()` (temp 2 dp, bstop/I0 4 dp),
  collection 📷 markers (`MARKS`, `updateCollectBadge()`).
- Status sync: the SSE handler fills fields once (`window._specInit`) and updates readouts
  (`t-cur`, `t-bstop`, …).

## src/reactor/controller.py — the brain (state machine + run loop)

State: `idle → arming → running → flushing → ready`; `estop` from anywhere.

- **Lifecycle:** `submit` (queue a recipe) → `start`/`start_now` → `_begin_next`
  (arming: temperature/timed/ramp; `ramp_wait_seconds`) → `_enter_running` (pumps on)
  → `_end_run` (reagents off + **cooldown** `csettemp`) → `_enter_flush` → `_end_flush`.
- **Control loop:** `_loop` (background thread) — advances state, checks deadlines,
  fires the sample shot at `run_deadline − spec_lead` and the background shot at
  `flush_deadline − spec_lead`; `_safety_check` enforces `T_max` etc.
- **Collection:** `_fire_spec_collection(recipe_id, role)` — builds `<id>_<tag>` and the
  path, calls `beamline.collect(...)`. `collect_now(role)` — manual, idle-only.
- **Safety:** `estop` (pumps only, beamline untouched), `abort`, `reset`, `vent_all`.
- **Live settings:** `set_run_settings`, `set_spec_settings`, `default_data_dir`,
  `set_pump_limits`, `tare_pump`.
- **Backend:** `switch_backend` (rebuilds pumps + beamline, rewires temperature).
- **Exit:** `shutdown` (idle pumps, close shutter, release SPEC control).
- **UI feed:** `status()` — the dict the app streams (state, pumps, temperature{current,
  bstop,i0}, spec{…}, last_collect).

## src/reactor/hardware.py — pumps + temperature

- `MockPump` / `RealPump` (serial, `src/reactor/drivers/Py_P_Pump.py`) — one syringe pump.
- `PumpBank` — all pumps: `set_pump_flow`, `set_all`, `idle_all` (E-stop, per-pump
  guarded), `zero_pumps` (reagents at run end), `tare`, `tick`, `state`.
- `TempController` — `set_temperature` (commands the beamline `csettemp` when wired),
  `read`, `tick` (throttled beamline read → `current`/`bstop`/`i0`), `is_stable`.

## src/beamline/driver.py — the ONLY thing that talks to SPEC/bServer

- `make_beamline(cfg)` → `MockBeamline` or `SpecBeamline` (per `spec.backend`).
- `SpecBeamline`: `_sis` (HTTP GET), `_cmd` (execute_command, auto `_ensure_control`),
  `_do_set_temperature` (`csettemp`), `_do_read_counters` (optional `ct` refresh),
  `_do_open_shutter`/`_do_close_shutter` (`sopen`/`sclose`), `_do_collect`
  (commands mode streams flat-macro lines / qdo mode), `close` (release control).
- Hard SPEC guard: `collect` holds the lock the whole acquisition; `read_state` is
  non-blocking (skips during a collection unless `read_during_collect`).
- Helpers: `render_macro` (fills `{{markers}}`), `macro_command_lines` (splits a flat
  macro into SPEC commands).

## Config & macros

- `reactor/config.yml` — pumps, `arming`, `flush`, `safety`, `temperature`
  (`cooldown_c`, `read_interval_s`), `spec` (backend, base_url, counters,
  `read_refresh_cmd`, `set_temp_cmd`, shutter cmds, `macro_file`, `collect_mode`,
  `data_dir`, `spec_lead_s`, exposure/frames, tags).
- `reactor/macros/Singlesnapshot.flat.template.txt` — used by **commands** mode
  (plain action commands, values inlined).
- `reactor/macros/Singlesnapshot.template.txt` — used by **qdo** mode (eval-wrapped).

## Related (closed loop, not the reactor app itself)

- `src/optimizer/` — Bayesian campaign proposing next conditions (`campaign.py`).
- `src/analysis/nanoparticle.py` — size/PDI/phase/confidence from subtracted SAXS.
- `src/loop_naming.py` — the `recipe_id` filename convention shared with the pipeline.

---

## Standalone bench tools (bypass the app; one SPEC client at a time)

`tools/beamline_read_test.py` (counters), `beamline_temp_test.py` (csettemp),
`beamline_shutter_test.py` (sopen/sclose), `beamline_collect_test.py` (2D collect).
Runbook: `tools/BEAMLINE_TESTING.md`. Safety audit: `docs/audits/BEAMLINE_SAFETY_AUDIT.md`.
