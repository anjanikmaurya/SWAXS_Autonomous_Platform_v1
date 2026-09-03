# Flow Synthesis — knowledge

The Flow Synthesis app (port 5108) is the **execution layer** for the 5-pump
continuous-flow nanoparticle reactor (Fong et al., J. Chem. Phys. 154, 224201,
2021 — Dolomite Mitos P-pumps + LG16 flow sensors). It receives an
already-predicted recipe and drives the pumps. It does **not** run the Bayesian
optimization / SAXS analysis — those push recipes to it (the Analyzer app on
port 5107 owns the optimizer).

## Pumps & setpoints

Five pumps: `pd_top_precursor`, `oleylamine`, `top`, `ode_dilution`, `ode_flush`.
A recipe (`T_reac`, `F_tot`, `x_ODE`, `x_TOP`, `x_oley`) converts to flows (µL/min):

- `ode_dilution = x_ODE · F_tot`
- `top = x_TOP · F_tot`
- `oleylamine = x_oley · F_tot`
- `pd_top_precursor = (1 − x_ODE − x_TOP − x_oley) · F_tot`
- `ode_flush = 0` during synthesis

Each pump's `[sensor_min, max_flow]` is a **hard limit**: a recipe whose computed
setpoint for any pump is nonzero-but-below its minimum, or above its maximum, is
**rejected** (nothing is clamped or sent to hardware); a true 0 is always allowed.
Recipes are also validated against config bounds and **hard safety caps**
(`safety.T_max`, default 320 °C; `safety.per_pump_max`); a runtime breach trips
the emergency stop.

## Intake

Three ways in:
1. **A watched conditions folder.** The default is `1D/SAXS/Conditions`
   (`folders.recipes` in `reactor/config.yml`) — a sibling of
   `1D/SAXS/Subtracted`, resolved relative to the PROJECT ROOT unless absolute.
   It accepts `*.dat` and `*.txt` files written by the ML/optimizer pipeline as
   well as `*.json`. Consumed files are moved to `folders.processed`
   (`1D/SAXS/Conditions/done`). The folder can be changed live from the app's
   "📁 Conditions folder" card; the override is persisted in
   `reactor_settings.json` at the project root and reloaded on the next start.
2. `POST /api/recipe` — JSON, for the BO/SAXS side.
3. The manual form in the UI.

An **Auto-run** toggle decides whether an arriving recipe starts automatically or
waits for operator Start. Recipes that arrive while busy are **queued (FIFO)**;
`POST /api/queue/clear` empties the queue.

Folder polling interval is `poll_interval` (default 3.0 s).

## Run lifecycle

`STATES = ["idle", "arming", "running", "flushing", "ready", "estop"]` — `estop`
is a real state, not just an action.

Normal path: `idle → arming → running → flushing → ready`.

Temperature is **commanded through SPEC** (`set_temp_cmd`, default
`csettemp {T}`) and read back from the `CTEMP` counter (`temp_counter`) or from
EPICS when `spec.read_source: "epics"` — the app is not gate-only.

### Arming — TWO modes only
`arming.default_mode` accepts exactly `"temperature"` or `"timed"`:
- **temperature** — wait for `CTEMP` to reach and hold `T_reac`. Needs a wired
  temperature reading.
- **timed** — wait a fixed number of seconds (`arming.default_wait_s`), then start
  the pumps regardless of temperature. Use this when no thermocouple is connected
  to this machine.

The **shipped default is `timed` with a 120 s wait**. A recipe may override with
`arm_mode` / `arm_wait_s`. Anything other than `temperature` or `timed` —
including `"ramp"` — is REJECTED at intake and will stall the queue. There is no
ramp arming mode.

### Ending a run
The PRIMARY end condition is `run.end_on_measurement: true` — the run ends when a
new SAXS averaged file appears. That is detected from the `file.averaged` bus
event, with a folder watch on `folders.averaged_watch` (`1D/SAXS/Averaged`) as a
backstop. `run.default_duration` (600 s = 10 min) is only the FALLBACK if no
measurement signal arrives; a manual Stop also ends the run.

`run.advance_on_new_file: true` adds the autonomous hold-until-next-file
behaviour: the current condition keeps flowing until the next parameter file is
queued, then flushes and advances. `run.min_dwell_s` (60 s) is the minimum time a
condition runs before it may advance, and advancing is additionally blocked until
the 2D collection has fired, so a run can never finish with no data.

### 2D collection and background timing
`spec.background_when: "before"` is the DEFAULT and the current behaviour:
flush the line, collect the blank on the CLEAN capillary tagged with the
**upcoming** recipe_id, then run the synthesis. The background is therefore
already on disk when the sample frames land, so subtraction can start immediately
and the sample/background pairing is unambiguous. `"after"` is the legacy mode
(blank collected during the post-synthesis flush, tagged with the run that just
finished). Either way the pair shares one `recipe_id`, which is how the
Background app matches them.

Filenames carry role tags: `{recipe_id}_{spec.sample_tag}` (default `sample`) and
`{recipe_id}_{spec.bkg_tag}` (default `bkg`). The sample acquisition fires
`spec.spec_lead_s` before the run end.

`POST /api/collect_now` triggers a 2D collection on demand.

### Abort, E-stop, cooldown
**Abort** goes straight to flush. **E-stop** idles everything immediately (state
`estop`; `/api/reset` clears it). `temperature.cooldown_c` (25.0 °C in the shipped
config) is commanded the moment a synthesis run ends; unset/None leaves the
temperature as-is. `POST /api/vent` vents all pumps.

### Restart recovery — auto-run is NOT resumed
`run.resume_auto_run: false` is the shipped default and is a deliberate safety
choice: the data-processing apps resume their loops after a restart because they
only read and write files, but auto-run MOVES PUMPS. A power blip must not start
reagents flowing into a hot reactor with nobody in the hutch. After a restart the
app reports that auto-run had been on and waits for a deliberate Start. Set it
true only for fully unattended recovery.

## Flush & feedback

After every run the 4 reagent pumps zero and the flush pump runs at
`flush.rate` (50 µL/min) for `flush.duration` (1200 s = 20 min); new recipes are
blocked until it is done. A manual **Flush now** (`POST /api/flush`) is also
available.

The configured flush pump is **`ode_dilution`**, not `ode_flush`
(`flush.pump: "ode_dilution"`) — the same ODE line, used while `ode_flush` is
down; switch back to `ode_flush` in the app once it is fixed. A reagent pump used
for flushing is capped at its own `max_flow` (`ode_dilution` ≤ 50 µL/min,
auto-clamped), so a flush rate above that is not honoured.

After flushing it **auto-advances** to the next queued recipe. On completion it:
- records the run in `manifest.json` under the `reactor` key
  (`reactor.runs.<recipe_id>`),
- writes `<feedback folder>/<recipe_id>.done.json`. `folders.feedback` is
  `reactor/feedback`, resolved **relative to the PROJECT ROOT** — so the file
  lands in `<project_root>/reactor/feedback/`, not in the repo's `reactor/`
  folder. The done file carries the full run record plus a `flow_series`
  delivered-flow trace (sampled every `run.log_interval_s`), which is kept out of
  `manifest.json` and the bus to avoid bloat.
- emits a `reactor.run_complete` bus event carrying `recipe_id`, `recipe`,
  `setpoints`, `measured_flows`, `started`, `ended`, `duration_s`, `reason` and
  `status`, so the optimizer can predict the next recipe.

A run that never left `arming` produces no record at all (no flow, no
measurement), so the optimizer can never train on a synthesis that did not happen.

## Notifications

`GET/POST /api/slack` reads and sets the Slack webhook configuration;
`POST /api/slack/test` sends a test message. Notification failures never disturb
the reactor.

## Other endpoints

`GET /api/health`, `GET /api/status`, `GET /api/stream` (SSE log),
`GET /api/config`, `GET/POST /api/pumps` (pump flow limits, persisted in
`reactor_limits.json`), `GET/POST /api/recipes_folder`, `POST /api/set_project`,
`GET /api/project`, `POST /api/start`, `POST /api/start_now`, `POST /api/abort`,
`POST /api/estop`, `POST /api/reset`, `POST /api/vent`, `POST /api/flush`,
`POST /api/auto_run`, `POST /api/queue/clear`, `POST /api/collect_now`,
`POST /api/spec_settings`, `POST /api/run_settings`, `POST /api/tare`,
`POST /api/backend`.

`POST /api/tare` triggers a pump tare; `POST /api/backend` switches the
mock/real backend at runtime.

## Hardware swap

`backend=mock` (default) uses in-memory pumps so everything runs with no
hardware. Set `SWAXS_REACTOR_BACKEND=real` to use the vendored `Py_P_Pump` SDK
(`src/reactor/drivers/`). The real call points are marked `⟵ REAL DRIVER` in
`src/reactor/hardware.py`. The same switch also selects the beamline backend, so
one setting covers pumps and SPEC. Pumps are assumed **pre-tared** (the SDK tare
is interactive and is done from a console).

There is deliberately **no time compression anywhere**, mock included: every
duration is real seconds on every backend, so a mock rehearsal is timed exactly
like the beamline run it stands in for. Shorten the durations themselves for a
short test.

## Configuration precedence

Defaults live in `reactor/config.yml`, but **live overrides made in the app
persist to `reactor_settings.json`/`reactor_limits.json` at the project root and
WIN over the YAML** on the next start. If a value in the app disagrees with
`config.yml`, the settings file is the one in force. `run_settings` values set
from the app (`arm_mode`, `arm_wait_s`, `flush_rate`, `flush_duration`,
`flush_pump`, `run_duration`) apply for the life of the process; a blank field is
ignored rather than reset, and they fall back to the config defaults only after a
restart.
