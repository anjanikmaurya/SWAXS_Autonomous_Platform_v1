# Starting an autonomous measurement: operator steps

**What this is for.** The order of operations for a closed-loop run at the
beamline: the reactor makes a sample, SPEC collects it, the data apps reduce,
average and subtract it, the Autonomous Analyser fits it and proposes the next
recipe, and the reactor runs that. Follow the steps in order.

Want to rehearse first with no beam? The same steps work in **Simulation**; see
[SIMULATED_2D_DATA.md](SIMULATED_2D_DATA.md). For the final go/no-go list, see
[audits/PRE_BEAMTIME_READINESS.md](audits/PRE_BEAMTIME_READINESS.md).

**Several stages are manual to start.** The reactor's conditions watcher and the
analyser's folder watcher start by themselves; the reduction, averaging and
subtraction monitors and the optimiser campaign do not. If any one of them is off,
the loop stalls at that stage. Every app also starts fresh after a stop or hub
restart: monitors do not resume and the campaign is not resumed (export
`SWAXS_RESUME=1` before launching to opt back into resuming). Reactor pump limits,
the conditions folder and the reactor's run and data-collection settings are kept.

---

## 0. Hardware and beamline (before any software)

1. Pumps powered, USB connected, lines primed, no leaks.
2. Flow cell mounted at the sample position; beam ready; hutch searched.
3. bServer (`pySSRL-bServer`) running on the control PC.
4. Temperature controller in remote/SPEC mode.
5. The beamline link bench-tested with the reactor app stopped:
   [tools/BEAMLINE_TESTING.md](../tools/BEAMLINE_TESTING.md).

## 1. Launch the platform

1. Start the hub with the launcher, `./start_platform.sh` (macOS/Linux) or
   `start_platform.bat` (Windows), and open **http://localhost:5100**.
2. **Choose the project folder** in the hub's top bar (the experiment folder with
   `2D/`, `poni/` and `config.yml`) and type your name in **Operator**.
3. Start the apps you need: **Geometry Calibration** (5101, only if the `.poni`
   files still have to be made), **Reduction & Correction** (5102),
   **Visualisation & Average** (5103), **Background Subtraction** (5104),
   **Autonomous Analyser** (5107), **Autonomous Synthesis** (5108) and
   **Autonomous Watch** (5110). Quality Gate (5105) and Guinier (5109) are
   optional.
4. Open each app and check it shows the right project folder. If one shows a
   blank or wrong folder (it can happen when a network drive mounts late), choose
   the folder again in the hub. Check Autonomous Synthesis and Autonomous Analyser
   in particular.

## 2. Check the project config

1. `<project>/config.yml` → `data_directory` is `<project>/2D`. Reduction writes
   its `1D/` tree next to `data_directory`, so a leftover value pointing elsewhere
   sends every output away from the other apps.
2. `poni/` holds the `.poni` and mask files that `config.yml` names.

## 3. Reactor and beamline setup (Autonomous Synthesis, 5108)

1. Top-bar toggle → **Hardware** (covers pumps and beamline together). On the
   **Hardware test** tab press **Run all checks** (read only: pumps connected and
   answering, pressure, live temperature, I₀/bstop arriving) and fix any failed
   line.
2. Check the activity log shows the SPEC save folder (`SPEC data_dir → /msd_data/…`)
   pointing at the `2D/SAXS` of your project. If the `hub_path_map` prefixes in
   `reactor/config.yml` are wrong, the folder stays at the shipped
   `/msd_data/.../Auto_Test` and SPEC writes where nothing is reduced.
3. **Hardware test**, step 2 **Pump limits and calibration**: per-pump min/max and
   `Cal ×` set, then **Apply limits**. See
   [PUMP_FLOW_CALIBRATION.md](PUMP_FLOW_CALIBRATION.md).
4. **Hardware test**, step 4 (or the **Live plots** tab): temperature, bstop and I₀
   are updating.
5. **Autonomous setup**, step 2 **Timing**: when to start the pumps (**after a
   fixed wait** or when the **reactor reaches temperature**), **Synthesis duration**,
   **Flush rate**, **Flush duration** and **Flush pump** (shipped:
   `ode_dilution`).
6. **Autonomous setup**, step 3 **Data collection**: **Exposure**, **Number of
   frames**, **Sample keyword** (`sample`), **Background keyword** (`bkg`),
   **Trigger before end** (longer than frames × exposure) and **Save folder
   (SPEC)**, which is filled from the hub folder.

### `reactor/config.yml` settings that change how the run behaves

Not in the UI; check them before the first cycle.

| Key | Shipped | Why it matters |
|---|---|---|
| `spec.read_source` | `"spec"` | With `"spec"`, temperature, I₀ and bstop stop updating for the whole of every collection (10 s × 10 frames = 100 s with no fresh temperature for the over-temperature interlock). `"epics"` keeps reading; verify the PVs with `tools/beamline_epics_test.py` first. |
| `spec.background_when` | `"before"` | Physical order of each cycle, see step 7. `"after"` is the older order. |
| `run.resume_auto_run` | `false` | **Run autonomously** is not switched back on after an app restart, because that would move pumps unattended. Turn it on by hand. |
| `temperature.cooldown_c` | `25.0` | The reactor is set to this temperature when each synthesis ends. |

**Target Runs.** Each campaign is a Target Run tagged `Run1`, `Run2`, … (one more
than the highest on disk). The tag is the start of every `recipe_id` (`Run3_r001`,
…) and so of every 2D file name, which makes each dataset traceable to its
conditions. **Abort** in the analyser ends the campaign; the next **Start
campaign** is a new Target Run.

**Notifications.** Set up Slack in Autonomous Watch before an overnight run:
[NOTIFICATIONS.md](NOTIFICATIONS.md).

## 4. Single-shot dry pass (always do this first)

1. Autonomous Synthesis → **Hardware test**, step 3 → **Collect now** as *sample*.
2. A `.raw` appears under `<project>/2D/SAXS/`.
3. Start the monitors (step 5) or run each app once by hand, and follow the shot:
   `2D/SAXS` → `1D/SAXS/Reduction` → `Averaged` → `Subtracted`.
4. The analyser fits the subtracted file and the **size is physical, not 10× off**
   (that confirms the q-unit path).

## 5. Start the pipeline monitors (they do not start by themselves)

1. **Reduction & Correction** → **Run & monitor** tab → switch on **Watch for new
   files** (checks `2D/` every 10 s by default).
2. **Visualisation & Average** → **Auto-Average** tab → set **Frames per average
   (N)** to the reactor's **Number of frames**, then **Start auto-averaging**. The
   field defaults to 30 and the reactor to 10 frames; with N larger than the frames
   per acquisition no average is ever written (the log warns).
3. **Background Subtraction** → **Auto-Subtract** tab → sample and background
   keywords, scale method, and the trim/rebin q-range (default 0.3 to 6.0 nm⁻¹,
   549 points) → **Start auto-subtraction**.
4. Optional: **Quality Gate** → **Start grading**.

## 6. Start the optimiser campaign (Autonomous Analyser, 5107)

1. **Setup** tab: check the watched **Folder** (`1D/SAXS/Subtracted`), set **Target
   diameter (nm)** and **Tolerance ± (nm)**, **Max PDI** and **Max experiments**,
   and confirm the parameter bounds match your chemistry.
2. **Start campaign.** The analyser fits each new subtracted profile and writes the
   next condition into `1D/SAXS/Conditions/`.
3. **Quality gate.** If the Quality Gate is running and putting profiles in
   `Subtracted/Good/`, the analyser fits only those. If `Good/` is empty or missing,
   it fits every subtracted profile, and a bad subtraction (wrong pairing, wrong
   background) still reaches the campaign. Low-confidence fits enter the optimiser
   as noisy observations rather than being dropped. Watch the first few conditions.

## 7. Launch the autonomous run (Autonomous Synthesis)

1. **Autonomous setup**, step 4 → **Run autonomously**. The reactor runs each
   condition file in `1D/SAXS/Conditions/`, oldest first. The campaign wrote the
   first one at **Start campaign**; you can also queue one by hand with **Add to
   queue**.
2. Watch the **first full cycle** before leaving. With `background_when: "before"`
   the order is:

   ```
   flush → background shot (clean capillary, tagged with the UPCOMING recipe_id)
        → arm → synthesis → sample shot
        → reduce → average → subtract → analyser fit
        → next condition queued → reactor runs it
   ```

   The sample and its background share one `recipe_id`, which is how Background
   Subtraction pairs them. After the first cycle the pre-synthesis flush is a short
   solvent rinse (`flush.blank_rinse_s`, 30 s), because the line is already clean.
   With `background_when: "after"` the blank is collected during the post-synthesis
   flush instead (arm → synthesis → sample shot → flush → background shot).

## 8. During the run and stopping

- **Pause:** turn **Run autonomously** off. The current condition finishes and
  flushes, then the reactor waits with the queue kept. This is when beamline
  settings (exposure, frames, trigger before end) can be changed; they apply when
  you turn it back on.
- **End the current run early:** **Stop → flush**.
- **Stop / EMERGENCY STOP** act on the pumps only and never interrupt an X-ray
  collection in progress. An E-stop also turns **Run autonomously** off: after
  clearing the fault, press **Reset**, then switch **Run autonomously** on again.
- **At the end:** stop the reactor app (from the hub) to idle the pumps, close the
  shutter and hand SPEC control back to beamline staff. After an E-stop the
  temperature is left as it was; cool it by hand (`csettemp <ambient>`).

---

## Won't-start checklist

- **Nothing reducing?** Reduction's **Watch for new files** is off,
  `data_directory` is not `<project>/2D`, or SPEC writes to a different `2D/SAXS`.
- **Nothing averaging?** **Frames per average (N)** is larger than the reactor's
  **Number of frames**, or the monitor is not started.
- **Nothing subtracting?** The monitor is not started, or the keywords are wrong.
- **Optimiser not advancing?** Campaign not started, the analyser and reactor are
  on different project folders (choose the folder again in the hub), or bad
  subtractions are feeding it.
- **Sizes 10× off?** A q-unit problem. The analyser converts Å⁻¹ to nm⁻¹ itself,
  but confirm it on the dry pass.
- **Autonomous Watch** (5110) names the stage that stalled.
