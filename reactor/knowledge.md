# Autonomous Synthesis: knowledge

## 1. What the app is for

The Autonomous Synthesis app (port 5108) is the **execution layer** for the
5-pump continuous-flow nanoparticle reactor (Fong et al., J. Chem. Phys. 154,
224201, 2021: Dolomite Mitos P-pumps with LG16 flow sensors). It takes an
already-chosen recipe, sets the reactor temperature through SPEC, drives the
pumps, triggers the 2D SAXS acquisitions (sample and background), and flushes
the line between conditions. It does **not** choose recipes or analyse data:
the Analyzer app (port 5107) owns the Bayesian optimizer and writes the
conditions this app runs.

## 2. Where it sits in the pipeline

- **Reads** condition files from the conditions folder
  (`folders.recipes`, default `1D/SAXS/Conditions` under the project root,
  a sibling of `1D/SAXS/Subtracted`). Accepts `*.dat` / `*.txt` from the
  optimizer and `*.json`, oldest first (filename breaks a tie). Recipes can
  also arrive by `POST /api/recipe` or the manual form.
- **Writes** 2D frames via SPEC (or the simulator) into the 2D tree, named
  `{recipe_id}_{sample_tag}` and `{recipe_id}_{bkg_tag}` (defaults `sample`,
  `bkg`). The shared `recipe_id` is how the Background app pairs them. The
  keywords are not free text: the sample keyword must be `sample` or `smp`,
  the background keyword one of `background`, `solvent`, `buffer`, `blank`,
  `empty`, `water`, `bkg`, `bg` (`src/loop_naming.py`). Anything else is
  refused with a message, because the averaging and subtraction apps could
  not tell the two acquisitions apart.
- **Withdrawn conditions**: a condition file the analyzer moves to
  `Conditions/done/` (new campaign, abort, or an expired condition) is dropped
  from the queue, including one staged behind its pre-synthesis blank flush.
  The blank flush itself carries on (solvent only) and ends as a plain flush.
- **Listens** for the `file.averaged` bus event (with a folder watch on
  `folders.averaged_watch`, `1D/SAXS/Averaged`, as backstop) to know the
  measurement for the current recipe is done.
- **Records** each finished run in the manifest under `reactor.runs.<recipe_id>`,
  writes `<project_root>/reactor/feedback/<recipe_id>.done.json` (full record
  plus a `flow_series` delivered-flow trace, sampled every `run.log_interval_s`,
  default 2 s), and emits `reactor.run_complete` (recipe_id, recipe, setpoints,
  measured_flows, started, ended, duration_s, reason, status) for the optimizer.
  A run that never left `arming` produces no record, so the optimizer never
  trains on a synthesis that did not happen.
- A condition file is moved to `folders.processed` (`1D/SAXS/Conditions/done`)
  only when the reactor is **finished** with it (ran, abandoned, or cleared from
  the queue), so a restart re-reads anything still waiting.
- **Notifications**: the reactor sends none. All platform notifications come
  from Autonomous Watch (port 5110); see [NOTIFICATIONS.md](../docs/NOTIFICATIONS.md).

## 3. How to use it

### Page layout

- **Top bar**: **Simulation | Hardware** toggle (switches pumps AND beamline
  together; allowed only when idle, ready or in E-stop, and not during a
  collection), the project folder, and a link back to the hub.
- **Left column**: synthesis route (Flow reactor; Liquid handling robot and
  Syringe well plate are placeholders) and the **Pump controls**, visible from
  every tab: **Start**, **Stop → flush**, **Flush now**, **Vent all pumps**,
  **Reset**, **EMERGENCY STOP**. While arming, Start becomes "Start pumps now"
  (skips the wait, asks for confirmation) with the arming progress above it.
- **Tabs**: **Hardware test**, **Autonomous setup**, **Live plots**.
- **Activity log**: docked under the tabs, always visible.

### Step by step

1. **Pick the backend.** Simulation (the default) runs simulated pumps,
   temperature and synthetic 2D data. Hardware opens the pump serial ports and
   connects to the SPEC bServer (close the Dolomite GUI first).
2. **Hardware test tab.**
   - **Run all checks**: read-only checklist (pumps connected, pumps answering,
     pressure under each ceiling, temperature is a live reading, I0/bstop
     arriving), each with a fix hint. Sends nothing to pumps or SPEC
     (`src/reactor/checks.py`).
   - **1 · Tare pumps**: pressure tare (air disconnected) and flow tare (no flow).
     Idle only.
   - **2 · Pump limits (µL/min) and calibration**: Min, Max and **Cal ×**
     (water-to-fluid factor) per pump, then **Apply limits**. Saved to
     `reactor_limits.json` in the project root.
   - **3 · Probe: SAXS/WAXS beamline (SPEC)**: live bstop and I0, and
     **Collect now** (one test shot as sample or background, idle only).
   - **4 · Reactor temperature**: current vs target, with a badge showing
     whether the value is a live reading, a simulated ramp, or a placeholder.
3. **Autonomous setup tab.**
   - **1 · Recipe source: conditions folder**: set the watched folder (must
     exist). Persisted in `reactor_settings.json` and reloaded on start.
   - **2 · Timing**: start the pumps "after a fixed wait of N s" (timed) or
     when "reactor reaches temperature"; **Synthesis duration (s)**,
     **Flush rate (µL/min)**, **Flush duration (s)**, **Flush pump**.
   - **3 · Data collection**: Exposure (s per frame), Number of frames,
     Sample keyword, Background keyword, Trigger before end (s), Save folder (SPEC).
   - **4 · Run**: manual recipe (T_reac, F_tot, x_ODE, x_TOP, x_oley) with
     **Add to queue**, and **Run autonomously** (the auto-run toggle).
4. **Live plots tab**: flow rate, chamber pressure, beamline (temperature,
   bstop, I0 with collection markers), and Run status (state, recipe, elapsed,
   flush remaining, delivered volume, queue with a clear button).

### Run lifecycle

States: `idle`, `arming`, `running`, `flushing`, `ready`, `estop`.
Normal path: `idle → arming → running → flushing → ready`.

- **Arming**: temperature is commanded through SPEC (`spec.set_temp_cmd`,
  `csettemp {T}`) and read back from the `CTEMP` counter, or from EPICS when
  `spec.read_source: "epics"`.
- **Running**: pumps flow at the recipe setpoints. The sample acquisition fires
  `spec.spec_lead_s` (180 s) before the run end.
- **Ending a run**: the run ends when a new SAXS averaged file for the current
  recipe appears. This is always on: `run.end_on_measurement` is NOT read by the code
  (a no-op kept for old configs), so setting it `false` does not disable it.
  `run.default_duration` (600 s) is the fallback if no measurement arrives;
  Stop also ends the run. With `run.advance_on_new_file: true` the condition
  keeps flowing until the next condition is queued, then flushes and advances,
  but not before `run.min_dwell_s` (60 s) and not before its 2D collection has
  fired.
- **Background timing** (`spec.background_when`): `"before"` (default) flushes,
  collects the blank on the clean capillary tagged with the **upcoming**
  recipe_id, then runs the synthesis. If the line is already clean from the
  previous post-run flush, the blank uses a short rinse (`flush.blank_rinse_s`,
  30 s) instead of a full flush; cold start or post-abort does a full flush.
  `"after"` collects the blank during the post-run flush.
- **Flush**: the 4 reagent pumps zero and the flush pump runs at `flush.rate`
  for `flush.duration`; new recipes wait until it is done. On completion it
  advances to the next queued recipe **only while auto-run is on**. When a
  synthesis run ends the temperature is set to `temperature.cooldown_c` (25 °C).

### Stopping autonomous mode is a pause, not an abort

Turning **Run autonomously** off never interrupts anything: the current
condition finishes, flushes, and the loop stops at `ready` with the queue kept.
`/api/status` shows `pausing` (stopping after this one) and `paused_with_queue`
(stopped, work waiting). Pausing is the way to change data-collection settings
(see below). A paused flush is a plain clean-out and does not stage the next
blank.

## 4. Settings and defaults (`reactor/config.yml`)

All flows are µL/min, all times real seconds, temperatures °C, pressure mbar.
There is no time compression on any backend, Simulation included: shorten the
durations themselves for a quick test.

### Recipe to setpoints

A recipe (`T_reac`, `F_tot`, `x_ODE`, `x_TOP`, `x_oley`) becomes:
`ode_dilution = x_ODE·F_tot`, `top = x_TOP·F_tot`, `oleylamine = x_oley·F_tot`,
`pd_top_precursor = (1 − x_ODE − x_TOP − x_oley)·F_tot`, `ode_flush = 0`.

| Key | Default | Meaning |
|---|---|---|
| `pumps.<name>.sensor_min` / `max_flow` | 0 / 1000 (pd_top_precursor, ode_flush), 0 / 50 (oleylamine, top, ode_dilution) | Hard limits per pump |
| `pumps.<name>.max_pressure` | 10000 mbar | Pressure ceiling per pump |
| `pumps.<name>.calibration_factor` | 1.0 | Water-to-fluid correction (live-editable) |
| `pumps.<name>.flowrate_table` / `volume_limit` | unset | Optional calibration table / µL cap per run |
| `bounds` | T_reac 180–300, F_tot 40–120, each x 0–0.3, x_sum_max 0.9 | Recipe validation |
| `safety.T_max` / `F_tot_max` / `per_pump_max` / `max_pressure` | 320 / 150 / 1000 / 10000 | Hard caps |
| `safety.flow_tol` / `bad_flow_s` / `flow_settle_s` | 0.2 / 12 s / 10 s | Flow-health band, hold time, settle grace |
| `safety.flow_fault_estop` / `temp_stale_estop` | false / false | Warn only (true = E-stop) |
| `temperature.tolerance` / `stable_hold` / `timeout` | 2 °C / 5 s / 900 s | Temperature arming gate |
| `temperature.read_interval_s` | 1.0 s | Read rate for temperature, bstop, I0 |
| `temperature.cooldown_c` | 25 °C | Set when a run ends (blank = leave as is) |
| `arming.default_mode` / `default_wait_s` | temperature / 120 s | See below |
| `run.default_duration` / `min_dwell_s` | 600 s / 60 s | Fallback run time, minimum dwell |
| `run.advance_on_new_file` | true | Hold condition until the next file arrives |
| `run.resume_auto_run` / `clear_queue_on_restart` | false / false | Restart policy |
| `flush.pump` / `rate` / `duration` / `blank_rinse_s` | ode_dilution / 50 / 1200 s / 30 s | Flush |
| `spec.backend` | mock | Startup backend (UI: Simulation) |
| `spec.exposure_s` / `frames` / `spec_lead_s` | 10 s / 10 / 180 s | 2D acquisition |
| `spec.background_when` | before | Blank before or after its synthesis |
| `spec.read_source` | spec | `spec` counters or `epics` PVs |
| `spec.read_refresh_cmd` / `refresh_min_interval_s` | `ct 0.1` / 0 | Counter refresh before each read |
| `spec.data_dir_from_hub` / `hub_path_map` | true | Follow the hub folder, Windows to Linux prefix swap |
| `spec.mock_data_dir` | blank | Simulation save folder (blank = hub folder) |
| `spec.simulator.*` | enabled | Synthetic 2D frames, Simulation only |
| `poll_interval` | 3.0 s | Conditions folder polling |

### Arming modes

`arming.default_mode` accepts only `"temperature"` or `"timed"`:

- **temperature**: wait until the reactor reaches and holds `T_reac`. Needs a
  live temperature reading; without one every condition waits out
  `temperature.timeout` (900 s) and is abandoned. The app warns at the start of
  arming if the reading is not live.
- **timed**: wait `arming.default_wait_s` (120 s), then start the pumps
  regardless of temperature. Use when no thermocouple is wired to this machine.

The shipped default is `temperature`. A recipe may override with `arm_mode` /
`arm_wait_s`. Any other value (including `"ramp"`) is rejected at intake.

### Flush pump

The configured flush pump is `ode_dilution` (same ODE line) while `ode_flush` is
down; switch it in Timing. A reagent pump used for flushing is capped at its own
`max_flow` (50 µL/min for ode_dilution). Changing the flush pump during a flush
applies from the next flush.

### Which value is in force

- Conditions folder: `reactor_settings.json` (project root) wins over YAML.
- Pump limits and calibration: `reactor_limits.json` (project root) wins over YAML.
- Run settings (arm mode, wait, synthesis duration, flush rate/duration/pump)
  and data-collection settings (exposure, frames, trigger, keywords, save
  folder) are saved in `.swaxs_state/` and restored on restart if under 48 h
  old. A banner says whether they were restored or lost (back on config
  defaults). A blank field is ignored, not reset.
- The backend toggle is session-only; set `SWAXS_REACTOR_BACKEND=real` to start
  in Hardware mode.

## 5. Safety notes

- **Hard limits are not clamped.** A recipe whose setpoint for any pump is above
  `max_flow`, or nonzero but below `sensor_min`, is rejected and nothing is sent.
  Bounds and safety caps are checked at submit; a runtime breach (temperature,
  pressure, flow caps, `volume_limit`) trips the E-stop.
- **The flush is supervised** too: flow faults warn (or E-stop with
  `safety.flow_fault_estop`), a pump that should be idle but keeps flowing for
  `bad_flow_s` warns, and one that exceeds its `volume_limit` while idle E-stops.
- **EMERGENCY STOP** idles every pump at once and latches state `estop`;
  only **Reset** clears it. **Vent all pumps** releases pressure from any state
  but does not clear an E-stop. **Stop → flush** ends the run and flushes.
- **Auto-run is not resumed after a restart** (`run.resume_auto_run: false`): a
  power blip must not start reagents into a hot reactor unattended. The app
  reports that auto-run was on and waits for a deliberate Start.
- **Data-collection settings are frozen** while auto-run is on or the state is
  arming, running or flushing (`POST /api/spec_settings` returns 409 and the
  fields grey out). Changing them mid-campaign would make conditions
  incomparable. `exposure_s <= 0` is always refused (it writes blank frames).
- **`ct 0.1` may open the fast shutter.** With `read_source: "spec"` it runs on
  every read. Before an unattended run, run `sauto off` in SPEC, use
  `read_source: "epics"`, or blank `read_refresh_cmd`. With `"spec"`, reads also
  pause during each collection.
- The simulator exists only in the mock backend: Hardware mode can never write
  synthetic data.
- When the hub stops the app it first calls `/api/shutdown` to idle the pumps
  and release SPEC.

## 6. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Condition sits in arming, then abandoned after 900 s | Temperature arming with no live reading. Check the temperature badge and **Run all checks**; use timed arming if no thermocouple is wired. |
| Recipe rejected at intake | A pump setpoint outside `[sensor_min, max_flow]`, out-of-bounds recipe, or a bad `arm_mode`. The log names the field. |
| Data-collection fields greyed out | Auto-run is on or a run is active. Turn **Run autonomously** off, let the condition finish, edit, re-arm. |
| Queue not advancing after flush | Auto-run is off (paused with queue). Press **Run autonomously** or **Start**. |
| Can't switch Simulation/Hardware | Only allowed when idle, ready or estop and no collection is running. |
| Temperature / bstop frozen during a collection | Expected with `read_source: "spec"`; use `"epics"` (test PVs with `tools/beamline_epics_test.py`). |
| Wrong save folder on the rig | Check `spec.hub_path_map` prefixes, or set **Save folder (SPEC)**. |
| Did a condition actually collect? | `logs/reactor.log` has a simulator line per acquisition (frames written, peak counts, non-zero pixels) and all warnings/errors from the run. |

More: [REACTOR_SETUP.md](../docs/REACTOR_SETUP.md),
[REACTOR_HARDWARE_SETUP.md](../docs/REACTOR_HARDWARE_SETUP.md),
[BEAMLINE_TESTING.md](../tools/BEAMLINE_TESTING.md),
[PRE_BEAMTIME_READINESS.md](../docs/audits/PRE_BEAMTIME_READINESS.md).
Hardware mode needs `pip install -r requirements-hardware.txt` (pyserial,
pyepics). The real pump call sites are marked `⟵ REAL DRIVER` in
`src/reactor/hardware.py`; pumps are matched by serial number, COM port is the
fallback.

## 7. API routes

| Route | Purpose |
|---|---|
| `GET /api/health`, `GET /api/status`, `GET /api/stream` | Health, full status, SSE log + status |
| `GET /api/config`, `GET /api/project`, `POST /api/set_project` | Config and project folder |
| `GET /api/restart_notice` | Whether settings were restored or lost on restart |
| `GET/POST /api/recipes_folder` | Conditions folder |
| `GET/POST /api/pumps` | Pump limits and calibration |
| `POST /api/recipe` | Submit a recipe (JSON) |
| `POST /api/start`, `/api/start_now`, `/api/abort` | Start, skip arming wait, stop and flush |
| `POST /api/estop`, `/api/reset`, `/api/vent` | E-stop, clear, vent all pumps |
| `POST /api/flush` | Flush now (idle or ready only) |
| `POST /api/auto_run`, `/api/queue/clear` | Auto-run on/off, clear queue (files go to `done/`) |
| `POST /api/spec_settings`, `/api/run_settings` | Data-collection and run settings |
| `POST /api/collect_now`, `/api/tare` | Test acquisition, tare pumps |
| `POST /api/backend` | `{"backend": "mock"}` (Simulation) or `"real"` (Hardware) |
| `GET /api/checks`, `POST /api/checks/run` | Read-only hardware checklist |
| `POST /api/shutdown` | Make safe before exit (local only, called by the hub) |
