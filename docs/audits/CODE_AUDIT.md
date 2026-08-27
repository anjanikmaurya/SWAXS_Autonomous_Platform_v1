# Code Audit — hub + 9 apps + `src/`

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
`viewer/app.py:488` passed `n_files = mask.sum()`, which is `np.int64`, not `int`.

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
number** (positive q-points, not frames); the viewer now reports the real count.

### 3. Reduction alone never resumed after a restart

`reduction/app.py:850` referenced `_project_root`, which does not exist in that
module. Every call to `_state_root()` raised `NameError`, and **both** callers
swallowed it:

* `_persist_monitor_state` → `save_monitor()` was never reached, so the state was
  never written;
* `_boot_resume_monitor` → the resume attempt died before it began.

The restart-recovery work was therefore inert for reduction while working in every
other app. Observable state after a 03:00 restart: viewer averaging, background
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
status route and the already-running guard in reduction, viewer, background and
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
reactor ships `spec.frames: 10`; the viewer UI ships `frames_per_average: 30`. The
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
frame, the viewer's folder re-read ~1 ms per file, one analyzer fit 246 ms. Nothing
there is slow; the chain is dominated by four independent 10 s pollers in series
plus the batch wait. Reducing that means driving the stages off the event bus
instead of polling — recorded as **N16** below rather than changed here, because it
alters the timing of every stage at once.

---

## Redundancy

An AST scan compared every same-named helper across the ten app files. Most
duplication is unavoidable (each app owns its own Flask routes), but three cases
were real:

| | Finding | Action |
|---|---|---|
| a | `monitor_status` / the already-running guard existed in 4 apps and had **drifted** — none checked thread liveness | consolidated into `src/runstate.monitor_alive` (finding 4) |
| b | `_state_root` in 4 apps, 2 distinct implementations; reduction's was the broken one | reduction's fixed to match (finding 3) |
| c | `/api/browse` returns `{"path": …}` in reduction but `{"current": …}` in viewer, with different filtering — two contracts for one job | **left open**: cosmetic, UI-only, and unifying it means touching two templates |

`_persist_monitor_state` is byte-identical in all four apps — genuine duplication,
but harmless and self-documenting where it sits.

---

## Open — not fixed, ranked by expected loss

These were reproduced or confirmed by reading, but are larger than a safe
single-session change. Ranked by what they cost during an unattended run.

| # | Sev | Finding | Consequence |
|---|---|---|---|
| N1 | **HIGH** | `reduction/app.py` `_processed_files` is memory-only, and `find_new_raw_files` filters on nothing else. After a restart it re-reduces the **entire** experiment, oldest first. | Every `.dat` rewritten, thousands of manifest writes under the cross-process lock, and live frames starved until the backlog clears — worse now that the monitor auto-resumes. Fix: persist the set, or skip files whose `.dat` is newer than the `.raw`. |
| N2 | **HIGH** | No mutual exclusion between `/api/run` and the reduction monitor. Both take the same file list and share one `AzimuthalIntegrator` (whose contract says single-threaded), and both write then `replace()` the same `.part`. | An interleaved, truncated `.dat` published atomically — it looks complete to every downstream size/mtime check. Fix: one lock, or reject `/api/run` while monitoring. |
| N3 | **HIGH** | `viewer` `_avg_batch_state` is zeroed on every `monitor_start`, including the boot resume. | After a restart the whole night is re-averaged: every batch file overwritten and one `file.averaged` per batch — which is what the reactor treats as measurement-complete. Fix: persist the batch state alongside the monitor state. |
| N4 | **HIGH** | `viewer` batch state is a **count**, not a set of identities, and `read_folder` silently skips unreadable files. One transient read failure shifts every subsequent batch boundary. | A frame is silently reused in the next batch and one is dropped; if the count ever exceeds the group length, that keyword stops averaging entirely. Fix: track consumed filenames. |
| N5 | **MEDIUM** | `viewer` stop→start within one interval can leave two loops racing the same counter, or the old thread can switch the **new** monitor off after `start` returned OK. | Double-written or skipped batches; or a monitor that reports running and is not. Fix: per-run `threading.Event` instead of a shared bool; join the old thread. |
| N6 | **MEDIUM** | `quality` `_results` / `_overrides` are mutated by the grader thread, the bus thread and request threads while other requests iterate them. | `RuntimeError: dictionary changed size during iteration` — tolerable in `/api/results`, but `_recolor()` and `_rescore_all()` have side effects, so a mid-loop failure leaves half the profiles re-verdicted and re-copied, permanently, with nothing logged. Fix: snapshot under `_lock`. |
| N7 | **MEDIUM** | `viewer` re-reads and re-parses **every** `.dat` in the Reduction folder on every poll (default 10 s), with no mtime filter. | At 10 000 files the loop falls progressively behind the acquisition it is tracking. Fix: cache by `(path, mtime, size)`. |
| N8 | **MEDIUM** | `calibration` Stop does not stop the SFTP copy — the executor drains its queue and the handle is dropped anyway. | Status says "Stopped" while an orphan thread keeps writing; a second Start can run two syncs into the same `.part` paths. Fix: `shutdown(cancel_futures=True)`; keep the handle until the thread is dead. |
| N9 | **MEDIUM** | `calibration` never persists/resumes its transfer — the O1 fix skipped the first stage of the pipeline. | A restart silently stops data ingest; every downstream app then honestly reports "no new files" and stays green. Note: only key-based auth can be resumed, since the password is deliberately never persisted. |
| N10 | **MEDIUM** | `quality` "Re-grade with AI" is stored in `_overrides`, so it is recorded as `source="user", overridden=True` and pins the verdict forever. | Provenance claims a scientist labelled the profile; later threshold changes can never reclassify it. Fix: a separate `_ai_verdicts` store. |
| N11 | **MEDIUM** | A single override shifts the **global** adaptive threshold, persists it, and does not re-sort. Repeat overrides of one file are counted repeatedly. | The persisted threshold stops matching the already-sorted files. Fix: `_recolor()` after `_adapt_threshold()`; key `_adapt` by path. |
| N12 | **LOW** | Five legacy `analysis` routes duplicate the current ones and have drifted numerically (`/api/peak` forces `n_peaks ≥ 1`; `/api/waxs_peaks` auto-detects). Only the new ones produce `Analysed/` files and QC. | Two "peak fits" of the same curve disagree with no explanation. Fix: delete them (the template calls neither) or make them wrappers. |
| N13 | **LOW** | `analysis` Guinier overlay is drawn over the whole q-range, ignoring `res["q_range"]`. | The saved `*_fit.dat` shows an extrapolated curve that reads as a much worse fit than it is. |
| N14 | **LOW** | Both reduction and viewer drive `pyplot` from a 2-worker pool; the global figure manager is not thread-safe. | Two simultaneous render requests can garble a figure. UI-only. |
| N15 | **LOW** | `src/analysis/atsas.py:122` `mkdtemp()` per `run_datgnom` call, never removed. | One temp dir per file in an ATSAS batch. |
| N16 | **MEDIUM** | Four stages poll independently at 10 s, in series, plus a 2-poll stability gate — 99 % of the 264 s frame-to-fit latency is waiting, 1 % is CPU (measured). Every app already publishes and receives bus events. | A frame takes ~4 min to reach the optimizer when the work costs ~2 s. Fix: have each stage act on the upstream `file.*` event and keep polling only as the fallback it was meant to be. |

Also still open from the earlier audits: O3 (manifest write cost is O(N²)), O4
(silent `flock` failure on network shares), O7 (first-batch-only optimizer
training), O12–O16, O18.

---

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
