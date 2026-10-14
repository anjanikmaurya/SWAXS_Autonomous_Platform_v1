# Pre-beamtime readiness: go / no-go checklist

**What this is for.** The last check before an autonomous reactor run at the
beamline. Work through it top to bottom at the start of every beamtime; every item
is something that has, at some point, made a run silently go nowhere or put the rig
in an unsafe state. The step-by-step procedure is in
[../AUTONOMOUS_RUN_STEPS.md](../AUTONOMOUS_RUN_STEPS.md); the beamline bench tests are
in [../../tools/BEAMLINE_TESTING.md](../../tools/BEAMLINE_TESTING.md).

Software check first: `python -m pytest -q` in the platform folder should report
zero failures.

---

## 1. Beamline link (bench tests, reactor app stopped)

- [ ] Counter reads live (CTEMP / bstop / I₀ with the `ct 0.1` refresh): `beamline_read_test.py`
- [ ] If using EPICS reads: the PVs answer and move: `beamline_epics_test.py`
- [ ] Temperature set and readback (`csettemp`): `beamline_temp_test.py`
- [ ] Shutter opens and closes (`sopen` / `sclose`): `beamline_shutter_test.py`
- [ ] One 2D collection end to end (flat macro, `commands` mode): `beamline_collect_test.py --fire`

## 2. `reactor/config.yml`

1. **Every pump's `sensor_min` is the installed sensor's real floor, not 0.** It
   ships as `0.0`, which would disable the "nonzero setpoint below the minimum is
   rejected" check. The app refuses to open the pump ports on Hardware in that
   state; put the real floors in, do not zero them to get past it.
2. **`spec.simulator.enabled: false`.** It ships `true`. It has no effect on the
   Hardware backend, but synthetic frames look entirely real, so turn it off.
3. **spec block:** `temp_counter: CTEMP`, `read_refresh_cmd: "ct 0.1"`,
   `collect_mode: commands`, `macro_file: …flat.template.txt`, `data_dir` = the SPEC
   `/msd_data/...` folder, and both `hub_path_map` prefixes confirmed.
4. **`sauto off` in SPEC.** This is a dose decision, not optional. With
   `read_source: "spec"` the app runs `ct 0.1` before **every** read (about 86,400
   counts a day at the 1 s read interval, between runs included) so the
   temperature / bstop / I₀ trace is live. `ct` obeys `sauto` and may open the fast
   shutter onto whatever is in the beam. `sauto off` makes that safe; the collection
   macro opens and closes the shutter itself. Alternatives:
   - `spec.refresh_min_interval_s: 10.0`: 10× fewer counts; the interlock and plot
     still update at 1 Hz but the trace steps every 10 s.
   - `spec.read_source: "epics"`: no counting at all, a smooth trace, and readings
     continue during an acquisition. Verify with `tools/beamline_epics_test.py`.
5. **Temperature reading during collections.** With `read_source: "spec"` the
   temperature (and the over-temperature interlock) has no fresh value for the
   whole of every acquisition (about 100 s at 10 s × 10 frames). That is expected,
   but you must be able to tell it from a dead sensor. `read_source: "epics"` keeps
   reading.
6. **Flush pump.** `flush.pump` ships as `ode_dilution` (a reagent pump, capped at
   50 µL/min), not `ode_flush`. Confirm that is what is plumbed.
7. **Calibration edited in `config.yml`?** Restart the app. Switching Simulation |
   Hardware does not re-read the file, and a saved `<project>/reactor_limits.json`
   overrides the file (see [../PUMP_FLOW_CALIBRATION.md](../PUMP_FLOW_CALIBRATION.md)).
8. **`run.resume_auto_run: false`** (shipped). After any restart, switch **Run
   autonomously** on by hand. An E-stop also switches it off.

## 3. Data path (where a run silently goes nowhere)

- [ ] **Project folder** selected in the hub, and every started app shows it
  (Autonomous Synthesis and Autonomous Analyser especially). If the `X:` drive
  mounts late, choose the folder again once it appears.
- [ ] **`<project>/config.yml`** → `data_directory` = `<project>/2D`. Reduction puts
  `1D/` next to `data_directory`, so a stale value moves all outputs away from the
  other apps. `poni/` holds the files `config.yml` names.
- [ ] **SPEC save folder.** The reactor log line `SPEC data_dir → /msd_data/…` shows
  your project's path. If the `hub_path_map` prefix is wrong it stays at the shipped
  `/msd_data/.../Auto_Test` and SPEC writes where nothing is reduced. Check the
  resolved path in the app, not the config.
- [ ] **Frames per average.** In Visualisation & Average → Auto-Average, **Frames per
  average (N)** must not exceed the reactor's **Number of frames** (shipped: 30 and
  10). Otherwise no average is ever written and the loop stops; the app warns when
  the monitor starts.

## 4. Before you press "Run autonomously"

1. Autonomous Synthesis top-bar toggle = **Hardware** (covers pumps and beamline).
   It resets to the startup default (`SWAXS_REACTOR_BACKEND`, default `mock`) when
   the app restarts, so check it again after any restart.
2. **Hardware test → Run all checks** (read only) and fix any failed line.
3. **Arming mode matches the rig.** The shipped default waits for the reactor to
   reach temperature, which needs a live thermocouple; the app warns at the start of
   arming if the reading is not trustworthy. Check **Autonomous setup → 2 · Timing**.
4. Only one SPEC client: no bench tool running alongside the app.
5. One **Collect now** from the app (Hardware test, step 3), and the new `.raw`
   appears under `data_dir/2D/SAXS` and is reduced (via `X:\`).
6. Reduction, averaging and subtraction monitors started; the analyser campaign
   started with the target diameter, tolerance and bounds that match the chemistry.
7. Slack notifications from Autonomous Watch tested
   ([../NOTIFICATIONS.md](../NOTIFICATIONS.md)).
8. Watch the first full cycle end to end before leaving.

## 5. End of the run

- Stop the Autonomous Synthesis app from the hub: it idles the pumps, closes the
  shutter and releases SPEC remote control for beamline staff.
- Each synthesis that ends normally or with **Stop → flush** sets the reactor to
  `temperature.cooldown_c` (25 °C). An E-stop leaves the temperature as it is, so
  after an E-stop cool it by hand (`csettemp <ambient>`).

---

## What the software guarantees (verified by the test suite)

| Behaviour | Test |
|---|---|
| Stop / E-stop act on the pumps only; nothing is sent to SPEC | `test_estop_is_pumps_only_leaves_beamline_untouched` |
| A collection in progress is never interrupted; reads skip during it | `test_collection_blocks_commands_and_reads_skip` |
| Optional reads during a collection (`read_during_collect`) | `test_read_during_collect_keeps_polling` |
| On exit or when the hub stops the app: pumps idled, shutter closed, SPEC control released | `test_r1_the_reactor_hands_the_rig_back_on_sigterm` |
| Sample shot and background shot tagged `<recipe_id>_sample` / `<recipe_id>_bkg` | `test_reactor_fires_sample_then_background_tagged` |
| **Collect now** works only when idle | `test_collect_now_manual_and_guarded` |
| The Simulation / Hardware toggle switches pumps and beamline together | `test_backend_switch_covers_pumps_and_beamline` |
| 2D collection can be disabled (`spec.enabled`) | `test_spec_can_be_disabled` |
| The flat macro is streamed as plain SPEC commands, no file written | `test_commands_mode_streams_lines_no_file` |

Also: E-stop idles every pump with a separate guard each, so one failure cannot
block the rest; a lost or hung pump is detected; pumps are matched by exact serial
(a missing or ambiguous port is refused rather than guessed); a pump commanded to
flow but delivering almost nothing is a flow fault. Apart from taking and releasing remote
control, the platform sends SPEC only `csettemp`, `ct`, `sopen` / `sclose` and the
collection macro, and no pipeline app
talks to the bServer. Every command is listed in
[BEAMLINE_SAFETY_AUDIT.md](BEAMLINE_SAFETY_AUDIT.md).
