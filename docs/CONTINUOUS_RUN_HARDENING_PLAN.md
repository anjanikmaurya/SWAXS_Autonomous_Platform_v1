# Continuous-run hardening — remediation plan

**Goal:** the platform runs an autonomous closed loop for days without a
restart, a transient read failure, or a concurrent operator click corrupting
the run or silently stalling it.

**Status: Phases 1 and 2 are DONE** (N1, N2, N3, N4 — September 2026, before
the beta hand-off), with regression tests in
`tests/test_continuous_run_hardening.py`. N5 is still open, and Phases 3–4
(N6, N7, N16, O3) are unimplemented. Every defect below is open
in [audits/OPEN_DEFECTS.md](audits/OPEN_DEFECTS.md); this document says how to close each
one, what will bite while doing it, and what test proves it is closed.

---

## The headline: a database is not the fix

The obvious-sounding upgrade — put a SQLite work-queue under the pipeline —
addresses **one** of the eight problems below (N7, folder-scan cost) and would
leave the other seven exactly as they are. Three of the four HIGH-severity
defects are *concurrency and state-lifecycle* bugs:

- **N2** — two code paths sharing one pyFAI integrator
- **N3** — state zeroed on the boot-resume path
- **N6** — dicts mutated while another thread iterates them

No storage engine fixes those. What fixes them is **persisting the right state,
keyed by identity rather than by count, and serialising the paths that share a
mutable object.** That is this plan.

SQLite becomes worth revisiting at tens of thousands of files per campaign, or
when several independent consumers must not double-process the same file.
Neither applies today.

---

## What actually breaks, at 3am

Ordered by expected loss during an unattended run, which is also a sensible
implementation order — each phase is independently shippable and testable.

| Phase | Defect | The 3am symptom |
|---|---|---|
| 1 | **N3** | A restart re-averages the whole night, emitting one `file.averaged` per batch — which the reactor reads as *measurement complete*. It advances the campaign on stale data. |
| 1 | **N4** | One unreadable frame shifts every later batch boundary. A frame is silently reused, another dropped; if the count outruns the group, that keyword stops averaging **forever**, silently. |
| 1 | **N1** | A restart re-reduces the entire experiment, oldest first. Thousands of manifest writes under the cross-process lock; live frames starve until the backlog clears. |
| 2 | **N2** | An operator clicks *Run* while the monitor is running. Both drive the same `AzimuthalIntegrator` (documented single-threaded) and both `replace()` the same `.part` → a **truncated `.dat` published atomically**, indistinguishable from a good one downstream. |
| 2 | **N5** | Stop→start inside one poll interval: the old thread wakes, exits, and sets `_avg_monitoring = False` — switching off the monitor that just reported OK. |
| 3 | **N6** | `RuntimeError: dict changed size during iteration` inside `_recolor()`, which moves files as it goes. Half the profiles end up re-verdicted and physically re-sorted into the wrong folder. Nothing is logged. |
| 3 | **N7** | Every poll re-parses every `.dat`. At ~10 000 files the loop falls behind the acquisition it is tracking. |
| 4 | **N16 / O3** | ~264 s frame-to-fit latency, 99% of it waiting; manifest writes grow superlinearly. Slow, not broken. |

---

## Phase 1 — state that survives a restart

### N3 + N4 — average batch state: persist it, and key it by identity

**Root cause.** `_avg_batch_state` (`average/app.py:67`) maps `(det, keyword)`
to an **integer count** of frames consumed. The monitor slices
`grp[consumed:consumed + n_per_batch]` (`:633`) out of a freshly re-globbed
list, and `batch_no = consumed // n_per_batch + 1` (`:634`) names the output
file. `monitor_start` rebinds it to `{}` (`:777`) on every start.

Two independent failures fall out of that: a reset regenerates `batch001…` and
overwrites the night's work (N3); and because `read_folder` silently skips
unreadable files (`src/plot_reduction.py:308-311`), one bad read shortens the
list and shifts every subsequent slice (N4).

**Fix.**

1. Change the value from `int` to a **set of consumed filenames**. Batch
   membership becomes "the frames in this group I have not yet consumed",
   which is invariant under a file going missing.
2. Derive `batch_no` from a persisted counter per key, not from `len(consumed)`,
   so numbering never rewinds.
3. Persist via the existing `src/runstate.save_state` / `load_state`
   (generic dict in/out, atomic tmp+replace, never raises) under a
   `average_batch` name, alongside the monitor state the app already writes.
4. Make the reset at `:777` conditional: **restore** on a boot resume, clear on
   a fresh operator start.

**Gotchas.**

- `_boot_resume_monitor` (`:1040`) replays the saved body through the *same*
  `/api/monitor/start` endpoint that does the zeroing, and the body is
  byte-identical to an operator click. The endpoint cannot currently tell the
  two apart — add an explicit `resume: true` flag to the replayed body, or a
  module-level `_resuming` guard set by the boot thread.
- JSON keys must be strings; the state dict is keyed by the tuple `(det, kw)`.
  Encode as `f"{det}|{kw}"` or nest by detector.
- A persisted count is meaningless if `frames_per_average` changed between
  runs. Storing filenames sidesteps this — another reason for the set.
- `_avg_batch_state` is unlocked and touched by the monitor thread and by
  request threads. Phase 2 (N5) adds the lock; until then, keep all mutation
  inside the monitor thread.

**Regression test** (`tests/test_average_batch_deadlock.py` is the closest
harness — reuse its `_dat()`, `_wait()` and the `_average_app(tag, tmp_path,
monkeypatch)` loader, which already sets `SWAXS_NO_RESUME=1`):

- *N3:* write 3 batches' worth of frames, run the monitor, stop it, reload the
  module (simulating a restart), resume — assert **no** `_batch001` file is
  rewritten and **no** second `file.averaged` fires for an already-averaged
  batch.
- *N4:* write N frames, make one unreadable mid-run (chmod or truncate), and
  assert the following batch contains the *correct remaining* frames — not a
  shifted window — and that no frame appears in two batches.

### N1 — reduction: persist the processed set

**Root cause.** `_processed_files` (`reduction/app.py:213`) is a memory-only
set, and `find_new_raw_files` (`src/reduction/core.py:746-778`) filters on
nothing else — no mtime, no size, no check for an existing output. An empty set
means every `.raw` in the tree is new.

**Fix.** Persist the set through `src/runstate` on the same
`@app.after_request` hook the app already uses for monitor state, and reload it
at startup. Prefer **relative** paths (relative to `data_directory`) over the
raw `str(f)` keys, which are unresolved and break if the project is remounted
at a different path.

A cheaper belt-and-braces alternative, worth adding *as well*: skip any `.raw`
whose corresponding `.dat` exists and is newer. It needs no persistence at all
and covers the case where the state file is lost.

**Gotchas.**

- `_processed_files` is mutated in **two places** — the monitor
  (`reduction/app.py:589,604`) and `run_pipeline` in `src/`
  (`src/reduction/core.py:842,865`) — so a save hook must cover both, or the
  set must move behind a small accessor.
- Reduction has no `_project_root` global (unlike average/background);
  `_state_root()` (`:861`) falls back to `SWAXS_PROJECT`, while the manifest is
  written relative to `data_directory.parent` (`:324`). These can differ
  (tracked as O10) — the state file and the manifest could land in different
  trees. Resolve this first or the persistence is unreliable.
- No test currently loads `reduction/app.py` as a module. The first one must
  set `SWAXS_NO_RESUME=1` before import, because `_boot_resume_monitor` starts
  a daemon thread at import time (`:910`).

**Regression test:** reduce a folder, reload the module, and assert the second
run reports zero new files and rewrites no `.dat`.

---

## Phase 2 — stop the two racing paths

### N2 — serialise `/api/run` against the monitor

**Root cause.** `/api/run` (`reduction/app.py:414-475`) spawns a worker thread
with **no guard at all**. Both it and the monitor call `_get_experiment(config)`
(`:437`, `:542`) — which returns a *shared cached* `Experiment`, hence a shared
pyFAI `AzimuthalIntegrator` whose contract is single-threaded — and both write
then `replace()` the same `.part`.

**Fix.** A module-level `_processing_lock`, acquired around the per-file
integrate-and-publish step in **both** paths. Not around the whole run: the
monitor's `while` loop must acquire per cycle, or per file, so an in-flight
one-shot cannot block it indefinitely.

Simplest correct version, and the one to prefer: **reject `/api/run` with 409
while `monitor_alive(_monitoring, _monitor_thread)` is true** (`:506` shows the
predicate already in use), and take a lock for the remaining case of two
concurrent `/api/run` posts. Refusing is honest, easy to explain in the UI, and
removes the interleaving entirely.

**Gotchas.**

- `_run_stop_event` (`:219`) is a single global cleared at `:426`. A second
  `/api/run` cancels the first run's pending stop. Fix alongside, or the
  rejection above makes it moot.
- An app-level lock does not protect a second reduction **process**. The hub
  starts one, but `_boot_resume_monitor` also drives the app through
  `app.test_client()`. Cross-process safety would need the manifest's `flock`
  pattern applied to the `.part` write.
- Giving `/api/run` its own `Experiment` would fix the integrator race but
  re-introduce the slow PONI reload the cache exists to avoid. Not recommended.

**Regression test:** start the monitor, POST `/api/run`, assert 409 and that no
`.part` is left behind. Separately, drive two concurrent processing calls at
one `Experiment` and assert every produced `.dat` parses and has the expected
row count — the truncation signature.

### N5 — average monitor stop→start race

**Root cause.** `/api/monitor/stop` sets `_avg_monitoring = False`
(`average/app.py:795`) and returns **without joining**. The old thread is
sleeping in `time.sleep(interval)` (`:702`); it wakes, exits its `while`, and
at `:704` sets `_avg_monitoring = False` again — switching off the *new*
monitor that `monitor_start` just enabled and reported OK for.

**Fix.** Replace the shared bool with a **per-run `threading.Event`**. Each
loop owns its own event and checks only that; stopping sets that run's event.
The old thread can no longer affect the new one. `monitor_start` should also
join the previous thread (with a short timeout) before starting a new one, and
`time.sleep(interval)` becomes `stop_event.wait(interval)` so a stop is
immediate rather than up to one interval late.

**Regression test:** start, stop, immediately restart inside one interval;
assert after `2 × interval` that the monitor still reports running **and** is
producing batches.

---

## Phase 3 — degradation over hours

### N6 — quality: snapshot under lock, and do not hold the lock across I/O

**Root cause.** `_results` and `_overrides` (`quality/app.py:70-71`) are
written by the grader loop, the websocket bus thread, and request threads,
while `_recolor()` (`:408`) and `_rescore_all()` (`:420`) iterate them **and
move files on disk as they go**. A mid-iteration `RuntimeError` leaves the
sort half-applied, permanently, with nothing logged.

**Fix.** Two-step, and the second step is the one that matters:

1. Iterate a **snapshot** (`list(_results.items())` taken under a lock), never
   the live dict.
2. Because records are mutated **in place** (`rec["verdict"] = …` at `:356`,
   `:412`, `:433`, `:679`, `:753`), a snapshot still hands out live
   references — compute `_public(r)` *inside* the lock for read paths, and have
   the mutators build new record dicts rather than editing shared ones.

**Gotcha — do not simply wrap everything in the existing `_lock`.** That lock
(`:64`) is already held for the lifetime of an SSE connection (`:873`), and
`_grade_and_record` (`:338`) performs a **network LLM call** (`:349`) and a
cross-process `flock`'d `update_manifest` (`:379`). One coarse lock would
serialise the whole app behind a paid API call. Add a separate, narrow
`_state_lock` that is never held across I/O.

**Regression test:** run `_recolor()` on a large `_results` while a second
thread inserts records; assert no exception and that every record ends in a
folder matching its final verdict.

### N7 — average: index by signature, parse lazily

**Root cause.** `read_folder` (`average/app.py:596`, once per detector per
poll) fully parses **every** `.dat` — `read_dat_data_metadata`
(`src/utils/read_dat_metadata.py:9-73`) iterates every line in Python and
`float()`s ~1000 rows per file — and all of it is discarded except the frames
in a completed batch. There is no cache of any kind in the app.

**Fix.** Split `read_folder` in `src/plot_reduction.py` into:

- a **list step** — filename, `scan_idx`, keyword, `(size, mtime_ns)`: enough
  to group frames and decide whether a batch is complete, at one `stat` per
  file;
- a **load step** — parse only the frames in a batch that is about to be
  averaged.

`quality/app.py:483` (`_graded`, resolved path → `(size, mtime_ns)`) and
`background/app.py:67,71` are both working models of the signature memo.

**Gotcha:** `average_batch` (`:640`) consumes the full frame dicts, so a cache
that keeps parsed arrays for every file is a real memory bound at 10 000 files.
Lazy parse, do not cache arrays.

**Regression test:** a folder of N files, two consecutive polls with no
changes; assert the second poll performs zero full parses (count calls via
monkeypatch) and still produces identical batches.

---

## Phase 4 — speed, once correctness holds

### N16 — event-driven triggering, polling as fallback

Four stages poll at 10 s in series plus a 2-poll stability gate: ~264 s
frame-to-fit, 99% waiting. Every app already has a bus client; only quality,
reactor and assistant subscribe.

**Fix.** Have average and background act on the upstream `file.*` event and
keep the poll as the fallback it was meant to be.
`quality/app.py:461-478` is the reference implementation — it works because
quality's per-file work is already a callable, `_grade_and_record(path, det)`.

**Effort is asymmetric, and this is why it is last:**

- **background — moderate.** `_process_one(...)` (`:1183`) is already factored,
  but the surrounding decision logic and the monitor's settings live in a
  closure (`_sub_monitor_loop`, `:1309`); they must be lifted to module state.
- **average — harder.** There is *no* factored per-file function; the scan,
  grouping, batch check, averaging, emit and manifest batching are all inline
  in `_avg_monitor_loop` (`:574-706`), which is also stateful across cycles
  through `_avg_batch_state`. **Do N3/N4/N5 first** — extracting this loop
  before its state is fixed means doing the work twice.

Both loops need `stop_event.wait(interval)` instead of a bare sleep so an event
can wake them (the same change N5 needs).

### O3 — manifest write cost

**The register's stated cause is wrong, and the fix follows the real one.**
O3 attributes the O(N²) to `config_snapshot` embedded per entry. In fact the
snapshots the apps pass are small — 4 keys from reduction
(`reduction/app.py:329-333`), 1–6 elsewhere, ~100–200 bytes. The dominant cost
is that **every write is a full read → `json.load` → mutate → `json.dump`
(with `indent=2`) of the entire file** under `flock`. Deduplicating configs
would barely help.

**Fix, in order of value:**

1. **Batch the writes.** Reduction calls `update_manifest` once *per frame*
   (`_register_reduced`, `:295-348`, from the callback at `:452` and the
   monitor at `:591`/`:606`). Average already batches a cycle's mutations into
   one call (`average/app.py:688-699`) — copy that pattern.
2. Drop `indent=2` on save (`src/manifest.py:220-242`), roughly halving bytes
   written.
3. Only then consider a top-level `configs` key with entries holding
   `config_hash` alone. Note `run_id` is a fresh uuid4 per entry and can never
   be deduplicated.

**Gotchas:** the schema change in (3) means `_migrate_to_v2` (`:727`) and every
consumer of `provenance.config_snapshot` — including the assistant, which reads
the whole manifest — must tolerate its absence. Also note `save_manifest` does
not `fsync` before `replace` (O12), and `manifest_lock` (`:260`) has no
timeout and no Windows fallback (O4/O18); both are worth fixing in the same
pass since they threaten exactly the same long-run scenario.

---

## Verification strategy

**A failing test first, for every defect.** These bugs appear only after a
restart, a race, or thousands of files — precisely the conditions a normal test
run does not reproduce. A test written after the fix proves nothing about
whether the bug was real.

Harness already available and worth reusing rather than reinventing:

- `_load(tag, path)` via `spec_from_file_location` — the isolated-app-import
  pattern repeated in six test files; it is the closest thing to a process
  restart, which is exactly what N1/N3 need.
- **Always set `SWAXS_NO_RESUME=1` before importing** average or reduction —
  both start `_boot_resume_monitor` daemon threads at import
  (`average/app.py:1056`, `reduction/app.py:910`).
- `_dat()`, `_wait()`, `_msgs()` from `tests/test_average_batch_deadlock.py`.
- `tests/test_restart_recovery.py` already parametrises app resume, but its
  list currently holds **only** quality (`:84-86`) — extend it rather than
  starting a new file.

After each phase, run the full suite (462 tests, 2 skipped at time of writing).
Before declaring the goal met, do a **mock-backend soak**: run the closed loop
against the simulator, kill and restart each app mid-run, and assert nothing is
re-processed, nothing is lost, and no duplicate `file.averaged` reaches the
reactor.

---

## Explicitly out of scope

- **SQLite work-queue.** See the top of this document.
- **Filesystem notifications** (`inotify`/FSEvents/`watchdog`) as a polling
  replacement — unreliable on the NFS/SMB mounts beamline data arrives on.
  Use the event bus for latency instead (N16).
- The reactor/beamline residual risks — separate register, separate audit.
- N8–N15: real, but none of them break a multi-day run.
