# Open defects — the platform's single register

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

> **N1–N4 are FIXED** (September 2026, before beta): the reduction processed-set
> and the average batch state now persist across a restart, batch membership is
> tracked by filename rather than by a count that a failed read could shift, and
> `/api/run` is refused with 409 while the monitor is live. Regression tests:
> `tests/test_continuous_run_hardening.py`. Rows kept below for the history.
> **N5–N7, N16 and O3 remain open** — see
> [../CONTINUOUS_RUN_HARDENING_PLAN.md](../CONTINUOUS_RUN_HARDENING_PLAN.md).

These were reproduced or confirmed by reading, but are larger than a safe
single-session change. Ranked by what they cost during an unattended run.

> **Planned work.** The subset that threatens a multi-day autonomous run —
> N1–N7, N16 and O3 — has a phased remediation plan with a fix, the gotchas
> and a regression test for each: **[../CONTINUOUS_RUN_HARDENING_PLAN.md](../CONTINUOUS_RUN_HARDENING_PLAN.md)**.
> Not yet implemented. Two findings from writing it that change this table:
> **O3's stated cause below is wrong** (see the note on O3), and **N16 must be
> done after N3/N4/N5** — extracting the average monitor loop before its state
> is fixed means doing the work twice.

| # | Sev | Finding | Consequence |
|---|---|---|---|
| N1 | **FIXED** | `reduction/app.py` `_processed_files` is memory-only, and `find_new_raw_files` filters on nothing else. After a restart it re-reduces the **entire** experiment, oldest first. | Every `.dat` rewritten, thousands of manifest writes under the cross-process lock, and live frames starved until the backlog clears — worse now that the monitor auto-resumes. Fix: persist the set, or skip files whose `.dat` is newer than the `.raw`. |
| N2 | **FIXED** | No mutual exclusion between `/api/run` and the reduction monitor. Both take the same file list and share one `AzimuthalIntegrator` (whose contract says single-threaded), and both write then `replace()` the same `.part`. | An interleaved, truncated `.dat` published atomically — it looks complete to every downstream size/mtime check. Fix: one lock, or reject `/api/run` while monitoring. |
| N3 | **FIXED** | `average` `_avg_batch_state` is zeroed on every `monitor_start`, including the boot resume. | After a restart the whole night is re-averaged: every batch file overwritten and one `file.averaged` per batch — which is what the reactor treats as measurement-complete. Fix: persist the batch state alongside the monitor state. |
| N4 | **FIXED** | `average` batch state is a **count**, not a set of identities, and `read_folder` silently skips unreadable files. One transient read failure shifts every subsequent batch boundary. | A frame is silently reused in the next batch and one is dropped; if the count ever exceeds the group length, that keyword stops averaging entirely. Fix: track consumed filenames. |
| N5 | **MEDIUM** | `average` stop→start within one interval can leave two loops racing the same counter, or the old thread can switch the **new** monitor off after `start` returned OK. | Double-written or skipped batches; or a monitor that reports running and is not. Fix: per-run `threading.Event` instead of a shared bool; join the old thread. |
| N6 | **MEDIUM** | `quality` `_results` / `_overrides` are mutated by the grader thread, the bus thread and request threads while other requests iterate them. | `RuntimeError: dictionary changed size during iteration` — tolerable in `/api/results`, but `_recolor()` and `_rescore_all()` have side effects, so a mid-loop failure leaves half the profiles re-verdicted and re-copied, permanently, with nothing logged. Fix: snapshot under `_lock`. |
| N7 | **MEDIUM** | `average` re-reads and re-parses **every** `.dat` in the Reduction folder on every poll (default 10 s), with no mtime filter. | At 10 000 files the loop falls progressively behind the acquisition it is tracking. Fix: cache by `(path, mtime, size)`. |
| N8 | **MEDIUM** | `calibration` Stop does not stop the SFTP copy — the executor drains its queue and the handle is dropped anyway. | Status says "Stopped" while an orphan thread keeps writing; a second Start can run two syncs into the same `.part` paths. Fix: `shutdown(cancel_futures=True)`; keep the handle until the thread is dead. |
| N9 | **MEDIUM** | `calibration` never persists/resumes its transfer — the O1 fix skipped the first stage of the pipeline. | A restart silently stops data ingest; every downstream app then honestly reports "no new files" and stays green. Note: only key-based auth can be resumed, since the password is deliberately never persisted. |
| N10 | **MEDIUM** | `quality` "Re-grade with AI" is stored in `_overrides`, so it is recorded as `source="user", overridden=True` and pins the verdict forever. | Provenance claims a scientist labelled the profile; later threshold changes can never reclassify it. Fix: a separate `_ai_verdicts` store. |
| N11 | **MEDIUM** | A single override shifts the **global** adaptive threshold, persists it, and does not re-sort. Repeat overrides of one file are counted repeatedly. | The persisted threshold stops matching the already-sorted files. Fix: `_recolor()` after `_adapt_threshold()`; key `_adapt` by path. |
| N12 | **LOW** | Five legacy `analysis` routes duplicate the current ones and have drifted numerically (`/api/peak` forces `n_peaks ≥ 1`; `/api/waxs_peaks` auto-detects). Only the new ones produce `Analysed/` files and QC. | Two "peak fits" of the same curve disagree with no explanation. Fix: delete them (the template calls neither) or make them wrappers. |
| N13 | **LOW** | `analysis` Guinier overlay is drawn over the whole q-range, ignoring `res["q_range"]`. | The saved `*_fit.dat` shows an extrapolated curve that reads as a much worse fit than it is. |
| N14 | **LOW** | Both reduction and the average app drive `pyplot` from a 2-worker pool; the global figure manager is not thread-safe. | Two simultaneous render requests can garble a figure. UI-only. |
| N15 | **LOW** | `src/analysis/atsas.py:122` `mkdtemp()` per `run_datgnom` call, never removed. | One temp dir per file in an ATSAS batch. |
| N16 | **MEDIUM** | Four stages poll independently at 10 s, in series, plus a 2-poll stability gate — 99 % of the 264 s frame-to-fit latency is waiting, 1 % is CPU (measured). Every app already publishes and receives bus events. | A frame takes ~4 min to reach the optimizer when the work costs ~2 s. Fix: have each stage act on the upstream `file.*` event and keep polling only as the fallback it was meant to be. |


---

### Reactor & beamline — 8 residual risks

From the July 28 reactor safety audit. All eight re-verified open, and none were
in any register before this consolidation — the biggest gap the merge closed.

| # | Sev | Finding | Consequence |
|---|---|---|---|
| R1 | **HIGH** | **E-stop latency.** `_end_run` runs the cooldown `set_temperature`, `_manifest` and `_feedback` inline (`src/reactor/controller.py:698`, `:723-726`, `:766-779`) while callers hold `self._lock` (`:408-412`, `:1087-1103`). | Measured E-stop latency with a blocking manifest write: **7.8 s**. Fix: do the bookkeeping outside the lock, or make E-stop lock-free. |
| R2 | **HIGH** | **Serial retry latency.** `for _ in range(5)` retries at ~1 s each while holding the per-pump lock (`src/reactor/drivers/Py_P_Pump.py:168`). | One unresponsive pump stalls every other pump command for 5 s, E-stop included. |
| R3 | **HIGH** | **Negative flows are accepted.** `set_run_settings` / `flush_now` never validate sign (`src/reactor/controller.py:298-325`). | `flush_rate: -500` reaches the pump; `arm_wait_s: -99` skips timed arming entirely, so a recipe runs before it reaches temperature. |
| R4 | **HIGH** | **`run.end_on_measurement` is dead config.** Assigned at `src/reactor/controller.py:99` and referenced nowhere else in the repo. | The documented primary run-end condition is not wired to the flag that claims to control it. Either wire it or delete the key. |
| R5 | **MED** | **`start_now()` bypasses the temperature gate** (`src/reactor/controller.py:205-212`). | A manual start can inject reagent into a cold reactor. |
| R6 | **MED** | **Volume limits and flow faults are only checked while `running`** — `if self.state == "running":` at `src/reactor/controller.py:1248-1249`. | During `flushing` (the longest phase, 20 min shipped) neither check runs. |
| R7 | **MED** | **`shutdown()` never joins the loop thread or closes the serial ports** (`src/reactor/controller.py:1324-1341`). | On Windows the COM ports stay locked, so the next start cannot find the pumps. |
| R8 | **MED** | **Run records carry no `backend` flag.** `backend` appears only in the `reactor.run_start` event (`:696`) and `status()` (`:1289`), not in the record `_end_run` writes. | A campaign resuming from `manifest.json` cannot tell mock-derived observations from real ones, and will train the GP on both. |

### Manifest & optimizer (`O`)

From the July 29 platform audit; these are the only full descriptions of these
numbers anywhere, which is why the file they came from could not simply be deleted.

| # | Sev | Finding | Consequence & fix |
|---|---|---|---|
| O3 | **HIGH** | **Manifest write cost is O(N²).** Every entry embeds a full `config_snapshot` (`src/manifest.py:695-696`), nothing prunes, and reduction writes once per frame under `flock` with no timeout. Measured 3.5 ms at 100 entries → **609 ms at 20 000**. | By 03:00 the bookkeeping costs more than the science and blocks all six writers. Fix: **batch reduction's per-frame writes** per poll cycle as average/background already do, and drop `indent=2`. **Correction (Sept 2026):** the cause stated here is wrong. The snapshots the apps actually pass are 1-6 keys, ~100-200 bytes (`reduction/app.py:329-333` passes four); the dominant cost is that every write is a full read -> `json.load` -> mutate -> `json.dump` of the whole file under `flock`. Deduplicating configs would barely help. See ../CONTINUOUS_RUN_HARDENING_PLAN.md Phase 4. |
| O4 | **HIGH** | **`flock` failure disables manifest writing silently** — the call is unguarded (`src/manifest.py:274-283`); on NFS/SMB it raises `ENOTSUP` and every caller swallows it into one log line. | Data reduces fine, `.dat` files appear, `manifest.json` is never created: **no provenance for the entire run**. Highest-variance item left. Fix: `except OSError` with a one-time ERROR and a portable `os.mkdir` lock fallback. |
| O7 | **HIGH** | **Only the first rolling batch trains the optimizer.** The average app emits `batch001, batch002…`; `_pending.pop(rid, None)` (`analyzer/app.py:296`) means first-arrival wins — and `batch001` is the *least* settled flow with the worst statistics. | The manifest and campaign history disagree about the same recipe with no marker saying which trained the model. Fix: accumulate per recipe and `tell()` once on the highest-confidence batch. |
| O9 | **MED** | **Subtracted `.dat` drops the sample's metadata footer**, including the simulator's `simulated=1` flag — `background/app.py:215-241` writes only its own header. | Mock and real results are indistinguishable downstream: manifest, campaign history and Slack reports alike. |
| O10 | **MED** | **Switching the project folder mid-session splits the writers.** Monitor loops keep the absolute paths captured at start; the manifest write in the same loop uses the live `_project_root`; reduction writes to `data_directory.parent` — a third manifest. | Data lands in the old folder, provenance in the new. Fix: stop the monitors on `set_project` with a clear log line. |
| O12 | **MED** | **No `fsync` before `replace`** (`src/manifest.py:236`), and salvage handles only the "valid JSON + trailing bytes" case. | On salvage failure the manifest resets to empty and processing continues; 8 h of provenance survive only in a `manifest.corrupt-*` file nothing points the operator at. |
| O14 | **MED** | **`_FAIL_LOSS` rows still enter the GP fit and still consume budget** (`src/optimizer/campaign.py:27,70`). Partially mitigated by the `_transform()` compression at `:107-115`. | One bad fit degrades every subsequent suggestion. Fix: exclude them from the fit; count them separately. |
| O15 | **MED** *(half fixed)* | The `_MAX_RESULTS=600` bound and `_handled` pruning landed (`analyzer/app.py:76`, `:383-384`, `:527-528`). **Residual: the SSE stream still has no sequence-number filter.** | Re-sends summaries the client already has. Reduction's SSE is the model implementation. |
| O16 | **MED** *(half fixed)* | `at_bounds` is now computed (`src/analysis/core.py:734-779`) and persisted (`src/analysis/io.py:82`). **Residual: confidence is still not scaled down when it is non-empty** (`src/analysis/nanoparticle.py:183`). | A railed boundary artefact is still reported to the optimizer at full confidence. |
| O18 | **LOW** | On Windows the manifest lock is a silent no-op — `_HAVE_FCNTL` only, no `msvcrt` (`src/manifest.py`). | Clean, invisible lost updates with six concurrent writers. Windows is a supported platform. Fix: `msvcrt.locking` fallback, or the portable directory lock from O4. |

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
| D3 | **MED** | **Background subtraction science still lives in `background/app.py`** (`_subtract`, `_interpolate_onto`, `_auto_scale`, `_qc_metrics`, `truncate_rebin`, `_write_dat`) with no `src/background/`. The one standing violation of the project's "all logic in `src/`" rule. `docs/design/AUTOPILOT_PIPELINE_DESIGN.md` §4 is the write-up. |
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
| LOW | `/api/browse` returns `{"path": …}` in reduction but `{"current": …}` in the average app — two contracts for one job. |
| LOW | `quality`, `reactor`, `analyzer` and `calibration` post-date the design audit and have **never been contrast-checked**. See `docs/DESIGN_SYSTEM.md` §6. |

---

## Recommended order

1. **O4, then O3** — the silent `flock` failure is the highest-variance item left
   (on an NFS/SMB project folder the run produces no provenance at all and nothing
   says so); then the O(N²) write cost.
2. **R1–R4** — the reactor HIGHs. R3 (unvalidated sign) and R4 (dead config) are
   small, self-contained changes; R1 is a lock-scope refactor.
3. **N1, N3** — the two restart-integrity bugs that re-process a whole night.
4. **O7, O14, O16** — what the optimizer is actually trained on.
5. **N16** — drive the stages off the event bus instead of four 10 s pollers in
   series. Largest latency win (264 s → seconds) and the most invasive.
6. **D3** — move the subtraction science into `src/`.


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
