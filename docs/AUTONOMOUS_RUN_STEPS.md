# Starting an autonomous measurement — operator steps

Follow in order. Fuller rationale + risks are in
`docs/audits/AUTONOMOUS_RUN_READINESS.md`. The loop is:
reactor → SPEC collect → reduction → averaging → background subtraction →
analyzer/optimizer → next recipe. Several stages are **manual to start** — that's
what most of these steps are.

---

## 0. Hardware / beamline (before software)

1. Pumps powered, USB connected, lines primed, no leaks; pumps tared.
2. Flow cell mounted at the sample position; beam ready; hutch searched.
3. bServer (`pySSRL-bServer`) running on the control PC.
4. Temperature controller in remote/SPEC mode.

## 1. Launch the platform

1. Start the hub:  `python hub/app.py`  → open **http://localhost:5000**.
2. **Pick the project folder** in the hub (the experiment root that holds `2D/`,
   `poni/`, `config.yml`). This pushes the folder to every app.
3. Start each app from the hub: **Reduction, Viewer, Background, Quality,
   Analyzer, Flow Synthesis (reactor)** (Assistant optional).
4. Re-select the project folder once more in the hub so every just-started app
   receives it. Open each app and confirm it shows the right folder.

## 2. Check the project config

1. `<project_root>/config.yml` → `data_directory` = `<project_root>/2D`
   (this is what reduction scans; if it points elsewhere the loop won't connect).
2. `poni/` has the calibration `.poni` + mask files; `config.yml` names them.

## 3. Reactor / beamline setup (Flow Synthesis app, :5007)

1. Backend pill → **Real** (covers pumps + beamline).
2. Confirm the log shows `📁 SPEC data_dir → /msd_data/…` matching where SPEC saves
   (the `2D/SAXS` under your project, via the `hub_path_map`).
3. Live plot reads temperature / bstop / I₀ (EPICS if configured, else `ct`).
4. **Timing:** arming mode (temperature or timed) + wait, run duration, flush
   rate/duration. Flush pump = **ode_dilution** (current workaround).
5. **Data collection card:** exposure, frames, sample keyword (`sample`),
   background keyword (`bkg`), trigger-before-end (`spec_lead`) longer than
   frames×exposure. `data_dir` should already be filled from the hub.
6. **Pump limits & calibration:** per-pump min/max and `cal ×` (water→fluid
   factor) set if you have them.

## 4. Single-shot dry pass (do this before autonomous!)

1. Reactor → **📷 Collect now** (role = sample).
2. Confirm a `.raw` appears under `<project>/2D/SAXS/`.
3. Start reduction/averaging/subtraction monitors (next step) OR run them once
   manually, and confirm the shot flows: `2D/SAXS` → `1D/SAXS/Reduction` →
   `Averaged` → `Subtracted`.
4. Open the Analyzer and confirm it fits the subtracted file and the **size is
   physical (not 10× off)** — this proves the q-unit path is correct.

## 5. Start the pipeline monitors (these do NOT auto-start)

1. **Reduction app** → *Start auto-reduction* (watches `2D` for new `.raw`).
2. **Viewer app** → *Start auto-average* (Reduction → Averaged).
3. **Background app** → *Start auto-subtraction* (Averaged → Subtracted).
   - Confirm sample/background keywords + scale method; ML truncate/rebin panel
     as needed (default 0.03–0.6, 549 pts).
4. (Optional) **Quality app** for review — not required by the loop.

## 6. Start the optimizer campaign (Analyzer app, :5008)

1. Set the **target size** (nm) and **tolerance**, and confirm the parameter
   **bounds** match your chemistry.
2. **Start campaign.** The analyzer now watches `1D/SAXS/Subtracted`, fits each new
   profile, and writes the next recipe to `1D/SAXS/Conditions`.

## 7. Launch the autonomous run (reactor)

1. Turn **Auto-run ON** so the reactor pulls each new condition from
   `1D/SAXS/Conditions` and runs it.
2. Seed the loop: either submit one starting recipe manually, or let the campaign
   propose the first condition.
3. Watch the **first full cycle** end-to-end before leaving it:
   arm → run → sample shot → flush → background shot → reduce → average →
   subtract → analyzer fit → next condition queued → reactor runs it.

## 8. During / stopping

- **Stop / E-stop** act on pumps only and never interrupt an in-progress X-ray
  collection.
- Turn **Auto-run OFF** to stop after the current condition; **Abort** to end the
  current run into flush.
- At the end: the reactor cools to room temp on run end (`cooldown_c`); close the
  reactor app to release SPEC remote control for beamline staff.

---

### Quick "won't-start" checklist
- Nothing reducing? → reduction monitor not started, or `data_directory` ≠ `<root>/2D`,
  or SPEC writing to a different `2D/SAXS` than reduction scans.
- Nothing subtracting? → viewer/background monitors not started, or keywords wrong.
- Optimizer not advancing? → campaign not started, or analyzer/reactor on different
  project roots (re-select the hub folder), or bad subtractions feeding it.
- Sizes look 10× off? → q-unit; the analyzer auto-converts Å⁻¹, but confirm on the
  dry pass (step 4).
