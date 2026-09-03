# Starting an autonomous measurement — operator steps

Follow in order. The loop is:
reactor → SPEC collect → reduction → averaging → background subtraction →
analyzer/optimizer → next recipe. Several stages are **manual to start** — that's
what most of these steps are.

Every hop is driven by **polling monitors/watchers**. The reactor and analyzer
watchers auto-start; the reduction, average, and background monitors and the
optimizer campaign do not. If any one is off, the loop stalls at that stage.

---

## 0. Hardware / beamline (before software)

1. Pumps powered, USB connected, lines primed, no leaks; pumps tared.
2. Flow cell mounted at the sample position; beam ready; hutch searched.
3. bServer (`pySSRL-bServer`) running on the control PC.
4. Temperature controller in remote/SPEC mode.

## 1. Launch the platform

1. Start the hub with a launcher — `./start_platform.sh` (macOS/Linux),
   `start_platform.ps1` or `start_platform.bat` (Windows) → open
   **http://localhost:5100**. The launchers source `.env` and resolve the AI
   token; `python hub/app.py` starts the hub but leaves `.env` unread, so the
   assistant and Slack/email notifications come up unconfigured.
2. **Pick the project folder** in the hub (the experiment root that holds `2D/`,
   `poni/`, `config.yml`). This pushes the folder to every app.
3. Start each app from the hub: **Calibration, Reduction, Visualisation & Average, Background,
   Quality, Analyzer, Flow Synthesis (reactor)** (Assistant optional).
   Calibration (:5101) is what generates the `.poni` files step 2 checks for —
   start it first if the calibration has not been done yet.
4. Open each app and confirm it shows the right folder. The hub injects
   `SWAXS_PROJECT` into every subprocess at launch (`hub/app.py:292`) and each
   app reads it on boot, so a re-select is not normally needed — but re-select
   if any app shows a blank or wrong root (this happens when the path was not
   yet mounted at launch; background and analysis both guard on `is_dir()`).
   The reactor and analyzer fall back to the repo directory when the root is
   blank, so check those two in particular.

## 2. Check the project config

1. `<project_root>/config.yml` → `data_directory` = `<project_root>/2D`
   (this is what reduction scans; if it points elsewhere the loop won't connect).
   Reduction derives its output root from `data_directory.parent`, so a leftover
   `config.yml` pointing elsewhere silently relocates all the 1D folders away
   from the other apps.
2. `poni/` has the calibration `.poni` + mask files; `config.yml` names them.

## 3. Reactor / beamline setup (Flow Synthesis app, :5108)

1. Backend pill → **Real** (covers pumps + beamline).
2. Confirm the log shows `📁 SPEC data_dir → /msd_data/…` matching where SPEC saves
   (the `2D/SAXS` under your project, via the `hub_path_map`). If the
   `hub_path_map` prefixes are wrong the translation silently fails and
   `spec.data_dir` stays at the hardcoded `/msd_data/.../Auto_Test` — SPEC then
   writes where nothing is reduced.
3. Live plot reads temperature / bstop / I₀ (EPICS if configured, else `ct`).
4. **Timing:** arming mode (temperature or timed) + wait, run duration, flush
   rate/duration. Flush pump = **ode_dilution** (current workaround).
5. **Data collection card:** exposure, frames, sample keyword (`sample`),
   background keyword (`bkg`), trigger-before-end (`spec_lead`) longer than
   frames×exposure. `data_dir` should already be filled from the hub.
6. **Pump limits & calibration:** per-pump min/max and `cal ×` (water→fluid
   factor) set if you have them.

### `reactor/config.yml` settings that change how the run behaves

These are not in the UI. Check them before the first cycle.

| Key | Shipped default | Why you care |
|---|---|---|
| `spec.read_source` (`:300`) | `"spec"` | With `"spec"` a collection holds the SPEC lock for its whole duration and temperature / I₀ / bstop reads **stop** — with `exposure_s: 10` and `frames: 10` that is ~100 s per acquisition with no fresh temperature, so the over-temperature interlock has nothing to compare against for that window. The app warns once per run instead of raising a sensor fault. Set `"epics"` (verify PVs with `tools/beamline_epics_test.py`) to keep reading during a collection. |
| `spec.background_when` (`:284`) | `"before"` | Sets the physical order of the cycle — see step 7. `"after"` is the legacy order. |
| `reactor.resume_auto_run` (`:379`) | `false` | Deliberately false: auto-run does **not** resume after an app restart, because resuming moves pumps unattended. After any restart you must turn Auto-run back on by hand. |

**Run notifications** (Slack thread per recipe, email) are configured
separately — see `docs/NOTIFICATIONS.md`. Set them up before an overnight run;
the analyzer's fit for each recipe is posted into that recipe's thread, with the
QC plot attached when the fit is suspect.

## 4. Single-shot dry pass (do this before autonomous!)

1. Reactor → **📷 Collect now** (role = sample).
2. Confirm a `.raw` appears under `<project>/2D/SAXS/`.
3. Start reduction/averaging/subtraction monitors (next step) OR run them once
   manually, and confirm the shot flows: `2D/SAXS` → `1D/SAXS/Reduction` →
   `Averaged` → `Subtracted`.
4. Open the Analyzer and confirm it fits the subtracted file and the **size is
   physical (not 10× off)** — this proves the q-unit path is correct.

## 5. Start the pipeline monitors (these do NOT auto-start)

1. **Reduction app** → *Run & Monitor* tab → the **Watch for new files** toggle
   (watches `2D` for new `.raw`; the interval box next to it defaults to 10 s).
2. **Visualisation & Average app** → *▶ Start auto-averaging* (Reduction → Averaged).
3. **Background app** → *▶ Start auto-subtraction* (Averaged → Subtracted).
   - Confirm sample/background keywords + scale method; ML truncate/rebin panel
     as needed (default 0.03–0.6, 549 pts).
4. (Optional) **Quality app** for review — not required by the loop.

## 6. Start the optimizer campaign (Analyzer app, :5107)

1. Set the **target size** (nm) and **tolerance**, and confirm the parameter
   **bounds** match your chemistry.
2. **Start campaign.** The analyzer now watches `1D/SAXS/Subtracted`, fits each new
   profile, and writes the next recipe to `1D/SAXS/Conditions`.
3. **There is no quality gate in front of the optimizer.** The analyzer fits
   every subtracted file and feeds the campaign regardless of the Quality app's
   verdict; a low-confidence fit is not discarded, it enters the surrogate as a
   high-noise observation (by design — see
   `docs/PARAMETER_SPACE_AND_CONVERGENCE.md`). Consequence for you: a *bad
   subtraction* (wrong pairing, wrong background) still gets told to the
   campaign. Eyeball the analyzer feed for the first few conditions. The
   Quality app's `Good/` sorting is not in the automatic path.

## 7. Launch the autonomous run (reactor)

1. Turn **Auto-run ON** so the reactor pulls each new condition from
   `1D/SAXS/Conditions` and runs it.
2. Seed the loop: either submit one starting recipe manually, or let the campaign
   propose the first condition.
3. Watch the **first full cycle** end-to-end before leaving it. With the shipped
   `background_when: "before"` (`reactor/config.yml:284`) the physical order is:

   ```
   flush → background shot (clean capillary, tagged with the UPCOMING recipe_id)
        → arm → run → sample shot
        → reduce → average → subtract → analyzer fit
        → next condition queued → reactor runs it
   ```

   So the blank is measured **before** the synthesis, not after it. The
   background is therefore already on disk when the sample frames land and
   subtraction can start immediately. The pair shares one `recipe_id`, which is
   how the background app matches them.

   With the legacy `background_when: "after"` the blank is collected during the
   post-synthesis flush and tagged with the run that just finished — that gives
   the older order (arm → run → sample shot → flush → background shot). If what
   you see does not match the setting you have, something misfired.

## 8. During / stopping

- **Stop / E-stop** act on pumps only and never interrupt an in-progress X-ray
  collection.
- Turn **Auto-run OFF** to stop after the current condition; **Abort** to end the
  current run into flush.
- At the end: the reactor cools to room temp on run end (`cooldown_c`); close the
  reactor app to release SPEC remote control for beamline staff.

---

## Go / no-go checklist

Condensed from the pre-beamtime readiness review. Run through it at the start of
every beamtime, in this order.

1. Hub: select the project folder; confirm **every started app** shows it
   (reactor and analyzer especially — they fall back to the repo directory when
   the root is blank).
2. `<root>/config.yml` `data_directory` = `<root>/2D`; `poni/` present with the
   `.poni` and mask files `config.yml` names.
3. Reactor: backend = **Real**; the `📁 SPEC data_dir → …` log line shows the
   right `/msd_data/...` path; EPICS reads live (or `read_source: "spec"` with
   `ct`, accepting the blind window in §3); flush pump + per-pump calibration
   set.
4. One **📷 Collect now** → the `.raw` lands in `2D/SAXS` and reduction
   auto-processes it → averaged → subtracted → analyzer produces a fit. This is
   the full single-shot dry pass (§4).
5. Start the three monitors: reduction, average, background. Start the analyzer
   campaign with the target size and tolerance set.
6. Confirm the first subtracted file's size looks physical, not 10× off. The
   analyzer detects the header q-unit and converts Å⁻¹ → nm⁻¹ itself, but
   confirm it on the dry pass rather than assuming.
7. Launch the autonomous run; watch the first full cycle end-to-end before
   leaving it.

### Known alignment points — no action needed

- `recipe_id` survives reactor → reduction (`{stem}_SAXS.dat`) → average
  averaging (grouped by `{recipe_id}_{role}`) → background pairing (by
  `recipe_id`, nearest-index fallback) → analyzer → optimizer match.
- The optimizer→reactor file contract matches, and the folders align: the
  analyzer writes `1D/SAXS/Conditions`, the reactor watches it.
- Safety: E-stop acts on pumps only; no command can interrupt an in-progress
  collection; EPICS reads are independent of SPEC; remote control is released
  on exit; per-pump flow calibration, flow-OK and volume-limit checks all
  apply; the reactor cools down at run end.

---

### Quick "won't-start" checklist
- Nothing reducing? → reduction monitor not started, or `data_directory` ≠ `<root>/2D`,
  or SPEC writing to a different `2D/SAXS` than reduction scans.
- Nothing subtracting? → average/background monitors not started, or keywords wrong.
- Optimizer not advancing? → campaign not started, or analyzer/reactor on different
  project roots (re-select the hub folder), or bad subtractions feeding it.
- Sizes look 10× off? → q-unit; the analyzer auto-converts Å⁻¹, but confirm on the
  dry pass (step 4).
