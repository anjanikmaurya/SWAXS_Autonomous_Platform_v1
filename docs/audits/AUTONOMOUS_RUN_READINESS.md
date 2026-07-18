# Autonomous run — readiness audit (pre full-synthesis)

End-to-end audit of the closed loop before a full autonomous synthesis run.
Test suite: **133 passing**. Two blocker/bug fixes were applied tonight (below);
the rest are operator checklist items and flagged risks.

## The loop (what must run)

```
reactor (arm→run→flush) ──SPEC collect──► <data_dir>/2D/SAXS/<recipe_id>_{sample,bkg}_*.raw
   ──REDUCTION (auto monitor)──► 1D/SAXS/Reduction/*.dat
   ──VIEWER average (auto monitor)──► 1D/SAXS/Averaged
   ──BACKGROUND subtract (auto monitor)──► 1D/SAXS/Subtracted  (paired by recipe_id)
   ──ANALYZER (auto watcher) fit size/PDI/phase──► OPTIMIZER proposes next
   ──write 1D/SAXS/Conditions/<rid>.txt──► reactor folder-watcher ingests → next run
```
Every hop is driven by **polling monitors/watchers**, not the event bus (the bus is
telemetry only). Reactor and analyzer watchers auto-start; the reduction, viewer,
and background monitors and the optimizer campaign are **started manually**.

## ✅ Fixed tonight

1. **Unit mismatch (was a silent results-corrupter).** Background truncates
   subtracted files to Å⁻¹ (`q_A-1`) for the ML model, but the analyzer/optimizer
   work in nm⁻¹ (radius in nm, campaign target in nm). The analyzer now detects the
   header unit and converts Å⁻¹→nm⁻¹ before fitting (`analyzer/app.py:_q_is_angstrom`,
   `_analyze_file`). Sizes/targets are now consistent regardless of the truncation
   unit. (test: `tests/test_analyzer_units.py`)
2. **Analyzer double-reading / quality copies.** The watcher was recursive
   (`rglob`) and re-analyzed the Quality app's `Good/` & `NeedsReview/` copies. Now
   non-recursive (`glob`) — it analyzes only the flat `Subtracted/*.dat` once.

## ⚠ BLOCKERS — operator must do before/at run start

1. **Start the three monitors + the campaign** (they don't auto-start):
   - Reduction app → **Start auto-reduction** (watches `2D` for new `.raw`).
   - Viewer app → **Start auto-average**.
   - Background app → **Start auto-subtraction** (watches `Averaged`).
   - Analyzer app → **Start campaign** (with the correct size target + tolerance).
   If any one is off, the loop stalls at that stage.
2. **Confirm SPEC writes where reduction scans.** A fired collection must land under
   `<reduction data_directory>/**/SAXS/*.raw`. Reduction's `data_directory` (project
   `config.yml`) must be `<project_root>/2D`, and `spec.data_dir` (Windows→Linux via
   `hub_path_map`) must resolve to that **same physical** `2D/SAXS` folder. Verify
   with one **📷 Collect now** → the `.raw` appears in the reduction folder AND
   reduction picks it up.
3. **Confirm the hub pushed the project folder to every app** (reactor + analyzer
   especially — they fall back to the repo dir if the root is blank). Re-select the
   folder in the hub after all apps are started, and check each app shows it.

## ⚠ RISKS — decide/watch

- **No quality gate before the optimizer.** The analyzer fits every subtracted file
  and feeds the optimizer **regardless of fit confidence or QC verdict** — a bad
  subtraction can poison the campaign. Options for tomorrow: (a) eyeball the analyzer
  feed early on; (b) I can add a `min_confidence` gate before `_feed_campaign` if you
  want it enforced. The Quality app's `Good/` sorting is **not** in the auto path.
- **`data_dir` fallback.** If the `hub_path_map` prefix is wrong, `_sync_data_dir_from_hub`
  can't translate and leaves `spec.data_dir` at the hardcoded
  `/msd_data/.../Auto_Test`, which may not match the reduction root → SPEC writes
  where nothing is reduced. Confirm the log shows `📁 SPEC data_dir → …` with the
  right path when you select the folder.
- **Stale project `config.yml`.** Reduction derives its root from `data_directory.parent`.
  A leftover `config.yml` pointing elsewhere silently relocates the 1D outputs away
  from the other apps. Make sure `<project_root>/config.yml` `data_directory` = `<root>/2D`.
- **`set_project` dropped on a not-yet-mounted path** (background/analysis guard on
  `is_dir()`). If `X:` is slow to mount, re-select the folder once it's available.

## ✅ Verified aligned (no action)

- `recipe_id` survives reactor → reduction (`{stem}_SAXS.dat`) → viewer averaging
  (grouped by `{recipe_id}_{role}`) → background pairing (by recipe_id, nearest-index
  fallback) → analyzer→optimizer match (`match_recipe_id`).
- Optimizer→reactor file contract matches (`to_param_file` ↔ `parse_param_file`),
  and folders align: analyzer writes `1D/SAXS/Conditions`, reactor watches it.
- Reactor/beamline safety (all test-backed): E-stop = pumps only; hard SPEC guard
  (no command interrupts a collection); EPICS reads independent of SPEC; remote
  control released on exit; flush pump = ode_dilution (workaround); per-pump flow
  calibration (power-law/linear) + flow-OK + volume-limit checks; cooldown at run end.
- Windows paths: code is pathlib-clean; the only Windows exposure is the
  `X:` ↔ `/msd_data` mount + `hub_path_map` correctness (item B2 above).

## Go / no-go checklist (tomorrow)

1. Hub: select the project folder; confirm all 8 apps show it.
2. `<root>/config.yml` `data_directory` = `<root>/2D`; `poni` present.
3. Reactor: backend = **Real**; `spec.data_dir` log shows the right `/msd_data/...`;
   EPICS reads live (or `read_source: spec` with `ct`); flush pump + calibration set.
4. One **📷 Collect now** → `.raw` lands in `2D/SAXS` and reduction auto-processes it
   → averaged → subtracted → analyzer produces a fit. (Full single-shot dry pass.)
5. Start: reduction, viewer, background monitors; analyzer campaign (target set).
6. Confirm the first subtracted file's size looks physical (not 10× off) — sanity on
   the unit fix.
7. Launch the autonomous run; watch the first full cycle end-to-end before leaving it.
