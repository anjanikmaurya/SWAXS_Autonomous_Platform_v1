# Open defects — the platform's single register

> **Status, re-checked against the code on 2026-10-14.** Open: N5, N6 (mostly
> fixed), N8 to N16, O3, O4, O7, O9, O10, O15, O18, C4, C5, U3, U5, U6, D1, D4,
> W11, W12, W14, W15, W17 to W23, assistant A9, analyzer L1 and L2, analyser AN2 to AN4, AN10 to AN14 (2026-10-25; AN1, AN5 to AN9 fixed), R12
> (reactor audit), and the shared-folder pairing residual of the platform quick audit (Q1 to Q37 fixed 2026-10-14). Fixed since the last pass: O12 (SQLite store), O14, O16,
> W10, assistant A1 to A3. The manifest rows (O3, O4, O12,
> O18) describe the current SQLite store (`src/manifest_store.py`).

**This is the one place open defects are tracked.** Ten point-in-time audits
(June–July 2026) were consolidated into this file; they remain in git history if
you need the original narratives. `git log --diff-filter=D -- docs/audits/`.

Numbering is historical and gaps mean **fixed** — an `N`, `O`, `C`, `U` or `D`
number that is absent has been resolved, and older documents that cite it still
resolve here. Prefixes: `N` = the July code audit, `O` = the platform audit,
`C`/`U` = the subtraction audit, `D` = the June pipeline audit.

Sections below: **fixed this session** (kept for the three rationale blocks that
are recorded nowhere else — the temperature-interlock trade-off, the deliberate
absence of mock time compression, and the frames-vs-batch deadlock), then the
open register grouped by owner.

---

# Fixed — history, kept for the rationale


Brief: *"audit the whole hub and apps for any redundancy, code brokenness, and
any logical errors."*

Method: a `ruff` sweep for undefined names and shipped-code faults, an AST scan
for helpers duplicated across apps, two parallel deep reads of the app files, and
— for every candidate — **a reproduction before any fix**. Findings that could not
be reproduced were dropped. All nine apps were then booted through the real hub
endpoints to confirm they start, report healthy and stop cleanly.

**483 tests pass** (was 444; 39 added).

Every defect below shares one shape: *code that looks correct, runs without
complaint, and quietly does the wrong thing.* None of them raised an error an
operator would ever have seen.

---

## CRITICAL

### 1. A control-loop fault skipped the emergency stop and killed the supervisor

`src/reactor/controller.py` — no `logger` was ever imported, and the fault handler
used it **before** the E-stop:

```python
except Exception as exc:
    ...
    logger.exception("control loop fault")   # NameError
    try:
        self.estop()                         # never reached
```

Reproduced: one faulting tick → `estop` called **0 times**, control thread
**dead**, `self._alive` still `True`.

The comment directly above that handler reads *"NOTHING may escape this loop. If
an unhandled exception killed the thread, `_safety_check()` would stop running
while reagent pumps are still commanded."* That is exactly what happened. A single
transient glitch — a serial hiccup, a stale temperature read — left reagent pumps
commanded and the heater on with **no over-temperature, over-pressure, volume or
flow-fault supervision and no run deadline**, while the controller reported itself
healthy.

**Fixed** — `logger` defined; the E-stop now runs **first**, in its own guard, and
nothing downstream can skip it; if the loop ever does exit, it E-stops and marks
itself not-supervising on the way out. `status()` gained `supervising`
(thread liveness, not a flag), `loop_faults` and `last_fault`. A test asserts the
*ordering*, because ordering is the whole fix.

### 2. One numpy value permanently disabled an app's event bus

`src/events.py` `publish()` wrapped `json.dumps` and `ws.send` in one `try`, and
treated any failure as a transport failure — dropping the socket.
`average/app.py:488` passed `n_files = mask.sum()`, which is `np.int64`, not `int`.

Reproduced: first publish → `TypeError`, `_connected = False`, `_ws = None`. The
socket was **still open**, so `run_forever` never returned and the reconnect loop
never fired. Every later event — including well-formed ones — was silently
dropped for the rest of the process.

The reactor advances the autonomous loop on `file.averaged`. So one click of
*Average* stalled the campaign, with nothing but a single `logging.WARNING` to
show for it.

**Fixed** — encoding is no longer confused with transport: a bad payload drops the
*event* and keeps the connection, and `default=_json_safe` coerces numpy scalars
and arrays so this cannot recur. Separately, `mask.sum()` was also the **wrong
number** (positive q-points, not frames); the average app now reports the real count.

### 3. Reduction alone never resumed after a restart

`reduction/app.py:850` referenced `_project_root`, which does not exist in that
module. Every call to `_state_root()` raised `NameError`, and **both** callers
swallowed it:

* `_persist_monitor_state` → `save_monitor()` was never reached, so the state was
  never written;
* `_boot_resume_monitor` → the resume attempt died before it began.

The restart-recovery work was therefore inert for reduction while working in every
other app. Observable state after a 03:00 restart: average-app averaging, background
subtracting, **reduction dead**, no new `.dat` files, all hub cards green.

**Fixed** — read the env var the hub sets, plus whatever `set_project()` recorded.

---

## HIGH

### 4. A dead monitor thread reported a healthy monitor — and blocked its own restart

All four processing apps kept a bare `_monitoring = True` that only the worker
loop itself cleared. Reproduced by killing a live grader thread: status still said
`monitoring: true`, and `/api/monitor/start` refused with **"Already
monitoring"** — so the app could not be recovered from the UI at all and nothing
was processed for the rest of the night. The same green-card-doing-nothing failure
the restart-resume work exists to prevent, reached by a different route.

**Fixed** — one shared `src/runstate.monitor_alive(flag, thread)` used by both the
status route and the already-running guard in reduction, average, background and
quality, so the two can never disagree. Taking over from a dead worker is
announced rather than done silently.

### 5. The quality gate's LLM adjudication was thrown away one line later

`quality/app.py` set `rec["verdict"] = adj["verdict"]` and then immediately
`rec["verdict"] = _effective_verdict(rec)` — and `_effective_verdict` never looked
at the LLM answer. Every borderline profile was sorted by the plain rule score,
while the manifest recorded the verdict with `source="ai"`. One paid API call per
borderline profile bought a cosmetic note and nothing else.

**Fixed** — `_effective_verdict` now honours, in order: a human override, the LLM
adjudication, then the threshold.

### 6. The Guinier R² quality gate could never fire

`src/analysis/core.py` read `result.get("r2")`, but `guinier_fit` returns `"R2"`.
Reproduced: a fit with **R² = 0.42** was reported `PASS` with no warnings.

**Fixed** — read `"R2"` (accepting both spellings). The same fit now returns
`WARN` with `Guinier R² = 0.42 (<0.99)`.

### 7. Fit-trust flags were dropped before being saved

`src/analysis/io.py::_scalar_results` did `if isinstance(v, bool): continue`,
which silently discarded the only two fields that say whether a fit can be
believed: `converged` from the optimiser and `at_bounds` from the parameter check.
A model fit whose radius railed to its bound was persisted as a clean number,
indistinguishable from a good one, in the saved JSON, the `.dat` annotation and
the manifest.

**Fixed** — bools are kept; `at_bounds` / `warnings` / `flags` are flattened to a
readable string. Arrays are still excluded.

### 8. Batch analysis could not address the Quality Gate's folders

`analysis/app.py` built stage paths with `stage.capitalize()`, which lowercases
the rest: `"NeedsReview"` → `"Needsreview"`, a folder that does not exist, so the
file list came back **empty with no error**. And the gate's accepted set lives in
`Subtracted/Good`, which that scheme could not express at all — so an unattended
batch ran over the unfiltered `Subtracted/`, fitting rejected profiles and writing
`Rg`/radius values into the record.

**Fixed** — an explicit alias → folder map (`good → Subtracted/Good`,
`needs_review`/`NeedsReview` → `Subtracted/NeedsReview`, …). Verified for all five
stages plus an unknown one.

### 9. A test whose reference value depended on import order

`tests/test_reduction_corrections.py` installed its `xraydb` stub only
`if "xraydb" not in sys.modules`. When a sibling test had already imported the
real library, the stub was skipped, `material_mu` returned the true coefficient,
and the hard-coded µ = 10 cm⁻¹ expectations failed with a 3.2× mismatch that reads
exactly like a science bug in the product. The dangerous half is the mirror image:
such a test can also **pass while the code under test is wrong**.

**Fixed** — an autouse fixture pins `core`'s view of `material_mu` for every test
in the module, whichever xraydb is loaded, and restores it afterwards.

### 10. `tools/notify_test.py` was broken at five call sites

`SlackNotifier` was used but never imported (the module imports `slack as S`).
Every path through the Slack test tool raised `NameError` on first use.

**Fixed** — import added.

### 11. The temperature interlock cried "sensor fault" during its own acquisition

Reported live from the beamline:

> 🛑 SAFETY: temperature reading is STALE (15s since the last successful read) —
> the over-temperature interlock cannot protect you. Check the SPEC/EPICS
> temperature source (spec.temp_counter / spec.epics_pvs).

Nothing was wrong with the counter. With `read_source: "spec"`, a collection holds
the SPEC lock for its entire duration and `read_state()` is deliberately
non-blocking, so it returns `{}` for the whole acquisition. Reproduced by holding
the lock exactly as `collect()` does: the reading froze at its last value and
`stale` flipped at 15 s.

With the shipped `exposure_s: 10 × frames: 10` an acquisition is **100 seconds**,
so the alarm fired on **every single acquisition** and stayed up for ~85 s of it —
sending the operator to inspect hardware that was working perfectly, and training
them to ignore the one message that should never be ignored.

**Fixed** — three parts:

* `TempController.polling_paused` asks the beamline whether it is collecting; a
  pause we caused ourselves is no longer counted as staleness.
* The controller now emits a **once-per-run informational note** naming the real
  trade-off and the real remedy: `spec.read_source: "epics"` (reads the live
  monitors directly and keeps working during a collection) or
  `spec.read_during_collect: true`.
* The genuine fault message is unchanged in severity but now says *"and no
  acquisition is running"*, so the two causes cannot be confused. A dead source
  with nothing collecting still trips it — there is a test for each.

The trade-off is also documented at `read_source` in `reactor/config.yml`, since
the default leaves the interlock without fresh data for the length of every
acquisition.

### 12. Mock ran on the real beamline clock, and averaging never happened

Asked as *"why in mock does everything seem very slow and not follow real time
for the synthesis and flush? also reduction, averaging and analyser seem very
slow."* Measured, not guessed. Two separate causes, neither of them slow code.

**(a) Mock was following real time — exactly.** The shipped config is 120 s
arming + 600 s synthesis + 1200 s flush = **1920 s = 32 minutes per recipe**, so a
25-run campaign is **13.3 hours**, identical in mock and on the beamline. Nothing
was broken; the reactor simply has no time compression.

A mock-only `run.mock_time_scale` was added and then **removed at the operator's
request**: a mock rehearsal must be timed exactly like the beamline run it stands
in for, so a 60 s synthesis takes 60 s everywhere. Short tests come from short
durations, not from a different clock. A test now keeps compression out of the
controller and the config.

**What the shortened durations exposed instead:** the live run settings (arming
mode/wait, synthesis duration, flush rate/duration) that the app writes were
**in-memory only**. Set "reach temp + 60 s synthesis + 60 s flush" in the UI,
restart the app, and the reactor silently reverted to `reactor/config.yml` —
600 s and 1200 s — with a resumed autonomous campaign using the long defaults and
nothing to say so. **Fixed**: they are persisted through `src/runstate` and
re-applied on boot *before* auto-run can start a recipe, with a log line naming
what was restored.

**(b) Averaging was not slow — it was deadlocked, silently, forever.** Loop frames
group per `{recipe_id}_{role}`, so frames from different recipes never combine. The
reactor ships `spec.frames: 10`; the Visualisation & Average UI ships `frames_per_average: 30`. The
loop condition is `while (len(grp) - consumed) >= n_per_batch`, and 10 >= 30 is
never true — **no average is ever written, for any recipe.** No averaged file means
no subtracted profile, no fit and no next recipe: the entire autonomous loop stops.
Reproduced: `batches: 0`, status green, and not one log line after
"Auto-averaging started".

**Fixed** — the mismatch is detected at monitor start and reported as an **error**
naming the remedy ("Set frames/batch to 10, or raise spec.frames"), and each group
now reports `12/30 frames — waiting for 18 more`, once per change. Silence was half
the bug.

**(c) The rest of the "slowness" is poll latency, not compute.** Measured end to
end for one acquisition: 264 s from the last frame to a fitted result, of which
**99 % is waiting** — 200 s for a full batch, 50 s of poll intervals and the
2-poll stability gate — and **1 % is CPU**. Per-item costs: reduction ~0.15 s per
frame, the average app's folder re-read ~1 ms per file, one analyzer fit 246 ms. Nothing
there is slow; the chain is dominated by four independent 10 s pollers in series
plus the batch wait. Reducing that means driving the stages off the event bus
instead of polling — recorded as **N16** below rather than changed here, because it
alters the timing of every stage at once.

### 13. Guinier, Porod and the nanoparticle fit computed sigma and then ignored it

Every stage before fitting — reduction, averaging, background subtraction —
propagates a real error bar (see `docs/ERROR_PROPAGATION.md`). Three fits
downstream never looked at it: `guinier_fit` and `porod_fit`
(`src/analysis/core.py`) accepted a `sigma` argument and had zero references
to it in the body (plain `scipy.stats.linregress`); the nanoparticle
form-factor fit (`src/analysis/nanoparticle.py`) didn't accept `sigma` at
all. Every point counted equally regardless of how precisely it was
measured — including in the fit that feeds the autonomous optimizer's
confidence score.

Also found while wiring this up: `analyze_profile` passed `sigma` to
`guinier_estimate` **unmasked and unsorted** relative to the `q`/`I` arrays
it had already cleaned and sorted — harmless only because `guinier_estimate`
ignored `sigma` too. Fixing the weighting without fixing the alignment would
have applied each point's error bar to the wrong point.

**Fixed** — both fits now weight by `1/sigma(ln I)²` (`1/sigma(log10 I)²`
for the nanoparticle fit), the standard inverse-variance treatment (Sedlak,
Bruetzel & Lipfert 2017; see `docs/ERROR_PROPAGATION.md` §1). `sigma=None`
or an all-invalid `sigma` falls back to exactly the prior unweighted
behaviour — verified bit-identical against the old code path. The alignment
bug is fixed alongside it. Verified on synthetic heteroscedastic data against
known ground truth: the weighted Guinier fit recovered a true Rg with 0.02 nm
error vs 0.55 nm unweighted; the weighted nanoparticle fit recovered a true
8.0 nm radius to within 0.0015 nm vs 0.018 nm unweighted. The demo-pipeline
golden reference was regenerated (`tests/fixtures/demo_pipeline/golden/`) —
its Guinier numbers changed because the auto-range refinement now correctly
narrows a too-broad initial q-range once weighting is applied; this was
inspected and is the fit behaving more correctly, not a regression.

Still open, not addressed by this fix: `peak_fit` (`core.py`) reads `sigma`
only once; `pair_distance_ift` and `sasmodels_fit` already used it fully.

---

## Analyser quick audit, 2026-10-25 (AN1 to AN14; AN1 and AN5 to AN9 fixed)

Write-up with evidence, fixes and tests: [ANALYZER_QUICK_AUDIT_2026_10.md](ANALYZER_QUICK_AUDIT_2026_10.md). AN2 is O7 (below), restated with the current line numbers. AN1, AN5 and AN7 were fixed the same day: the fitter is restored from the campaign on resume and Continue (paused, or Continue refused, if it is not available), a fitter change is refused while a campaign runs, and Continue keeps the frozen fitter knowledge; tests in `tests/test_analyzer_campaign_controls.py`. AN6, AN8 and AN9 were fixed next: frozen fitter snapshots are written only at Start and pruned (those a campaign record names are kept, plus the newest 20); the AI fitter cache keeps at most 5,000 entries and 30 days (`SWAXS_FITTER_CACHE_MAX`), pruned at most hourly; dated trace files are kept 90 days (`SWAXS_TRACES_KEEP_DAYS`), pruned at most daily; tests in `tests/test_fit_knowledge.py`.

| ID | Severity | Area | Finding (evidence) | Fix | Test |
|---|---|---|---|---|---|
| AN1 | ~~HIGH~~ **FIXED 2026-10-25** | Accuracy, days of running | **A restart mid-campaign switches the fitter to the default.** `_active_fitter` starts as `fitting.DEFAULT_ID` (`ls_sphere`, app.py:110) and is only set by `POST /api/methods` (app.py:2307). `_restore_campaign` and Continue restore the optimiser from `meta["optimiser"]` but never `meta["fitter"]`. A campaign started with Catalogue + BIC or AI-assisted structure carries on after a hub restart with the sphere-only fitter: non-spheres are then sized as spheres and the held-shape rule changes meaning, with no message. | On restore and Continue, set `_active_fitter = meta["fitter"]` when it is available; log it; if it is not available, pause the campaign and say why. | Start with `catalogue_bic`, restore in a fresh module, assert the fitter is `catalogue_bic`. |
| AN2 | **HIGH** (carried, = O7) | Accuracy | **The first batch profile of a recipe trains the model.** `_pending.pop(rid)` (app.py:1441) consumes the condition on the first matching file; later, better-settled batches are fitted and recorded but never used. | Accumulate per recipe until the reactor's done marker (or N batches), then `tell()` once with the highest-confidence fit, recording which batch. | Three batches with rising confidence: the told value is the third. |
| AN3 | MEDIUM | Efficiency | **The AI fitter blocks the watcher.** `_analyze_file` runs on the single watcher thread (app.py:1978, 2040). An AI-assisted fit is one AI call (60 s timeout, 2 client retries, so up to about 3 min) plus up to 5 model fits of up to 6 s each, then possibly a second AI round. During that time no other profile is fitted, and a recipe's several batch files queue behind it. | Run AI-assisted fits in a small worker pool (1 to 2) with the result fed back under the campaign lock; or fit the first batch with Catalogue + BIC and refine with the AI in the background. Log the time per fit. | A stub AI that sleeps 5 s: a second profile is still fitted within 1 s. |
| AN4 | MEDIUM | Efficiency, stability | **Knowledge tests run during a live campaign.** `_fk_start` and `_eval_start` (app.py:3671, 3191) do not check for a running campaign. The fitter test refits up to 20 curves × 4 arms (AI calls plus model fits) on the same CPU as live fitting. | While a campaign runs, refuse with "run the test after the campaign", or run at low priority with a pause between curves. | Start a campaign, POST evaluate: 409 with the reason. |
| AN5 | ~~MEDIUM~~ **FIXED 2026-10-25** | Robustness | **The fitter can be changed mid-campaign through the API.** `/api/methods` refuses a new optimiser while a campaign exists (app.py:2310) but accepts a new fitter (app.py:2300 to 2307). The page locks the control; scripts or a second tab do not. | Refuse a fitter change while a campaign runs, like the optimiser (409). | POST `{fitter}` while running: 409, fitter unchanged. |
| AN6 | ~~MEDIUM~~ **FIXED 2026-10-25** | Days of running (disk) | **Frozen fitter snapshots grow quadratically outside campaigns.** `_fk_snapshot` freezes with `save_root=_project_root` on every AI fit outside a campaign (app.py:3448). Every call on a fit changes the hash, so the next fit writes a new snapshot holding every judged fit. Measured: 200 calls gave 200 files, 9.5 MB; 1,000 would be about 240 MB under `.swaxs_state/fit_knowledge/`. | Save snapshots only at Start (and Continue); outside a campaign freeze in memory. Keep at most the last 20 snapshots not referenced by a campaign record. | 50 calls plus fits outside a campaign: 0 new files; Start: 1. |
| AN7 | ~~MEDIUM~~ **FIXED 2026-10-25** | Robustness | **Continue drops the fitter's frozen knowledge.** The Continue `meta_keys` list (app.py:1277 to 1280) has `knowledge` but not `fit_knowledge`, so a continued run's AI fits use the base as it is now, not as it was at Start. | Add `fit_knowledge` to `meta_keys`. | Continue a run whose base changed since Start: the snapshot hash is the original. |
| AN8 | ~~MEDIUM~~ **FIXED 2026-10-25** | Days of running (disk) | **The AI fitter cache is never pruned.** `.swaxs_state/llm_fit_cache/` gets one JSON per (curve, context, knowledge version, model); nothing removes old ones (llm_structure.py:211 to 241). Over a long beamtime with batch files and knowledge changes this is thousands of files. | Keep the newest N (e.g. 5,000) or 30 days, pruned at start and once a day. | Fill with N+10 entries: N remain, newest kept. |
| AN9 | ~~LOW~~ **FIXED 2026-10-25** | Days of running (disk) | **AI trace files are never rotated.** `logs/assistant_traces/YYYY-MM-DD.jsonl` (src/ai/traces.py:106), one per day, kept forever; the AI levels and fitter add a line per decision. Small per day, unbounded over months. | Keep 90 days (setting), pruned at start. | Old dated files beyond the window are removed; today's kept. |
| AN10 | LOW | Ease of use | **Calls on fits need a knowledge base.** With None in Setup, the box under the plot says to choose one and offers nothing else, so a call made during a quick look is lost. | Offer "Save to:" with the list of bases (and "+ New base") right in the box. | Choose a base in the box: the call is saved there. |
| AN11 | LOW | Privacy, portability | **Judged fits store absolute file paths in a git-tracked file.** Each judged fit keeps `path` (e.g. `/Users/<name>/...`) in `ai_knowledge/design/<id>.json`, which is in git and shared on Export. It is used only to reload the curve for the Fitter evaluation. | Store the path relative to the project root plus the project name; strip paths on Export; the evaluation says which curves it could not find. | Export: no absolute paths; evaluation reloads from the project-relative path. |
| AN12 | LOW (carried) | Ease of use | **No keyboard focus ring** in the Analyser (`:focus-visible` absent; also noted in the register's accessibility item). | Add the shared `:focus-visible` rule. | `test_ui_theme_uniform` checks the rule exists. |
| AN13 | LOW (structure, high value) | Backend organisation | **`app.py` is not a thin shell.** 3,719 lines: 145 functions, 61 module globals, 62 routes (knowledge 29, campaign 14, fit knowledge 2, AI 2, study 2, the rest 13). Campaign lifecycle (start, resume, Continue, advance, expiry, late results, records), cold-start assembly, the runs summary and the AI worker threads live here, against the project's one rule. Every test must import the whole app to reach them. | Move in steps, each with the golden tests unchanged: (1) a new module `session` in `src/optimizer/` (proposed, not yet written): one `CampaignSession` object owning campaign, meta, pending, pause, lock, save and restore (replaces about 20 globals); (2) a new module `records` in `src/optimizer/`: campaign and fit records, manifest entries; (3) Flask blueprints `analyzer/routes/{campaign,knowledge,fitter,ai,results}.py`; `app.py` keeps the sys.path block, wiring and startup. | The existing analyzer tests pass unchanged after each step; a new test asserts `app.py` stays under 800 lines. |
| AN14 | LOW (structure) | Clarity of code | **One 2,818-line page with about 1,900 lines of inline script** and global state shared by every tab (`_kset`, `_kStepSet`, `_fk`, `_fkPart`, `_aiLevel`, ...). Changes in one tab break others (this audit's own layout pass hit two such races). | Split the script into per-tab files served as static JS (`setup.js`, `fitting.js`, `report.js`, `knowledge.js`, `guide.js`) with one small state module; keep the jsdom harness as the guard. | The harness passes unchanged against the split files. |

## Redundancy

An AST scan compared every same-named helper across the ten app files. Most
duplication is unavoidable (each app owns its own Flask routes), but three cases
were real:

| | Finding | Action |
|---|---|---|
| a | `monitor_status` / the already-running guard existed in 4 apps and had **drifted** — none checked thread liveness | consolidated into `src/runstate.monitor_alive` (finding 4) |
| b | `_state_root` in 4 apps, 2 distinct implementations; reduction's was the broken one | reduction's fixed to match (finding 3) |
| c | `/api/browse` returns `{"path": …}` in reduction but `{"current": …}` in the average app, with different filtering — two contracts for one job | **left open**: cosmetic, UI-only, and unifying it means touching two templates |

`_persist_monitor_state` is byte-identical in all four apps — genuine duplication,
but harmless and self-documenting where it sits.

---

## Open — not fixed, ranked by expected loss

> **The reactor audit's findings (R1 to R28) live in a separate file: 25 of 27 fixed, R4 accepted, R12 open.**
> A September 2026 deep audit of `reactor/` + `src/reactor/` + `src/beamline/`
> produced its own register with severity, evidence and a proposed fix per
> item: **[REACTOR_AUDIT.md](REACTOR_AUDIT.md)**. They are kept there rather
> than merged here because they share a subsystem and a remediation order, and
> because eight of them are backed by executable probes that only make sense
> read together. All but **R12** (a rejected condition file is a log line
> only; the optimizer never hears) and the accepted **R4** were fixed on
> 2026-09-20 and are held by `tests/test_reactor_audit_2026_09.py`. Note: the
> R numbers there are not the same items as the July "8 residual risks" R1 to
> R8 further down this file. Two items still need the rig rather than code: the real `sensor_min`
> values (R14) and `sauto off` (R4); both are on the pre-beamtime checklist.

> **The assistant's findings (A1 to A10) live in
> [ASSISTANT_AUDIT.md](ASSISTANT_AUDIT.md).** Two September 2026 passes fixed
> A1 (PDF-ingest path traversal, now `secure_filename`), A2 (50 MB upload cap),
> A3 (`_fail()` instead of raw `str(exc)`), A6 (P(r) auto-Dmax; held by
> `tests/test_pr_ift.py`), A7 (Å⁻¹ to nm⁻¹ in `_load_dat`) and A8 (thread-safe
> plotting). **A9 is OPEN** (LOW): with no subtracted file, an ambiguous
> averaged keyword can fall back to the `_bkg` average. A4 (run_python is a
> guard, not a jail) is an accepted, human-gated residual.

> **The analyzer's findings live in [AUTOFIT_AUDIT.md](AUTOFIT_AUDIT.md)**
> (its own A1 to A9, all fixed, October 2026). Two LOW limitations are OPEN:
> **L1** `/api/stream` holds one server thread per viewer; **L2** a campaign
> that reaches its budget stops, and the next one must be started by hand.

> **N1–N4 are FIXED** (September 2026, before beta): the reduction processed-set
> and the average batch state now persist across a restart, batch membership is
> tracked by filename rather than by a count that a failed read could shift, and
> `/api/run` is refused with 409 while the monitor is live. Regression tests:
> `tests/test_continuous_run_hardening.py`. Rows kept below for the history.
> N7 was fixed 2026-09-11 and N6 is mostly fixed; **N5, N16 and O3 remain
> open**. Per-item status with evidence:
> [../CONTINUOUS_RUN_HARDENING_PLAN.md](../CONTINUOUS_RUN_HARDENING_PLAN.md).

These were reproduced or confirmed by reading, but are larger than a safe
single-session change. Ranked by what they cost during an unattended run.

> **Remediation plan.** The subset that threatens a multi-day autonomous run
> (N1 to N7, N16 and O3) has a phased plan with a fix, the gotchas and a
> regression test for each:
> **[../CONTINUOUS_RUN_HARDENING_PLAN.md](../CONTINUOUS_RUN_HARDENING_PLAN.md)**.
> N1 to N4 and N7 are done, N6 mostly; N5, N16 and O3 are open. N16 should
> follow N5, because the average monitor loop's stop logic changes in both.

| # | Sev | Finding | Consequence |
|---|---|---|---|
| N1 | **FIXED** | `reduction/app.py` `_processed_files` is memory-only, and `find_new_raw_files` filters on nothing else. After a restart it re-reduces the **entire** experiment, oldest first. | Every `.dat` rewritten, thousands of manifest writes under the cross-process lock, and live frames starved until the backlog clears — worse now that the monitor auto-resumes. Fix: persist the set, or skip files whose `.dat` is newer than the `.raw`. |
| N2 | **FIXED** | No mutual exclusion between `/api/run` and the reduction monitor. Both take the same file list and share one `AzimuthalIntegrator` (whose contract says single-threaded), and both write then `replace()` the same `.part`. | An interleaved, truncated `.dat` published atomically — it looks complete to every downstream size/mtime check. Fix: one lock, or reject `/api/run` while monitoring. |
| N3 | **FIXED** | `average` `_avg_batch_state` is zeroed on every `monitor_start`, including the boot resume. | After a restart the whole night is re-averaged: every batch file overwritten and one `file.averaged` per batch — which is what the reactor treats as measurement-complete. Fix: persist the batch state alongside the monitor state. |
| N4 | **FIXED** | `average` batch state is a **count**, not a set of identities, and `read_folder` silently skips unreadable files. One transient read failure shifts every subsequent batch boundary. | A frame is silently reused in the next batch and one is dropped; if the count ever exceeds the group length, that keyword stops averaging entirely. Fix: track consumed filenames. |
| N5 | **MEDIUM** | `average` stop→start within one interval can leave two loops racing the same counter, or the old thread can switch the **new** monitor off after `start` returned OK. | Double-written or skipped batches; or a monitor that reports running and is not. Fix: per-run `threading.Event` instead of a shared bool; join the old thread. |
| N6 | MEDIUM · **MOSTLY FIXED** (`_results` is guarded by `_results_lock` and every loop iterates `_results_snapshot()`; `tests/test_quality_audit_fixes.py`. Residual: `_overrides` is unlocked and records are still mutated in place) | `quality` `_results` / `_overrides` are mutated by the grader thread, the bus thread and request threads while other requests iterate them. | `RuntimeError: dictionary changed size during iteration`: tolerable in `/api/results`, but `_recolor()` and `_rescore_all()` have side effects, so a mid-loop failure leaves half the profiles re-verdicted and re-copied, permanently, with nothing logged. Fix: snapshot under `_lock`. |
| N7 | ~~MEDIUM~~ **FIXED 2026-09-11** | Two mechanisms. A parse cache in `src/plot_reduction.py::read_folder`, keyed by path and valued `((mtime_ns, size), entry)` so a rewritten file replaces its entry rather than leaving a dead one behind, LRU-bounded by `SWAXS_READ_CACHE_MAX` (3000; each entry holds three float64 arrays, so the bound is a memory bound). And `read_folder(skip_names=...)`, which the average monitor feeds from `_consumed_for(det)`, so frames already averaged into a batch are never stat'd or parsed — a poll now costs the NEW frames only. `todo`, and the gate this app publishes, are unchanged. Tests: `tests/test_read_folder_cost.py`. The average and subtraction monitor defaults dropped 10 s → 3 s as a result. |
| N8 | **MEDIUM** (partly mitigated: Stop now joins the sync thread for up to 3 s and the watch loop joins its workers before reporting "Stopped"; queued transfers are still not cancelled, and the handle is dropped after the 3 s timeout) | `calibration` Stop does not stop the SFTP copy: the executor drains its queue and the handle is dropped anyway. | Status says "Stopped" while an orphan thread keeps writing; a second Start can run two syncs into the same `.part` paths. Fix: `shutdown(cancel_futures=True)`; keep the handle until the thread is dead. |
| N9 | **MEDIUM** | `calibration` never persists/resumes its transfer — the O1 fix skipped the first stage of the pipeline. | A restart silently stops data ingest; every downstream app then honestly reports "no new files" and stays green. Note: only key-based auth can be resumed, since the password is deliberately never persisted. |
| N10 | **MEDIUM** | `quality` "Re-grade with AI" is stored in `_overrides`, so it is recorded as `source="user", overridden=True` and pins the verdict forever. | Provenance claims a scientist labelled the profile; later threshold changes can never reclassify it. Fix: a separate `_ai_verdicts` store. |
| N11 | **MEDIUM** | A single override shifts the **global** adaptive threshold, persists it, and does not re-sort. Repeat overrides of one file are counted repeatedly. | The persisted threshold stops matching the already-sorted files. Fix: `_recolor()` after `_adapt_threshold()`; key `_adapt` by path. |
| N12 | **LOW** | Five legacy `analysis` routes duplicate the current ones and have drifted numerically (`/api/peak` forces `n_peaks ≥ 1`; `/api/waxs_peaks` auto-detects). Only the new ones produce `Analysed/` files and QC. | Two "peak fits" of the same curve disagree with no explanation. Fix: delete them (the template calls neither) or make them wrappers. |
| N13 | **LOW** | `analysis` Guinier overlay is drawn over the whole q-range, ignoring `res["q_range"]`. | The saved `*_fit.dat` shows an extrapolated curve that reads as a much worse fit than it is. |
| N14 | **LOW** | Both reduction and the average app drive `pyplot` from a 2-worker pool; the global figure manager is not thread-safe. | Two simultaneous render requests can garble a figure. UI-only. |
| N15 | **LOW** | `src/analysis/atsas.py:122` `mkdtemp()` per `run_datgnom` call, never removed. | One temp dir per file in an ATSAS batch. |
| N16 | **MEDIUM** (partly mitigated: average and subtraction now poll every 3 s after N7; still no stage acts on the upstream event, only quality subscribes to `file.subtracted`) | Four stages poll independently at 10 s, in series, plus a 2-poll stability gate: 99 % of the 264 s frame-to-fit latency is waiting, 1 % is CPU (measured). Every app already publishes and receives bus events. | A frame takes ~4 min to reach the optimizer when the work costs ~2 s. Fix: have each stage act on the upstream `file.*` event and keep polling only as the fallback it was meant to be. |


---

### Reactor & beamline — 8 residual risks

From the July 28 reactor safety audit. **Re-checked against the code in October
2026:** R1, R2, R3 were already fixed; R4, R5, R6, R7, R8 fixed then (tests in
`tests/test_reactor_open_defects_2026_10.py`). **All eight are now closed.**

| # | Sev | Finding | Consequence |
|---|---|---|---|
| R1 | HIGH · **FIXED** (found Oct 2026: `estop()` idles pumps before taking the lock) | **E-stop latency.** `_end_run` runs the cooldown `set_temperature`, `_manifest` and `_feedback` inline (`src/reactor/controller.py:698`, `:723-726`, `:766-779`) while callers hold `self._lock` (`:408-412`, `:1087-1103`). | Measured E-stop latency with a blocking manifest write: **7.8 s**. Fix: do the bookkeeping outside the lock, or make E-stop lock-free. |
| R2 | HIGH · **MOSTLY FIXED** (found Oct 2026: the pump lock is per command, not across retries; E-stop idles each pump separately) | **Serial retry latency.** `for _ in range(5)` retries at ~1 s each while holding the per-pump lock (`src/reactor/drivers/Py_P_Pump.py:168`). | One unresponsive pump stalls every other pump command for 5 s, E-stop included. |
| R3 | HIGH · **FIXED** (found Oct 2026: `set_run_settings` rejects negatives; flush falls back to config) | **Negative flows are accepted.** `set_run_settings` / `flush_now` never validate sign (`src/reactor/controller.py:298-325`). | `flush_rate: -500` reaches the pump; `arm_wait_s: -99` skips timed arming entirely, so a recipe runs before it reaches temperature. |
| R4 | HIGH · **FIXED, Oct 2026** (documented no-op: measurement-end is always on; key not read) | **`run.end_on_measurement` is dead config.** Assigned at `src/reactor/controller.py:99` and referenced nowhere else in the repo. | The documented primary run-end condition is not wired to the flag that claims to control it. Either wire it or delete the key. |
| R5 | MED · **FIXED, Oct 2026** (the skip stays, but the UI asks first and the log records the temperature it started at) | **`start_now()` bypasses the temperature gate** (`src/reactor/controller.py:205-212`). | A manual start can inject reagent into a cold reactor. |
| R6 | MED · **FIXED, Oct 2026** (the flush is supervised: flow faults warn or E-stop as in a run; a pump that keeps delivering past its volume limit E-stops) | **Volume limits and flow faults are only checked while `running`** — `if self.state == "running":` at `src/reactor/controller.py:1248-1249`. | During `flushing` (the longest phase, 20 min shipped) neither check runs. |
| R7 | MED · **FIXED, Oct 2026** (`shutdown()` joins the loop and closes every pump port after idling) | **`shutdown()` never joins the loop thread or closes the serial ports** (`src/reactor/controller.py:1324-1341`). | On Windows the COM ports stay locked, so the next start cannot find the pumps. |
| R8 | MED · **FIXED, Oct 2026** (`backend` in every run record and manifest entry) | **Run records carry no `backend` flag.** `backend` appears only in the `reactor.run_start` event (`:696`) and `status()` (`:1289`), not in the record `_end_run` writes. | A campaign resuming from `manifest.json` cannot tell mock-derived observations from real ones, and will train the GP on both. |

### Manifest & optimizer (`O`)

From the July 29 platform audit; these are the only full descriptions of these
numbers anywhere, which is why the file they came from could not simply be deleted.

| # | Sev | Finding | Consequence & fix |
|---|---|---|---|
| O3 | **HIGH · OPEN** (partly mitigated) | **Manifest write cost grows with the whole manifest.** Every `update_manifest` call is lock → `load_manifest` → mutate → `save_manifest`. With the SQLite store (`src/manifest_store.py`) that means `load_dict` rebuilds the full dict from the DB, `save_dict` upserts **every** row of every section in one transaction, and `_write_json` then re-exports the whole `manifest.json` with `indent=2`. Reduction still calls `update_manifest` once per frame, and the hub writes every bus event into the manifest the same way (`_append_event_to_manifest`). So each write is still O(N) and a run's total is O(N²). The July measurement (3.5 ms at 100 entries, 609 ms at 20 000) was taken on the old JSON store and has not been repeated. | By 03:00 the bookkeeping can cost more than the science and blocks every writer behind the lock. Mitigated: `analyses` is capped (`SWAXS_MANIFEST_ANALYSES_CAP`, default 3000) and routine snapshots are throttled to one per 600 s. Fix: write only the changed rows (the per-entry `add_*` helpers already know them), batch reduction's per-frame writes per poll cycle as average/background do, and drop `indent=2` from the export. The original cause given here (large `config_snapshot`) was wrong: the snapshots are 1 to 6 keys. See ../CONTINUOUS_RUN_HARDENING_PLAN.md Phase 4. |
| O4 | **HIGH · OPEN** | **A lock failure disables manifest writing silently.** `manifest_lock` (`src/manifest.py`) calls `fcntl.flock` unguarded; on NFS/SMB it can raise `ENOTSUP`, and every caller swallows the exception into one log line. The SQLite store adds a second risk on the same mounts: `manifest.db` runs in WAL mode, which SQLite does not support on network file systems. | Data reduces fine, `.dat` files appear, and the run has **no provenance**. Highest-variance item left. Fix: `except OSError` with a one-time ERROR and a portable `os.mkdir` lock fallback; detect a network mount and fall back to `journal_mode=DELETE` (or warn loudly). Until then keep the project folder on a local disk. |
| O7 | **HIGH** | **Only the first rolling batch trains the optimizer.** The average app emits `batch001, batch002…`; `_pending.pop(rid, None)` (`analyzer/app.py:296`) means first-arrival wins — and `batch001` is the *least* settled flow with the worst statistics. | The manifest and campaign history disagree about the same recipe with no marker saying which trained the model. Fix: accumulate per recipe and `tell()` once on the highest-confidence batch. |
| O9 | **MED** | **Subtracted `.dat` drops the sample's metadata footer**, including the simulator's `simulated=1` flag — `background/app.py:215-241` writes only its own header. | Mock and real results are indistinguishable downstream: manifest, campaign history and Slack reports alike. |
| O10 | **MED** | **Switching the project folder mid-session splits the writers.** Monitor loops keep the absolute paths captured at start; the manifest write in the same loop uses the live `_project_root`; reduction writes to `data_directory.parent` — a third manifest. | Data lands in the old folder, provenance in the new. Fix: stop the monitors on `set_project` with a clear log line. |
| O12 | MED · **FIXED** (October 2026, SQLite store) | **No `fsync` before `replace`, and an unsalvageable manifest reset to empty.** With the default SQLite backend the DB is the source of truth and each save is one transaction; a missing, empty or unreadable `manifest.db` is restored from the newest snapshot in `.manifest_snapshots/` with the newer `manifest.json` merged on top; a JSON that cannot be read with nothing to restore from raises `ManifestUnreadableError` instead of starting empty. Tests: `tests/test_manifest_snapshots.py`, `tests/test_manifest_sqlite.py::test_json_reset_cannot_wipe_history`. | Residual, only with `SWAXS_MANIFEST_BACKEND=json`: `_read_json_manifest` still resets to an empty manifest when salvage fails (the damaged copy is kept as `manifest.corrupt-*.json`), and the JSON write has no `fsync`. |
| O14 | MED · **FIXED** | **`_FAIL_LOSS` rows entered the GP fit.** `CampaignController` now drops failed measurements (no size, or loss at the `_FAIL_LOSS` sentinel) before fitting the surrogate (`src/optimizer/campaign.py`, the fit helper). They still count against the budget, by design. | A single 1e3 outlier no longer flattens the posterior. |
| O15 | **MED** *(half fixed)* | The `_MAX_RESULTS=600` bound and `_handled` pruning landed (`analyzer/app.py:76`, `:383-384`, `:527-528`). **Residual: the SSE stream still has no sequence-number filter.** | Re-sends summaries the client already has. Reduction's SSE is the model implementation. |
| O16 | MED · **FIXED** | `at_bounds` is computed and persisted, and `_confidence` in `src/analysis/nanoparticle.py` now scales confidence by 0.25 when a parameter rails against a bound (and by 0.5 when the solver did not converge). | A railed boundary artefact reaches the optimizer with low confidence, so the GP treats it as noisy. |
| O18 | **LOW · OPEN** | On Windows the manifest lock is a silent no-op: `manifest_lock` uses `fcntl` only, with no `msvcrt` fallback (`src/manifest.py`). SQLite's own locking serialises each transaction but not the load → mutate → save cycle, so two apps can still overwrite each other's update. | Clean, invisible lost updates with several concurrent writers. Windows is a supported platform. Fix: `msvcrt.locking` fallback, or the portable directory lock from O4. |

**O13 is FIXED** (was listed open): the quality gate's LLM adjudication is now
honoured (`quality/app.py:216-222`) and there is a graded-file cache (`:481`).

### Subtraction UI (`C`/`U`) and pipeline (`D`)

| # | Sev | Finding |
|---|---|---|
| C4 | LOW | No warning when the sample and background q-ranges barely overlap — the scale is fitted on a handful of points and reported with normal confidence. |
| C5 | LOW | The detector tag is taken from the UI toggle, not inferred from the file path, so a mismatched toggle mislabels every output. |
| U3 | LOW | No busy state during batch loops — the UI looks idle while a long batch runs. |
| U5 | LOW | The metadata tab shows the sample only; the background's metadata is not viewable. |
| U6 | LOW | A `keyword` subtraction mode exists in the backend (`background/app.py:859`) and is not surfaced in the UI. |
| D1 | LOW | `check_imports.py:28-35` `SRC_MODULES` omits `src.analysis.core`, `src.events` and `src.ai.*`, so the import check passes over them. |
| D3 | ~~MED~~ **FIXED 2026-09-11** | Background subtraction science moved to `src/background/core.py` (`_subtract`, `_interpolate_onto`, `_interpolate_onto_signed`, `_auto_scale`, `_qc_metrics`, `truncate_rebin`); `background/app.py` imports them. `_write_dat` stays in the app — it is I/O bound to the app's truncation state, not science. |
| D4 | LOW | Scan averaging is unweighted (`src/plot_reduction.py:183`); inverse-variance or I0 weighting would be statistically optimal. `np.interp` also clamps to edge values outside a file's q-range. |

(D2 — CLAUDE.md's stale import table — was fixed in this documentation pass.)

### Ops, UI and docs

| Sev | Finding |
|---|---|
| **MED** | `spec.data_dir` falls back to a hardcoded `/msd_data/.../Auto_Test` when the `hub_path_map` prefix is wrong, so SPEC writes where nothing is reduced. Pre-run check, see `PRE_BEAMTIME_READINESS.md`. |
| **MED** | A stale `<project_root>/config.yml` silently relocates the 1D outputs, because reduction derives its root from `data_directory.parent`. |
| LOW | `/api/set_project` is dropped on a not-yet-mounted path — background and analysis guard on `is_dir()`. Re-select the folder once a slow network drive appears. |
| LOW | The reactor's **"■ Stop → flush" is mislabelled during the `flushing` state**, where it stops the flush and idles. |
| LOW | No tooltips on the reactor's stop/safe control cluster, and the E-stop → Reset recovery path is not explicit in the UI. |
| LOW | `/api/browse` returns `{"path": …}` in reduction but `{"current": …}` in the average app. Five apps (analysis, analyzer, assistant, quality, reactor) now share `src/folder_browse.py` (`{"current", "parent", "dirs"}`); reduction, average, background, calibration and the hub keep their own routes. |
| LOW | Accessibility: every app now uses the shared palette, whose text contrast is tested (`tests/test_ui_theme_uniform.py`), but `--border-strong` fails 3:1 in both themes and six apps (analysis, analyzer, calibration, quality, reactor, watchdog) have no `:focus-visible` rule. See `docs/DESIGN_SYSTEM.md` §6. |

### Fresh start reprocessed the whole folder — FIXED 2026-09-11

Reported twice from the beamline. Pressing **Start** on a project folder that
already held data made reduce, average, subtract and the auto-fit app work
through the ENTIRE folder — every file of every previous run, oldest first —
before touching the frames of the run just started. Each stage is one worker
thread, so the live data queued behind the whole back-catalogue and the loop
appeared stalled for minutes to hours.

Four separate causes, one rule. `monitor_start` in `average` set
`_avg_batch_state = {}` and in `background` set `_sub_done = {}` unless the
request carried `resume=True`; the analyzer cleared `_handled` on abort and on
a folder change without reseeding; reduction survived only because
`_already_reduced` checks the disk — and *that* check was silently inoperative
whenever `saxs_filename_prefix`/`waxs_filename_prefix` was configured, because
`Experiment._make_output_path` strips the prefix from the output stem while
the glob looked for the prefixed name.

Those clears came from the N3 fix, which was about the *boot resume* wrongly
clearing state; "fresh start" was then read as "forget everything", which is
not what an operator means by it.

The rule now lives once in **`src/backlog.py`** and is applied by all four:
an input whose output exists is done; an input with no output older than the
crash-gap window is history; an input with no output newer than the window is
left for normal processing. Each app rebuilds its memo from durable disk state
on a fresh start instead of wiping it —
`average/app.py::_seed_batch_state_from_disk` (which also restores batch
NUMBERING, since `n` resetting to 0 rewrote `batch001` over the existing
file — lossy, not merely slow),
`background/app.py::_seed_sub_done_from_disk`,
`analyzer/app.py::_reseed_intake`. Each now also logs, once per run, how much
of the folder it is skipping: the silence was what made this indistinguishable
from a hang. Tests: `tests/test_fresh_start_no_backlog.py`,
`tests/test_analyzer_fresh_run_backlog.py`.

### Autonomous Watch (watchdog, 5110; formerly "Auto Watch")

Audited 2026-09-11; full write-up with line references and the reasoning in
[AUTO_WATCH_AUDIT.md](AUTO_WATCH_AUDIT.md). W1–W9, W13 and W24 are **fixed**;
these are the residuals, kept here so this file stays the one place an open
defect lives.

| ID | Sev | Finding |
|---|---|---|
| W10 | ~~MED~~ **FIXED** | The `campaign` notification category was wired to nothing. `src/watchdog/policy.py` `_EVENT_CATEGORY` now maps `average.skipped` and `file.skipped` (a condition losing its data point) to `campaign`. |
| W11 | MED | **A low-confidence fit is classified as a fault**, so it overrides quiet hours and snooze (`messages.format_fit_complete` returns `level="fault"` when `suspect`). Its category is `results`, so the only way to stop 3am pages about a poor fit is to silence all fit results. |
| W12 | MED | **`summary: "hourly"` is accepted, validated, persisted and exposed on `/api/settings` — and never read.** `should_send` ignores `cfg["summary"]`. config.yml's comment admits it is a placeholder; `policy.py`'s docstring claims otherwise. |
| W14 | MED | **Ports hard-coded in three places** (`probes.MONITOR_APPS`, `probes.ANALYZER_PORT`/`REACTOR_PORT`, `app.LOOP_APPS`), duplicating `apps.yml`. A renumbering makes every probe read "down". |
| W15 | MED | **The localhost probes do not bypass a configured proxy.** With `http_proxy` exported, every probe is routed out and fails, so all six apps read as down and every stall diagnosis is wrong in the same direction. Fix: one opener with `ProxyHandler({})`. |
| W17 | LOW | **The loop-closure edge is unmonitored.** `fit` is last in `_STAGE_ORDER` and `STAGE_TIMEOUTS` has no `collect` key, so once `fit.complete` lands nothing is expected until it ages out of the 2 h window. The reactor failing to start the next run raises no stall. Documented as intended by `test_at_final_stage`, but it is the gap that matters overnight. |
| W18 | LOW | Dead code: `expectations.format_stall_message` (imported by the app, never called — `diagnose_stall` superseded it) and `probes.check_monitor_alive`. `format_stall_message` also labels the *overdue delta* as the silence duration. |
| W19 | LOW *(half fixed)* | `save_notify_settings` now writes config.yml through a temp file and `os.replace` (atomic). Residual: still **unlocked**, so two concurrent POSTs can each overwrite the other's change. |
| W20 | LOW | **Docstring/contract drift on `now`.** `policy.should_send` documents UTC but *requires* local time for quiet hours to mean the operator's night; `expectations` wants UTC. The app passes each correctly; a future caller following the docstrings breaks quiet hours by the UTC offset. |
| W21 | LOW | `_in_quiet_hours` treats `start == end` as "never quiet" rather than "always quiet", accepted silently. |
| W22 | LOW | `should_send`'s snooze check is `isinstance(snooze_until, float)` with a `0.0` fallback, so an int silently disables the snooze. Unreachable today (`_state` is memory-only); a trap if snooze is ever persisted. |
| W23 | LOW | `/api/test` bypasses `should_send`, so the Test button still sends with the master switch off. Defensible, undocumented. |
| W25 | ~~LOW~~ **FIXED 2026-09-14** | Auto Watch now has a generated scattering icon (`tools/make_watchdog_icon.py`): the monodisperse SAXS profile every other icon uses, ending in a large dot at the newest point — the curve is the loop's output, the dot is the frame being watched. Tuned by inspecting the downscaled result: a dashed "expected but not arrived" tail and a thin white ring around the dot were both tried and rejected as invisible at 16 px. Also closed a related gap the favicon work exposed — `analyzer` and `reduction` both shipped `#1565C0`, so they were distinguishable only by emoji; analyzer is now `#4527A0`. `tests/test_favicons.py` asserts no two apps share a colour or an icon, that every declared `icon_image` exists, and that `hub/static/` carries a copy of each (the hub serves its own static folder). |

### Platform quick audit, October 2026 (`Q`)

Audited 2026-10-14, every app and the shared code; file, line and fix for each in
[PLATFORM_QUICK_AUDIT_2026_10.md](PLATFORM_QUICK_AUDIT_2026_10.md). **All FIXED 2026-10-14**; residual: Background pairing in a shared SAXS+WAXS Averaged folder.

| ID | Sev | Finding |
|---|---|---|
| Q1 | ~~HIGH~~ **FIXED** | Assistant: Listens on every network interface (`0.0.0.0`) |
| Q2 | ~~HIGH~~ **FIXED** | Assistant: `run_python` sandbox can read files, reach the network, run shell commands |
| Q3 | ~~HIGH~~ **FIXED** | Reduction: A SAXS frame counts as reduced when its WAXS twin's `.dat` exists |
| Q4 | ~~HIGH~~ **FIXED** | Reduction: CSV metadata matched by substring: `scan10` can take `scan1`'s i0/bstop |
| Q5 | ~~HIGH~~ **FIXED** | Reduction: Stop then Start within one interval runs two monitor loops |
| Q6 | ~~HIGH~~ **FIXED** | Reduction: Run now and Watch can run together (docs say they cannot) |
| Q7 | ~~HIGH~~ **FIXED** | Analysis: q unit in the file header is ignored: Å⁻¹ files give results 10× off |
| Q8 | ~~HIGH~~ **FIXED** | Background: One q-window and rebin grid applied to both SAXS and WAXS |
| Q9 | ~~HIGH~~ **FIXED** | Analyser: An expired condition stays queued; its real result is discarded |
| Q10 | ~~HIGH~~ **FIXED** | Analyser: ML regressor fitter gives confident sizes for noise |
| Q11 | ~~MEDIUM~~ **FIXED** | Background: Stop then Start within one interval runs two monitor loops |
| Q12 | ~~MEDIUM~~ **FIXED** | Average: Shared output folder: WAXS batches overwrite SAXS batches of the same name |
| Q13 | ~~MEDIUM~~ **FIXED** | Average: Fresh start after changing N re-averages the whole folder |
| Q14 | ~~MEDIUM~~ **FIXED** | Analysis: Saving an analysis rewrites the Subtracted `.dat` in place |
| Q15 | ~~MEDIUM~~ **FIXED** | Analysis: Auto Guinier range does not converge to q·Rg ≤ 1.3 |
| Q16 | ~~MEDIUM~~ **FIXED** | Quality: An LLM verdict on a borderline curve never expires after a threshold change |
| Q17 | ~~MEDIUM~~ **FIXED** | Reactor: Abort or new campaign does not withdraw a recipe staged for its blank flush |
| Q18 | ~~MEDIUM~~ **FIXED** | Analyser: Continue re-proposes seed conditions already issued |
| Q19 | ~~MEDIUM~~ **FIXED** | Analyser: After a restart, a measurement that landed during downtime is never fitted |
| Q20 | ~~MEDIUM~~ **FIXED** | Analyser: Analyser and reactor can watch different Conditions folders, silently |
| Q21 | ~~MEDIUM~~ **FIXED** | Reactor: Sample/background tags are free text; the parsers know only fixed names |
| Q22 | ~~MEDIUM~~ **FIXED** | Platform: `SWAXS_HUB_PORT` override cuts every app off the event bus |
| Q23 | ~~MEDIUM~~ **FIXED** | Platform: Hub and assistant accept cross-site POSTs from any web page |
| Q24 | ~~MEDIUM~~ **FIXED** | Watch: Losing the bus is never reported; a dead watchdog looks like a quiet run |
| Q25 | ~~MEDIUM~~ **FIXED** | Watch: Messages show as "sent" when no webhook is configured |
| Q26 | ~~MEDIUM~~ **FIXED** | Hub: App log handles never closed; on Windows a restart empties the crash log |
| Q27 | ~~LOW~~ **FIXED** | Average: Batch `n_files` counts frames dropped by the I0 filter |
| Q28 | ~~LOW~~ **FIXED** | Average: Placeholder and fallback defaults disagree with the backend |
| Q29 | ~~LOW~~ **FIXED** | Average: Seed sorts by frame index across scans *(suspected impact)* |
| Q30 | ~~LOW~~ **FIXED** | Quality: Manifest quality flags are only added, never cleared |
| Q31 | ~~LOW~~ **FIXED** | Quality: Resume replays an old pass threshold over the saved one |
| Q32 | ~~LOW~~ **FIXED** | Background: Auto high-q scale silently falls back to 1.0 with fewer than 3 points |
| Q33 | ~~LOW~~ **FIXED** | Optimiser: Search space ignores per-pump `sensor_min` / `max_flow` |
| Q34 | ~~LOW~~ **FIXED** | Optimiser: Pareto front includes low-confidence fits |
| Q35 | ~~LOW~~ **FIXED** | Hub: Windows Stop kills only the top process |
| Q36 | ~~LOW~~ **FIXED** | Launchers: `start_platform.bat` banner error (`&`), blank Project line; 5107 named "Auto-Fit" |
| Q37 | ~~LOW~~ **FIXED** | Docs: Background "restart resumes" needs `SWAXS_RESUME=1`; p(r) `_fit.dat` columns are q, I |

---

## Recommended order

1. **O4, then O3**: the silent lock failure is the highest-variance item left
   (on an NFS/SMB project folder the run can produce no provenance at all and
   nothing says so); then the per-write cost that grows with the manifest.
2. **O7**: only the first rolling batch of a recipe trains the optimizer.
3. **N5**: the average monitor stop/start race.
4. **N16**: drive the stages off the event bus instead of pollers in series.
   Largest latency win and the most invasive.

Done since this list was first written: R1 to R8 (October 2026), N1 to N4,
N7, D3, O12, O14, O16.


## What I checked and found sound

Bounding the report matters as much as filling it. Verified as correct:

* every `src/` call signature used by the audited apps (`add_file_entry`,
  `make_provenance`, `average_batch`, `read_dat_data_metadata`'s 5-tuple,
  `save_monitor`/`load_monitor`, `grade_profile`, `save_analysis`);
* the `decide_intake` verb contract, now that `background` is fixed — a test
  asserts no watcher compares it to a string it never returns;
* `batch_no = consumed // n_per_batch + 1` is not off by one;
* the `.dat` metadata marker is consistent between every writer and the reader;
* **no SFTP credential leakage**: the password is stripped before the config is
  written, `/api/sftp/config` returns only that sanitised copy, and no log line
  formats the config dict;
* all nine apps boot, answer `/api/health`, and stop cleanly with no false crash.
