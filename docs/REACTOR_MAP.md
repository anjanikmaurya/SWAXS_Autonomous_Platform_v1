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
| Live plot frozen **only during a collection** (and the temperature interlock with it) | `driver.read_state` / `read_counters` (non-blocking, skipped while `_collecting`); `config.yml spec.read_source` → `"epics"`, or `read_during_collect: true` |
| Temperature wrong counter / value | `config.yml spec.temp_counter`; driver `_do_read_state` |
| `csettemp` / any command does nothing (silently) | `SpecBeamline._ensure_control` (must hold remote control) |
| Collection 500 error on a line | `driver._do_collect` (commands mode); use the FLAT macro (no `var=` lines) |
| Collection writes to wrong folder | `config.yml spec.data_dir`; `controller._fire_spec_collection` (builds path) |
| Files not named `<id>_sample`/`<id>_bkg` | `controller._fire_spec_collection`; `config.yml spec.sample_tag/bkg_tag` |
| Sample/background shot fires at wrong time | `controller._tick_once` (uses `spec_lead`, `_spec_fired`/`_bkg_fired`); `config.yml spec.background_when`; `controller._enter_flush` (`_flush_kind`, `_bkg_recipe_id` — empty = no background collected) |
| Pumps won't start / arming stuck | `controller._begin_next` → `_start_recipe`, `_tick_once` (arming block); `config.yml arming` (`temperature`\|`timed` only) |
| E-stop / Stop behaviour | `controller.estop` (pumps only; also sets `auto_run = False`), `abort` |
| Nothing runs after an E-stop + Reset | `controller.estop` disabled auto-run — re-arm **▶ Run autonomously** |
| Reactor won't cool after run | `controller._end_run` (cooldown); `config.yml temperature.cooldown_c` |
| Mock/Real toggle issues | `controller.switch_backend`; `app.py /api/backend` |
| SPEC still "held" after quitting | `controller.shutdown` + `atexit` in `app.py` |
| Pump on wrong COM / not found | `src/reactor/hardware.py RealPump`, `PumpBank`; serial matching |
| UI field not applying | `templates/index.html` `pushSpec()` / `pushRun()` → matching `/api/*` route |

---

## Control panel — what each button does (operator reference)

```
idle ──Start──▶ arming ──(temp stable / timed wait)──▶ running ──run ends──▶ flushing ──▶ ready
  ▲                │                                                            │
  └──── Reset ◀─ estop ◀── EMERGENCY STOP (from any state) ; Vent → idle (from any state)
```

(With the shipped `spec.background_when: "before"` a flush + background shot runs
*between* Start and `arming` — see the lifecycle section below.)

Every button ultimately acts on the 5 pumps (4 reagent + 1 flush/solvent). What
distinguishes the "make it stop" controls is **not** the pump action (several call
the same `idle_all()`) but the **state they leave you in** and **what they
preserve** (current recipe, pending queue, auto-run).

| Button (label) | Endpoint | Controller method | Valid in states | Effect | Intended use |
|---|---|---|---|---|---|
| ＋ Run manually (add to queue) | `/api/recipe` | `submit()` | any (queues) | Validates + enqueues one recipe. Auto-starts **only** if Auto-run is on and system is idle/ready. | Hand-enter a single condition to run. |
| ▶ Run autonomously (toggle) | `/api/auto_run` | `set_auto_run()` | any | Flips auto-run; when on, the next queued/dropped recipe starts automatically. | Hand the reactor to the folder/ML loop. |
| Start / ⏩ Start pumps now | `/api/start` **or** `/api/start_now` | `start()` / `start_now()` | idle/ready → `start`; arming → `start_now` | Begins the next queued recipe; **during arming** the same button skips the remaining arming wait and starts the pumps now. Relabels automatically. | Begin a run; or override the arming wait. |
| ■ Stop → flush | `/api/abort` | `abort()` | arming, running, flushing | arming/running → stop reagents and go to **flush**; **flushing → idle** (stops the flush, despite the label). | Cleanly end the current run and flush the line. |
| Flush now | `/api/flush` | `flush_now()` | idle, ready | Runs the flush/solvent pump at the set rate/duration. | Clear the line between runs. |
| Reset | `/api/reset` | `reset()` | estop, ready | Idles all, returns to **idle**. | Clear an E-stop (or a finished "ready") back to idle. |
| 🟦 Vent all pumps | `/api/vent` | `vent_all()` | any | `idle_all()` (P0 → chamber pressure 0), state → idle, **keeps** the queue and auto-run. An E-stop is **latched** and not cleared. | Release chamber pressure without abandoning the run plan. |
| 🛑 EMERGENCY STOP | `/api/estop` | `estop()` | any | `idle_all()`, state → **estop**, clears the current recipe, **turns auto-run OFF**. Locks out Start until Reset. | Fault / danger — stop everything immediately. |
| 🗑 Clear queue | `/api/queue/clear` | `clear_queue()` | any | Empties pending recipes (does not touch a running one). | Drop queued conditions. |
| Tare — Pressure / Flow / Both (per pump) | `/api/tare` | `tare_pump()` | idle, ready, estop | Zeroes the pump's pressure (`R0`) and/or flow sensor (`R1`). | Calibrate a pump while stopped. |
| Apply limits (per pump) | `/api/pumps` | `set_pump_limits()` | any | Updates per-pump min/max flow used for validation + dashboard bars, and `cal ×`. | Constrain a pump's allowed flow range. |

**Four controls call the same `idle_all()`** — E-stop, Vent, Reset, and
Abort-during-flush. They are behaviourally distinct, but only by the resulting
**state** and **what they preserve**, which the labels don't make obvious:

| Control | Ends in state | Keeps queue? | Keeps current recipe? | Re-arm needed? |
|---|---|---|---|---|
| EMERGENCY STOP | estop | yes | no | must Reset first, **and re-enable auto-run** |
| Vent all pumps | idle | yes | no | no (Start again) |
| Reset | idle | yes | — | no |
| Stop → flush (during flush) | idle | yes | — | no |

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
  - `/api/recipes_folder` (GET+POST) → the watched conditions folder (persists in
    `reactor_settings.json`); `/api/project` → current project root
  - `/api/config` → bounds / pump names / flush defaults for the UI form
  - `/api/slack` (GET+POST) → notification settings; `/api/slack/test` → send a test message
  - `/api/health` → hub health probe (state, queue length, runs completed)
  - `/api/status`, `/api/stream` (SSE, calls `_ctrl.status()` every 0.5 s)
- Persistence helpers: `_limits_path`/`_save_limits`/`_load_limits`
  (`<project_root>/reactor_limits.json` — **returns `None` and silently skips when
  no project root is set**, and is replayed over `config.yml` on `/api/set_project`).

## reactor/templates/index.html — UI (one file: HTML + JS)

- `pushSpec()` → POSTs the Data-collection card (exposure/frames/tags/lead/**data_dir**).
- `pushRun()` / run-settings inputs → `/api/run_settings` (arm mode, durations, flush).
- `collectNow()` → `/api/collect_now`. Arming UI: `armModeChange()`.
- Live plot: `BL` buffer, `drawBeamline()`, `blLegendHTML()` (temp 2 dp, bstop/I0 4 dp),
  collection 📷 markers (`MARKS`, `updateCollectBadge()`).
- Status sync: the SSE handler fills fields once (`window._specInit`) and updates readouts
  (`t-cur`, `t-bstop`, …).

## src/reactor/controller.py — the brain (state machine + run loop)

States: `idle`, `arming`, `running`, `flushing`, `ready`; `estop` from anywhere.
With the shipped `spec.background_when: "before"` the **order is
`flushing → arming → running → flushing`** — pressing Start begins a flush, not an
arm (see the lifecycle below).

- **Lifecycle** (`spec.background_when: "before"`, the shipped default — the
  background precedes its synthesis, so a run **starts with a flush**):
  `submit` (queue) → `start`/`start_now` → `_begin_next` → stashes the recipe in
  `_pending` and calls `_enter_flush(kind="blank", bkg_recipe_id=<upcoming id>)` →
  background shot at `flush_deadline − spec_lead` → `_end_flush` sees
  `_flush_kind == "blank"` and calls `_start_recipe` (this is where arming actually
  happens: `temperature` or `timed`, `arm_wait_s`/`default_wait_s`) →
  `_enter_running` (pumps on) → sample shot at `run_deadline − spec_lead` →
  `_end_run` (reagents off + **cooldown** `csettemp`) → `_enter_flush(kind="flush")`
  → `_end_flush` → next recipe or `ready`.
  With `background_when: "after"` (legacy) `_begin_next` goes straight to
  `_start_recipe` and the blank is collected during the post-run flush instead.
  `_bkg_recipe_id` decides whether a flush collects a background at all (empty =
  none).
- **Control loop:** `_loop` (background thread) is only the wrapper — it calls
  `_tick_once` and E-stops on any escaping exception. `_tick_once` holds the actual
  per-tick logic: state advance, deadlines, firing the sample shot at
  `run_deadline − spec_lead` and the background shot at `flush_deadline − spec_lead`;
  `_safety_check` enforces `T_max` etc. (and notes once per run when temperature
  polling is paused for an acquisition).
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
- Read path: `_read_source` (`config.yml spec.read_source`) picks between the SPEC
  counters and `_read_epics_state` (`caget` on `spec.epics_pvs`; sets
  `EPICS_CA_ADDR_LIST` before importing `epics`, returns `{}` on any error so the
  plot just gaps). The EPICS path takes no SPEC control and works during a collection.
- Hard SPEC guard: `collect` holds the lock the whole acquisition; `read_state` is
  non-blocking (skips during a collection unless `read_during_collect`), which is
  why the SPEC read path goes dark for `frames × exposure` seconds per shot.
- Helpers: `render_macro` (fills `{{markers}}`), `macro_command_lines` (splits a flat
  macro into SPEC commands).

## Config & macros

- `reactor/config.yml` — pumps, `arming`, `flush`, `safety`, `temperature`, `run`,
  `folders`, `notify`, `spec`. Key-by-key reference: `docs/REACTOR_SETUP.md` §5.
- `reactor/macros/Singlesnapshot.flat.template.txt` — used by **commands** mode
  (plain action commands, values inlined).
- `reactor/macros/Singlesnapshot.template.txt` — **qdo mode only** (SPEC variable
  assignments + `eval(sprintf)`, which don't survive `execute_command`). Never
  point `commands` mode at this file: `sopen` fires while the save path may not be set.

## Related (closed loop, not the reactor app itself)

- `src/optimizer/` — Bayesian campaign proposing next conditions (`campaign.py`).
- `src/analysis/nanoparticle.py` — size/PDI/phase/confidence from subtracted SAXS.
- `src/loop_naming.py` — the `recipe_id` filename convention shared with the pipeline.

---

## Standalone bench tools (bypass the app; one SPEC client at a time)

Five `tools/beamline_*_test.py` scripts — runbook, order and safety notes:
`tools/BEAMLINE_TESTING.md`. Safety audit: `docs/audits/BEAMLINE_SAFETY_AUDIT.md`.
