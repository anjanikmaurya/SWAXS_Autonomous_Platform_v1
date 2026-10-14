# Continuous-run hardening: remediation plan

**Goal:** the platform runs an autonomous closed loop for days without a
restart, a transient read failure, or a concurrent operator click corrupting
the run or silently stalling it.

**Status (checked against the code, 2026-10-14):** N1, N2, N3, N4 and N7 are
**done**; N6 is **mostly done**; N5, N16 and O3 are **open**. The register of
record is [audits/OPEN_DEFECTS.md](audits/OPEN_DEFECTS.md); this document says
how each item was or will be closed, and which test proves it.

| Item | Status | Evidence |
|---|---|---|
| **N1** reduction re-reduces everything after a restart | **Done** | `reduction/app.py` persists the processed set (`_save_processed`) and also skips any `.raw` whose `.dat` already exists (`_already_reduced`). `tests/test_continuous_run_hardening.py::test_the_processed_set_survives_a_restart`, `::test_a_reduced_file_is_not_reprocessed_even_without_saved_state` |
| **N2** `/api/run` races the monitor | **Done** | `/api/run` returns 409 while the monitor is alive, and `_run_lock_guard` refuses a second concurrent one-shot run. `::test_run_is_refused_while_the_monitor_is_active` |
| **N3** average batch state zeroed on restart | **Done** | Batch state persisted through `src/runstate.save_state` (`average/app.py`, `_BATCH_STATE`); a fresh start rebuilds it from disk (`_seed_batch_state_from_disk`, rule in `src/backlog.py`). `::test_batch_state_survives_a_restart_and_a_boot_resume`, `::test_the_resume_path_keeps_batch_state_but_a_fresh_start_clears_it`, `tests/test_fresh_start_no_backlog.py` |
| **N4** batch membership is a count | **Done** | Consumed frames are tracked by filename (`_consumed_for`). `::test_a_missing_frame_does_not_shift_every_later_batch` |
| **N5** average stop/start race | **Open** | `monitor_stop` still only clears the shared `_avg_monitoring` bool and does not join; the loop still ends in `time.sleep(interval)`. See below. |
| **N6** quality dicts mutated while iterated | **Mostly done** | `_results_lock` (RLock) guards `_results`; `_recolor`, `_rescore_all` and `_recount` iterate `_results_snapshot()`. `tests/test_quality_audit_fixes.py::test_recount_survives_concurrent_insertion`, `::test_results_insertion_and_iteration_are_locked`. Residual: `_overrides` is unlocked, records are mutated in place. |
| **N7** every poll re-parses every `.dat` | **Done** 2026-09-11 | `read_folder(skip_names=...)` plus a path-keyed parse cache in `src/plot_reduction.py`. `tests/test_read_folder_cost.py` |
| **N16** stages poll in series | **Open** (partly mitigated) | Average and subtraction default to a 3 s poll since N7. No stage except quality (`file.subtracted`) acts on the upstream bus event. |
| **O3** manifest write cost grows with the manifest | **Open** (partly mitigated) | Still lock, full load, full save, full JSON export per write, and reduction still writes once per frame. Mitigations: `analyses` cap (`SWAXS_MANIFEST_ANALYSES_CAP`), throttled snapshots. See below. |

Related manifest items, tracked in the register: O4 (lock failure on NFS/SMB)
and O18 (no Windows lock) are **open**; O12 (no `fsync`, reset to empty) is
**fixed** by the SQLite store for the default backend.

---

## Why a database was not the fix for N1 to N6

Three of the four HIGH-severity defects were concurrency and state-lifecycle
bugs (N2 shared pyFAI integrator, N3 state zeroed on resume, N6 dicts mutated
while iterated). No storage engine fixes those; persisting the right state,
keyed by identity rather than by count, and serialising the paths that share a
mutable object did. A SQLite **work queue** under the pipeline is still out of
scope. The manifest did move to SQLite in October 2026 (`src/manifest_store.py`),
for crash safety and search, which closed O12 but not O3.

---

## Done: what was changed, briefly

- **N3 + N4 (average).** `_avg_batch_state` holds a set of consumed filenames
  per `(detector, keyword)`, plus a persisted batch counter so numbering never
  rewinds. Saved with `src/runstate.save_state`, restored on boot resume; a
  fresh operator start re-seeds it from the files already on disk instead of
  wiping it (the rule lives in `src/backlog.py`).
- **N1 (reduction).** The processed set is persisted and reloaded, and
  `_already_reduced` skips a `.raw` whose `.dat` exists and is newer. The second
  check needs no state file at all, and honours the configured filename prefixes.
- **N2 (reduction).** Refuse rather than lock: `/api/run` returns 409 while
  `monitor_alive(...)` is true, and `_run_lock_guard` allows one one-shot run at
  a time. Refusing removes the interleaving and is easy to explain in the UI.
- **N7 (average).** Done with `skip_names` instead of the planned list/load
  split: frames already consumed are dropped from the glob before any stat or
  parse. The parse cache is keyed by path, valued `((mtime_ns, size), entry)`,
  LRU-bounded by `SWAXS_READ_CACHE_MAX` (3000). Monitor defaults dropped from
  10 s to 3 s as a result.
- **N6 (quality), mostly.** One re-entrant `_results_lock`, never held across
  the LLM call or the manifest write; every loop iterates a snapshot.

---

## Open items

### N5: average monitor stop/start race

**Root cause.** `/api/monitor/stop` sets `_avg_monitoring = False` and returns
without joining. The old thread is sleeping in `time.sleep(interval)` at the end
of `_avg_monitor_loop`. If `/api/monitor/start` runs before it wakes,
`monitor_alive` sees the flag cleared and starts a second thread; the old one
wakes, finds `_avg_monitoring` set again and keeps looping, so two loops race
the same batch state. Or the old thread exits after the new start and sets
`_avg_monitoring = False`, switching off the monitor that just reported OK.

**Fix.** Replace the shared bool with a **per-run `threading.Event`**: each
loop checks only its own event, stopping sets that run's event, and
`time.sleep(interval)` becomes `stop_event.wait(interval)` so a stop is
immediate. `monitor_start` joins the previous thread (short timeout) before
starting a new one.

**Regression test:** start, stop, immediately restart inside one interval;
after `2 × interval` assert exactly one live loop, the monitor reports running,
and batches are produced.

### N6 residual: quality overrides

`_overrides` is written by request threads without a lock, and records in
`_results` are edited in place (`rec["verdict"] = …`), so a snapshot still hands
out live references. Fix: build new record dicts in the mutators, and guard
`_overrides` with the same lock. Do not hold any lock across the LLM call or
`update_manifest`.

### N16: event-driven triggering, polling as fallback

Every app already has a bus client, but only quality, reactor, assistant and
watchdog subscribe. Have average and background act on the upstream `file.*`
event and keep the poll as the fallback. `quality/app.py::_on_bus_event` is the
reference: it works because the per-file work is one callable,
`_grade_and_record(path, det)`.

- **background, moderate.** `_process_one` is already factored; the decision
  logic and settings in the `_sub_monitor_loop` closure must move to module
  state.
- **average, harder.** Scan, grouping, batch check, averaging, emit and manifest
  batching are all inline in `_avg_monitor_loop`. Do N5 first: both need
  `stop_event.wait(interval)` so an event can wake the loop.

### O3: manifest write cost

**Cause.** Every write is lock → `load_manifest` → mutate → `save_manifest` of
the **whole** manifest. With the SQLite store that is: `manifest_store.load_dict`
rebuilds the full dict, `save_dict` upserts every row of every section in one
transaction, and `_write_json` re-exports all of `manifest.json` with
`indent=2`. Reduction calls `update_manifest` once per frame, and the hub does the same for every bus event (`hub/app.py::_append_event_to_manifest`). (The register's
first diagnosis, large `config_snapshot`s, was wrong: they are 1 to 6 keys.)

**Fix, in order of value:**

1. **Write only what changed.** The `add_*` helpers in `src/manifest.py` know
   the one row they touch; with SQLite that row can be upserted directly
   instead of re-syncing every table.
2. **Batch reduction's per-frame writes** per poll cycle, as the average and
   background monitors already do (one `update_manifest` per cycle).
3. **Throttle or slim the JSON export**: drop `indent=2`, or export at most
   every few seconds, since the DB is the source of truth.

**Gotchas:** the JSON export is what external readers use, so throttling it
must keep the last write of a run. Fix O4 (lock failure on NFS/SMB, WAL on a
network mount) and O18 (no Windows lock) in the same pass; they threaten the
same long-run scenario.

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
- **Always set `SWAXS_NO_RESUME=1` before importing** average or reduction:
  both start `_boot_resume_monitor` daemon threads at import.
- `_dat()`, `_wait()`, `_msgs()` from `tests/test_average_batch_deadlock.py`.
- `tests/test_restart_recovery.py` parametrises app resume; extend it rather
  than starting a new file.
- `tests/test_continuous_run_hardening.py` holds the N1 to N4 regressions; add
  N5 there.

After each change, run the full suite.
Before declaring the goal met, do a **mock-backend soak**: run the closed loop
against the simulator, kill and restart each app mid-run, and assert nothing is
re-processed, nothing is lost, and no duplicate `file.averaged` reaches the
reactor.

---

## Explicitly out of scope

- **SQLite work queue.** See the top of this document.
- **Filesystem notifications** (`inotify`/FSEvents/`watchdog`) as a polling
  replacement — unreliable on the NFS/SMB mounts beamline data arrives on.
  Use the event bus for latency instead (N16).
- The reactor/beamline residual risks — separate register, separate audit.
- N8–N15: real, but none of them break a multi-day run.
