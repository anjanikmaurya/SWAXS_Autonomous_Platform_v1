# Autonomous Synthesis (reactor app): software setup

**What this is for.** Installing, configuring and first-running the Autonomous
Synthesis app (port **5108**), which drives the five-pump flow reactor and the SPEC
beamline (temperature, shutter, 2D collection). Related docs:

- the physical rig (pumps, temperature, beamline wiring):
  [REACTOR_HARDWARE_SETUP.md](REACTOR_HARDWARE_SETUP.md)
- pump flow calibration: [PUMP_FLOW_CALIBRATION.md](PUMP_FLOW_CALIBRATION.md)
- bench tests for the beamline link: [tools/BEAMLINE_TESTING.md](../tools/BEAMLINE_TESTING.md)
- running a campaign: [AUTONOMOUS_RUN_STEPS.md](AUTONOMOUS_RUN_STEPS.md)
- where things are in the code: [REACTOR_MAP.md](REACTOR_MAP.md)

---

## 1. Install

Install the platform as in [QUICKSTART.md](../QUICKSTART.md) (Python 3.10 or newer,
`pip install -r requirements-core.txt`). That is enough for **Simulation**, which
needs no hardware at all.

For the **real rig** also install:

```bash
pip install -r requirements-hardware.txt   # pyserial (pumps) + pyepics (EPICS reads)
```

Real hardware also needs USB access to the Mitos pumps and network access to the
SPEC bServer (default `http://127.0.0.1:18085/SIS/`).

## 2. Simulation and Hardware

The app talks to two layers, **pumps** and **beamline**, and one toggle in its top
bar covers both:

- **Simulation** (backend `mock`): simulated pumps, temperature and synthetic 2D
  frames ([SIMULATED_2D_DATA.md](SIMULATED_2D_DATA.md)). The default.
- **Hardware** (backend `real`): the real pumps and SPEC.

The toggle is locked while a run is in progress. The startup backend comes from the
environment variable `SWAXS_REACTOR_BACKEND` (`mock`, the default, or `real`); the
toggle choice does not survive an app restart, so check it after every restart.
Every run record notes which backend produced it.

Switching the toggle does **not** re-read `reactor/config.yml`. After editing that
file, restart the app.

## 3. Configure `reactor/config.yml`

Everything rig-specific lives in this file, each key with a comment. The parts to
check:

- **`pumps`**: one entry per pump. Matched by `serial` first (portable across PCs,
  COM numbers change), with `address` (COM/tty) as fallback; map them with
  `tools/map_pumps.py`. `sensor_min` / `max_flow` are hard limits from the installed
  LG16 sensor: a recipe whose setpoint for a pump is nonzero but below `sensor_min`,
  or above `max_flow`, is rejected, never clamped.
  **The shipped config has `sensor_min: 0.0` on every pump, and on Hardware the app
  refuses to open the pump ports until each one is set to the sensor's real
  minimum.** Optional per pump: `calibration_factor`, `flowrate_table`,
  `volume_limit`, `max_pressure` (mbar).
- **`bounds`** / **`safety`**: allowed recipe ranges and absolute caps (`T_max`,
  `F_tot_max`, `per_pump_max`, `max_pressure`). A breach is rejected when the recipe
  is submitted and trips the E-stop during a run. `safety.flow_fault_estop: false`
  (shipped) means a pump that stays off its setpoint warns instead of E-stopping.
- **`temperature`**: `tolerance` (2 °C), `read_interval_s` (1 s) and `cooldown_c`
  (25 °C: the temperature set when a synthesis ends; `null` leaves it as is).
- **`arming`**: `default_mode` is `temperature` (shipped: wait until the reactor is
  at temperature) or `timed` (wait `default_wait_s`, 120 s). These are the only two
  modes; a condition file with any other `arm_mode` is rejected.
- **`flush`**: `rate` (50 µL/min), `duration` (1200 s = 20 min), `blank_rinse_s`
  (30 s) and `pump` (shipped: `ode_dilution`, a reagent pump capped at 50 µL/min;
  switch to `ode_flush` when the dedicated line works). All but `blank_rinse_s` can
  be changed in the app.
- **`spec`** (beamline):
  - `base_url` (bServer), `enabled` (false = run the loop with no 2D collection)
  - `read_source`: `"spec"` (shipped; counters, refreshed with `read_refresh_cmd:
    "ct 0.1"`) or `"epics"` (recommended; keeps reading during a collection, uses
    `epics_pvs` and `epics_ca_addr_list`). See §6.
  - `temp_counter: CTEMP`, `i0_counter`, `bstop_counter` (only for `"spec"`);
    `read_during_collect` (keep polling counters during a collection, only if the
    bServer allows it); `refresh_min_interval_s` (minimum gap between `ct` refreshes)
  - `set_temp_cmd: "csettemp {T}"`, `open_shutter_cmd`, `close_shutter_cmd`
  - `macro_file` (the **flat** `reactor/macros/Singlesnapshot.flat.template.txt`) and
    `collect_mode: commands`
  - `data_dir`: the Linux folder SPEC saves to (the pipeline's 2D base);
    `data_dir_from_hub` + `hub_path_map` translate the hub's Windows project path
    (`X:\bl1-5`) to the Linux one (`/msd_data/checkout/bl1-5`). Confirm both prefixes
    on the rig.
  - `spec_lead_s`, `exposure_s`, `frames`, `sample_tag`, `bkg_tag` (the app's Data
    collection fields)
  - `background_when`: `"before"` (shipped: blank on the clean capillary before each
    synthesis) or `"after"`
  - `mock_data_dir` and `simulator.*`: Simulation only
- **`run`**: `default_duration`, `resume_auto_run: false` (Run autonomously is not
  switched back on after a restart), `clear_queue_on_restart: false` (the queue in
  the conditions folder is kept across restarts, so the reactor and the analyser can
  be started in either order).
- **`folders`**: `recipes: 1D/SAXS/Conditions` (the watched conditions folder; it
  can also be changed in the app), relative to the project folder.

Pump limits and `Cal ×` set in the app are saved to
`<project>/reactor_limits.json` (only when a project folder is selected) and
override the values in `config.yml` from then on.

## 4. Run

Start the platform with the launcher (`./start_platform.sh` or
`start_platform.bat`), choose the project folder in the hub, then press **Start**
and **Open** on the **Autonomous Synthesis** card. The hub passes the project folder
to the app and stops it when the hub closes.

For development you can start it alone, with the environment activated:

```bash
python reactor/app.py                              # http://localhost:5108
SWAXS_REACTOR_BACKEND=real python reactor/app.py   # start on Hardware
```

## 5. First-run checklist

1. The app opens at http://localhost:5108 and the top-bar toggle shows the mode you
   expect.
2. **Hardware test** tab → **Run all checks**. It only reads (pumps connected and
   answering, pressure, temperature live, I₀/bstop arriving) and gives a fix hint for
   each failure.
3. **In Simulation:** queue a recipe (**Autonomous setup**, step 4, **Add to
   queue**) and press **Start** in the left column. With `background_when:
   "before"` the sequence is flush → background shot → arm → synthesis → sample
   shot → flush. The first thing you see is a full flush (20 min at the shipped
   duration), not arming: the app has not hung. Shorten **Flush duration** for a
   quick test.
4. **On the rig:** bench-test the beamline first with the app stopped
   ([tools/BEAMLINE_TESTING.md](../tools/BEAMLINE_TESTING.md)), then press **Collect
   now** once from the app (Hardware test, step 3).
5. The reduction app sees the new `.raw` under `<project>/2D/SAXS`.

## 6. Safety notes (Hardware)

- **Stop** and **EMERGENCY STOP** act on the **pumps only** and never interrupt an
  X-ray collection in progress.
- **E-stop also turns Run autonomously off**, so the folder watcher cannot restart
  the rig into an unresolved fault. Clear the fault, press **Reset**, then switch
  **Run autonomously** on again deliberately.
- **Start pumps now** (skipping the arming wait) asks for confirmation and shows the
  current and target temperature.
- **With `read_source: "spec"` the over-temperature interlock has no fresh reading
  during every acquisition.** A collection holds the SPEC link, so temperature, I₀
  and bstop stop updating for the whole shot (10 s × 10 frames = 100 s) and the live
  plot freezes for that window; the app logs it once per run. Fix: `read_source:
  "epics"` (verify the PVs with `tools/beamline_epics_test.py`), or
  `read_during_collect: true` if the bServer tolerates counter reads mid-scan.
- `ct 0.1` obeys SPEC's `sauto` and may open the shutter. Run `sauto off` in SPEC,
  or use `read_source: "epics"`, which never counts. See
  [audits/PRE_BEAMTIME_READINESS.md](audits/PRE_BEAMTIME_READINESS.md).
- With `read_source: "spec"`, the app holds SPEC remote control while it is open on
  Hardware. Stop the app or switch to Simulation to hand control back. Stopping the
  app idles the pumps, closes the shutter and releases SPEC control.
- Run **one SPEC client at a time**: do not run the bench tools against the real
  beamline while the app is on Hardware.
- Every SPEC command the platform sends:
  [audits/BEAMLINE_SAFETY_AUDIT.md](audits/BEAMLINE_SAFETY_AUDIT.md).
