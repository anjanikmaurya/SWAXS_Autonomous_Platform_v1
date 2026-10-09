# Auto-Fit & Optimiser (analyzer) — stability & correctness audit

Scope: `analyzer/app.py` and its deps (`src/optimizer/{gp,campaign,plots}.py`,
`src/analysis/nanoparticle.py`, `src/manifest.py`). Focus: correctness,
effectiveness, and efficiency over **days-to-weeks** of continuous running.

Status: findings A1–A9 below were fixed in the same pass as this audit
(October 2026). Tests added/updated in `tests/test_analyzer_resume.py` and
`tests/test_manifest_loading.py`.

---

## What was already sound (no change needed)

- **Bounded in-memory state.** `_results` (OrderedDict) capped at `_MAX_RESULTS`
  (600) via `_store_result`; `_log` is `deque(maxlen=300)`; `_handled`/`_lastsig`
  pruned to present files and hard-capped at `2×_MAX_RESULTS`.
- **GP cost is bounded.** Campaign history is capped by `budget` (~25) and resets
  per campaign, so the Cholesky (`O(n³)`) never grows across weeks.
- **Self-healing loop.** `_expire_pending` times out proposals whose measurement
  never arrived (`_PENDING_TIMEOUT_S`, 1 h) and advances.
- **Atomic durable writes** (`.part` + `replace`) for campaign records; manifest
  upsert-by-(type, file) avoids duplicate analysis records on re-fit.

---

## Findings and fixes

### A1 (HIGH) — matplotlib was not thread-safe and had no lock  — FIXED
`_write_fit_record` runs in the **watcher thread**; `/api/campaign/plot/<view>.png`
and the campaign-end figures render in **Flask request threads**. pyplot keeps
global state, so concurrent use corrupts figures or crashes over a long run.
**Fix:** one process-wide lock `src/plot_lock.py::MPL_LOCK` (RLock), taken by both
`_write_fit_record` and `src/optimizer/plots.py::figure`. A per-module lock would
not serialise the two against each other, so the lock is shared.

### A2 (HIGH) — figure leak on the error path  — FIXED
`plt.close(fig)` was only reached on success; an exception between `subplots()`
and `close()` leaked a Figure (memory + file descriptors) permanently.
**Fix:** `try/finally: plt.close(fig)` in `_write_fit_record`; `figure()` closes
`plt.close("all")` on error.

### A3 (MED) — unbounded disk growth of `Results/Fit/`  — FIXED
A PNG + `.dat` is written per fit and was never pruned; over weeks this fills the
project volume (which then breaks every savefig / state write).
**Fix:** retention cap `_FIT_RETENTION` (env `SWAXS_ANALYZER_FIT_RETENTION`,
default 5,000 pairs; 0 = unlimited), pruned once per `_FIT_PRUNE_EVERY` (200)
writes so pruning adds no per-fit cost. Full-disk writes were already caught by
the record-writer's `try/except` (analysis continues; a warning is emitted).

### A4 (MED) — watcher swallowed every exception silently  — FIXED
`_watcher` looped `except Exception: pass`, so a persistent fault (malformed
file, permissions, disk full) ran forever invisibly while the app read as
"running".
**Fix:** throttled error reporting (`_WATCH_ERR_THROTTLE_S`, 60 s) — the loop
still survives, but a stuck watcher is now visible in the log.

### A5 (MED) — per-poll cost grew O(N files)  — FIXED
`_watch_once` did `sorted(glob("*.dat"), key=…stat().st_mtime)` — a `stat()` on
every file — every 3 s, climbing as the folder filled.
**Fix:** build `present` from the glob (no stat), then only `stat()` the
**candidates** (not already in `_handled` / `_startup_present`). Files still
mid-write (`_lastsig`, not `_handled`) stay candidates.

### A6 (MED) — manifest write cost grew O(N profiles)  — FIXED
`analyses{}` accrues one entry per distinct profile and the whole `manifest.json`
is rewritten on every fit, so write latency climbed with N.
**Fix:** cap `analyses{}` at `_ANALYSES_CAP` (env `SWAXS_MANIFEST_ANALYSES_CAP`,
default 3000; 0 = unlimited), dropping the oldest by `updated_at`. The durable
per-fit trail lives in `Results/Fit/` regardless.

### A7 (fixed earlier this session) — startup re-fit of pre-existing data
`_startup_present` freezes everything already on disk at startup from auto-fit;
only files that arrive after startup are auto-fit. The operator backfill button
and a running campaign fit pre-existing files on demand.

### A8 (fixed earlier this session) — "343 of 25 used" miscount
Budget counts distinct recipes, not Fit files. `_fit_recs_by_recipe` collapses
records to one per `recipe_id`; both the Continue button count and the resume
replay use it.

### A9 (fixed earlier this session) — operator backfill
`/api/analyze_existing` + "Analyse existing profiles" button fit the pre-existing
backlog on demand, oldest-first, yielding to the live run, without driving the
optimizer.

---

## Known limitations (documented, low priority)

### L1 (LOW) — SSE holds one server thread per client
`/api/stream` holds a werkzeug thread per connected viewer for the life of the
connection. Fine for a single operator; many simultaneous viewers/reconnects
could exhaust the dev-server thread pool. Revisit only if multi-viewer use is
expected (e.g. move to a production WSGI/ASGI server).

### L2 (LOW) — no auto-chaining of campaigns
A campaign that reaches `budget` goes `exhausted` and stops proposing; the
reactor then idles until an operator starts the next campaign. For multi-week
unattended operation, chaining the next campaign is a manual step — note this in
the operator runbook.

---

## Tunables added

| Env var | Default | Effect |
|---|---|---|
| `SWAXS_ANALYZER_FIT_RETENTION` | 5,000 | Max fit-record pairs kept in `Results/Fit/` (0 = unlimited) |
| `SWAXS_MANIFEST_ANALYSES_CAP` | 3000 | Max entries in `manifest.json` `analyses{}` (0 = unlimited) |
| `SWAXS_ANALYZER_BACKFILL_GAP_S` | 0.5 | Pause between backfill fits (yields to the live loop) |
