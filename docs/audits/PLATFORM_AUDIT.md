# Platform-Wide Audit — Autonomous Beamtime Readiness

Scope: all 9 hub apps, the shared `src/` layer, the hub itself, and the closed
loop reduction → averaging → subtraction → quality → analyzer → optimizer →
reactor. Two dimensions were treated as the priority: **silently wrong science**
(a plausible but incorrect number reaching the optimizer) and **silent stoppage**
(the pipeline dying without anyone noticing).

Every defect below was **reproduced before being fixed**. 346 tests pass.
The earlier reactor-specific audit is in `REACTOR_SAFETY_AUDIT.md`.

---

## Fixed — silently wrong science

### 1. Every frame in a run read the SAME CSV metadata row

`find_row_number_to_read` searched the **whole path** for `_NNNN.`, and
`re.search` returns the leftmost match. A project folder like
`/data/Auto_Run_0002.5/…` matched `_0002.` in the directory name.

Reproduced: `…/Auto_Run_0002.5/2D/SAXS/x_sample_scan1_0007.raw` → row **2**, not 7.

Consequence: identical `i0`/`bstop` for all ~200 frames, so every
`normalization_factor`, transmission and derived thickness was wrong by the
beam's decay over the night — and the `i0_filter_pct` outlier filter was
defeated, because every frame reported the same I0. Intensities stayed smooth and
plausible; the shape-based quality gate saw nothing.

**Fixed** — match on `Path(...).name` (`src/reduction/process_metadata.py:29`).

### 2. The ML truncation fabricated ~74 % of every profile

`np.interp` holds the edge value flat outside the source range. The shipped
default window is 0.03–0.6 Å⁻¹ (= 0.3–6.0 nm⁻¹), but a 3 m camera reaches only
~1.8 nm⁻¹ — so 404 of 549 grid points were a **constant invented plateau**.

Measured effect through the real analyzer: PDI off by **2×** at R = 2 nm
(0.047 vs 0.099), and confidence corrupted in both directions. Since the campaign
loss weights PDI directly, that is a ~0.33 loss error — comparable to the entire
size term near target.

**Fixed** — the grid is clipped to the measured range, a non-overlapping request
raises instead of inventing, and the clip is logged
(`background/app.py:155-175`).

### 3. A truncation failure mislabelled nm⁻¹ data as Å⁻¹ → 10× wrong radius

`_apply_truncation` swallowed every exception and returned the untouched nm⁻¹
arrays, but the column label was computed **afterwards from the config flag**. So
the file said `q_A-1` while holding nm⁻¹ data, and the analyzer's unit conversion
multiplied q by 10 — reporting a 4 nm particle as 0.4 nm for the rest of the night.

**Fixed** — `_apply_truncation` returns an `applied` flag and the label is derived
from what was actually written; the failure is now logged
(`background/app.py:178-201`).

### 4. A sample could be subtracted against a DIFFERENT recipe's background

When a sample carried a `recipe_id` but that recipe's own blank was absent (failed
collection, not yet averaged), the code fell through to a nearest-index heuristic
over **every** background in `Averaged/` — including previous recipes and previous
nights, since that folder is never cleaned.

Reproduced: `…_b222_sample_…` paired with `…_a111_bkg_…`.

**Fixed** — when the background pool is recipe-keyed (i.e. an autonomous
campaign), a missing own-blank returns `None` and the monitor retries next cycle
rather than pairing across recipes. Manual datasets, where no blank carries a
recipe key, keep the nearest-index behaviour — there is a test for each
(`background/app.py:395-425`).

### 5. The Quality Gate was not in the data path at all

The gate **copies** profiles into `Good/` and `NeedsReview/` and leaves the
original in place. The analyzer watched the flat `Subtracted/` folder, so **every
rejected profile was still fitted and fed to the Bayesian campaign**. The verdict
was written to `manifest["quality"][…]["analysis_ready"]` and nothing in the
repository ever read it.

**Fixed** — the analyzer now analyses `Subtracted/Good/` whenever it exists
(`gate: "auto"`), so a rejected profile can never reach the fit. `gate: "good"`
forces it; `gate: "off"` restores the old advisory behaviour, with a warning. The
mode is settable via `POST /api/folder {"gate": …}` (`analyzer/app.py:79-118`).

---

## Fixed — silent stoppage

### 6. A crashed app was silently dead for the rest of the night

Nothing watched the subprocesses. The hub called `poll()` only to render a dot,
and never acted on a transition to dead — no log, no alert, no restart. A
reduction crash at 02:00 meant frames kept landing, nothing processed them, the
campaign advanced zero steps, and the card simply showed grey "Stopped".

**Fixed** — `_detect_crashes()` runs on every 2 s status tick, logs
`APP CRASHED: <app> exited with code N — see logs/<app>.log` at ERROR, publishes
an `app.crashed` bus event, and the hub card now reads
**⚠ CRASHED (exit 3) · logs/reduction.log · press Start** (`hub/app.py:395-430`).

### 7. A hung app read "Starting…" forever

`running && !healthy` was labelled `Starting…` with no time bound, so a wedged app
was visually identical to a normal 3 s startup — nobody investigated.

**Fixed** — after 20 s it becomes **Not responding · 47s · check logs/<app>.log**
with a red dot.

### 8. Two conditions that silently break a run were invisible

The event bus being down disables remote fit reports and the
measurement-complete signal (runs then end on the timer instead of on the data),
and the hub **hid its only indicator when it was off**. There was also no
free-space check anywhere in the codebase.

**Fixed** — the hub renders a persistent red banner for either condition; the
status payload now carries `event_bus` and `disk_free_gb`.

### 8b. Automated subtraction never subtracted anything

Reported from the beamline, then reproduced in seconds. `decide_intake()` returns
`"skip" | "wait" | "go"`. The background monitor compared it to a string the
function never returns:

```python
if decide_intake(rp, sig, {}, _sub_lastsig) != "handle":   # always True
    _sub_lastsig[rp] = sig
    continue                                               # every file, forever
```

So the stability gate rejected 100 % of samples. **The whole automated arm of the
loop was dead:** nothing reached `Subtracted/`, so the quality gate had nothing to
grade, the analyzer had nothing to fit, and the optimizer was never told anything —
an autonomous campaign could run all night and produce zero measurements.

It was invisible for the same reason it was severe: the reject path logged nothing.
The app printed `▶ Auto-subtraction started`, the status card stayed green at
`0 subtracted`, and there was no error anywhere to search for. The analyzer and
reactor watchers branch on the same three verbs correctly — only this call site
had drifted, and no test exercised the monitor thread end to end.

**Fixed** — branch on the real verbs (`skip`/`wait`/`go`); pass the real
`_sub_done` as the *handled* map, and make it a `dict` of signatures rather than a
`set` of paths so an average the viewer rewrites in place is subtracted again
instead of being ignored forever.

**Also fixed, because silence was half the bug:**

- a per-detector heartbeat whenever the split changes —
  `… [saxs] 4 .dat in the watched folder → 2 sample(s), 2 background(s)`, logged as
  a **warning** when either count is zero (which is what a typo'd keyword filter
  looks like);
- a once-per-sample explanation when a sample is skipped —
  `⏳ auto_A_sample…: waiting for the blank of recipe 'auto_A' (1 background(s)
  present, none from this recipe)`. In a campaign the sample average is written
  before the flush completes, so waiting is *normal*; it just has to be legible,
  and it must not repeat every poll for hours.

`tests/test_background_auto_monitor.py` runs the real monitor thread against real
`.dat` files: one sample + its blank → exactly one output (not zero, not one per
poll), a rewritten average is redone, keyword mode works for datasets whose blank
matches none of the built-in background tokens, and the log says what it is doing.
It also asserts directly that no watcher compares `decide_intake` to a verb it
never returns.

### 8c. Closing an app did not close its process (O11, O17)

Reported as "if I close the apps then close any running process — start that app
from fresh". Four separate defects sat behind it, in `hub/app.py`:

| | Was | Now |
|---|---|---|
| Stop an app the hub did not spawn | returned **"Not running"** and left it alive — an orphan from an earlier hub run kept polling folders and writing files while the UI said Stopped | the port is the ground truth; if the holder identifies as that app it is killed |
| Stop | returned as soon as `wait()` did, so an immediate restart could race the socket teardown | waits for the port to be released, then reports |
| Start with the port held | refused: *"Port 5003 is already in use — free that port and retry"*, unfixable from the UI | reclaims the port from our own orphan and starts fresh, saying so |
| Ctrl-C / SIGTERM on the hub | `sys.exit(0)` relying on `atexit`, which does not run reliably from a signal handler while Flask's threads are alive — nine apps kept running | the handler stops every app, then `os._exit` |

Children are now spawned in their own process group (`start_new_session` /
`CREATE_NEW_PROCESS_GROUP`) so the whole tree can be signalled, and
`logs/hub_children.json` records `app_id → pid`. A hub that is **SIGKILLed**
cannot clean up — but the next hub reads that file at boot, kills any leftover
that still identifies as the same app, and starts clean (**O11**). Logs are
rotated to `<app>.log.1` instead of truncated, so a restart no longer destroys the
traceback that prompted it (**O17**).

**Availability is decided by a real bind, never by the process table.** The first
version of this check refused to start the hub because macOS AirPlay Receiver
(`ControlCenter`) listens on port 5000 — even though Flask had been binding
127.0.0.1:5000 alongside it for months. `can_bind()` now asks the kernel the same
question the server will ask (including `SO_REUSEADDR`, so a `TIME_WAIT`
connection from the app just stopped does not read as "busy"), and `listeners()`
is used only to *explain* and to reclaim. It is restricted to LISTEN sockets on
loopback or the wildcard, so a service on another interface — or an outbound
connection whose ephemeral local port happens to equal 5003 — is not mistaken for
a holder. When the port genuinely cannot be bound, the hub says so, names the
holder, mentions AirPlay on macOS, and offers `SWAXS_HUB_PORT=5100`.

A zombie counts as dead. `psutil.wait_procs` reports one as alive until its real
parent reaps it, and an orphan we kill is not our child — so every reclaim burned
the full grace period. A hub restart over a running hub went from **8 s to 0.3 s**.

**Safety.** `reclaim_port` kills only a process whose command line references the
app's entry file or the project root *and* the app's own directory. A database, an
editor or another user's server on one of our ports is reported by name and left
alone — killing by port number would be a foot-gun on a shared beamline
workstation. When the owner cannot be identified at all (no `psutil`, or the OS
hides it) it refuses rather than guesses.

New in the UI: **⟲ Restart** per card, **■ Stop all**, and **🔌 Ports** — which
names the PID and command line holding each port, so a refused start explains
itself instead of sending the operator to `lsof`.

Logic lives in `src/proc_lifecycle.py`; `tests/test_hub_lifecycle.py` spawns real
processes on real ports and covers all of it, including the SIGKILL-then-restart
sequence. Marked `slow`; run with `pytest -m slow`.

### 8d. Pressing Stop reported "⚠ CRASHED (exit null)"

Seen on the hub cards and reported from the beamline. Nothing had crashed.

`_stop_app` sets `_procs[aid] = None`, and the crash detector computed

```python
running = proc is not None and proc.poll() is None
```

so a deliberate Stop was indistinguishable from a running→dead transition. It
then read the exit code off the handle it had *already discarded*, got `None`, and
the card rendered that as the word **"null"** — an alarming message, about an
event that never happened, containing no usable information. Meanwhile a *real*
crash showed only a bare number.

**Fixed** — a crash is reported only when the hub still holds the handle and that
process exited by itself. A stop clears the edge state and any stale badge; a
start clears it immediately rather than a tick later.

**And a crash now says something useful.** `_exit_reason` renders `exit 1` or
`killed by SIGKILL` (never "null"), the spent handle is reaped so the code is
real, and the last dozen log lines are captured and shown **inline on the card** —
`FileNotFoundError: poni/atT_SAXS.poni` is on screen instead of behind a file path.

Three more robustness defects found while verifying it:

- **The status stream had no error handling and one line silently disabled it.**
  Far below `connectStatus()`, a leftover
  `es.onerror = () => log('SSE connection lost — retrying…', 'err')` **overwrote**
  the reconnecting handler with one that only wrote a log line. After a hub
  restart the page froze on its last frame and kept displaying minutes-old state
  with no warning — a status display that lies is worse than one that is
  obviously broken. Removed; the handler now reconnects with backoff, and a
  banner appears when no update has arrived for 9 s.
- **An exception in one status tick killed the generator**, closing the SSE
  stream for good. Each tick is now guarded, reports `hub_error`, and backs off.
- **Two HTTP requests per app per tick, to the same endpoint** — 18 requests
  every 2 s with nine apps, each able to block for the full timeout, so a few
  wedged apps could push a tick past its own interval. One `_health_probe` now
  serves both, and `/api/status` and the SSE stream share one status builder
  (they had drifted: the stream reported crashes, the snapshot did not, so a page
  load right after a crash showed a plain "Stopped").

Verified by booting all nine apps through the real endpoints: every one starts,
reports healthy, and stops cleanly with no false crash. `tests/ui/hub_ui_check.js`
drives the rendered page in jsdom (20 assertions — "null" never reaches a card, a
signal death reads as `killed by SIGKILL`, the tail is shown, recovery clears it,
a dropped stream raises the banner) and `tests/test_hub_crash_reporting.py` covers
the server side.

---

## Fixed — restart recovery (O1, O2, O5, O6, O8)

### 9. Nothing resumed after a restart — now the data apps do

`src/runstate.py` stores small, frequently-changing state in
`<project>/.swaxs_state/` with temp+rename (deliberately **not** the manifest,
which is an expensive locked read-modify-write).

Each data app (reduction, viewer, background, quality) persists its
`monitor/start` body via an `after_request` hook and replays it through the **same
endpoint** on boot — so there is no second copy of the argument parsing to drift.
Guards: state older than 48 h is ignored, `SWAXS_NO_RESUME=1` disables it, a
monitor the operator *stopped* is not resumed, and a double-resume is treated as
success rather than logging a false failure.

**The reactor is deliberately different.** Auto-run moves pumps, so it is **not**
resumed. It reports *"auto-run was ON before this restart. It is NOT resumed
automatically because it moves pumps — check the rig, then press Start"* and waits
for a human. `run.resume_auto_run: true` opts in to full unattended recovery.

### 10. The Bayesian campaign survives a restart

`campaign.json` holds the hyperparameters, full history, pending proposals and the
analyzer's `handled` map. On boot the controller is rebuilt and the history
replayed through `tell()` — verified to reproduce **identical GP state** (losses
match to 6 dp). Replay never re-emits condition files or notifications. Restoring
`handled` is what stops a restart re-analysing every existing profile, which would
have appended duplicate manifest entries (a fresh UUID each) and fired duplicate
notifications. A converged/exhausted campaign is not restarted.

If nothing is pending after a restore, it proposes the next condition immediately
— so the loop self-heals instead of waiting for a human.

### 11. A lost measurement no longer stalls the loop until morning

`_pending` entries now expire (`SWAXS_PENDING_TIMEOUT_S`, default 1 h). On
timeout the proposal is recorded as a **failed measurement** —
`tell(params, None, None, 0.0)`, already the documented path — and the next
condition is proposed.

### 12. A run can no longer finish with no data

`advance_on_new_file` is blocked until the 2D collection has fired. With the
shipped 600 s duration / 180 s lead the sample collect is due at T+420 s while
`min_dwell_s` is 60 s, so a queued condition could previously end the run with
nothing measured — which then stalled the campaign.

### 13. All three `.dat` writers are atomic

Reduction (which wrote the file, then appended the metadata footer in a **second**
open), the averaged writer, and the subtracted writer now all write to `.part` and
`os.replace`. A `.dat` therefore never exists without its footer, and a watcher
can never read a half-written profile — which used to parse as a *shorter valid*
curve, permanently clipping the batch's q-range. The background monitor also gained
a size+mtime stability gate for files written by anything else.

---

## Open — NOT fixed, ranked by expected loss

These are real, reproduced, and deliberately left for a decision because each
changes behaviour or needs a design choice.

| # | Severity | Issue | Why it matters | Suggested fix |
|---|---|---|---|---|
| ~~O1~~ | **FIXED** | **Nothing resumes after a restart.** All five automation loops (reduction watch, auto-average, auto-subtract, quality grader, campaign) start only from an operator POST and persist no "was running" flag. The hub *does* restore the project folder, so the platform looks correctly restored while doing nothing. | A 03:00 restart leaves every automation off. Frames accumulate, nothing is processed, all cards are green. | Persist each `monitor/start` body to `<project>/.<app>_monitor.json`; re-issue on startup. Surface `monitoring: true/false` in `/api/health` and on the hub card. |
| ~~O2~~ | **FIXED** | **The Bayesian campaign is memory-only.** `_campaign` and `_pending` are module globals in the analyzer with no persistence. | An analyzer restart silently ends the closed loop: the fit still runs and writes to the manifest, but `_feed_campaign` returns early, no new condition is written, and the reactor idles until morning. | Dump `{target, tolerance, budget, history, pending}` next to the manifest on every `tell`; rehydrate by replaying `history` on startup. |
| O3 | **HIGH** | **Manifest write cost is O(N²).** Every entry embeds a full `config_snapshot`, nothing prunes, and reduction writes once per frame under `flock` with no timeout. Measured: 3.5 ms at 100 entries → **609 ms at 20 000**; `Demo_Data/manifest.json` is already 4.2 MB. | By 03:00 the bookkeeping costs more than the science and blocks all six writers — reduction falls behind the detector. | Store each distinct config once under a top-level `configs` key and keep only `config_hash`; batch reduction's writes per poll cycle as viewer/background already do. |
| O4 | **HIGH** | **`flock` failure disables manifest writing silently.** The call is unguarded; on a network share (macOS + NFS/SMB → `ENOTSUP`) it raises, and every caller swallows it into one per-app log line. | Data reduces fine, `.dat` files appear, `manifest.json` is never created. No provenance for the entire run. | `try/except OSError` with a one-time ERROR log and a portable `os.mkdir` lock fallback. |
| ~~O5~~ | **FIXED** | **No size+mtime stability gate in the viewer, background or quality watchers** (only the reactor and analyzer have one), and reduction writes `.dat` non-atomically — pyFAI writes the file, then the metadata footer is appended in a *second* open. | A `.dat` read mid-write parses as a shorter valid profile; one short frame clips the whole batch's `q_max` in `_common_q_grid`, and the file is marked done forever. Nothing retries. | Reuse the existing, tested `decide_intake` in all three loops; write to `.part` and `os.replace` after the footer. |
| ~~O6~~ | **FIXED** | **A lost measurement stalls the loop until morning.** `_pending` never expires and `_advance_campaign` only proposes after a `tell`. The reactor writes `<recipe_id>.done.json` and **nothing in the repo reads it**. | Any recipe that produces no subtracted file deadlocks both halves silently. | Timestamp `_pending`; on expiry call `tell(params, None, None, 0.0)` — already the documented failed-measurement path — and advance. |
| O7 | **HIGH** | **Only the first rolling batch trains the optimizer.** The viewer emits `batch001, batch002…`; `_pending.pop(rid)` means first-arrival wins — and `batch001` is the *least* settled flow with the worst statistics. Later batches are still written to the manifest. | The manifest and campaign history disagree about the same recipe, with no marker saying which trained the model. | Accumulate per-recipe and `tell()` once on the highest-confidence batch. |
| ~~O8~~ | **FIXED** | **`advance_on_new_file: true` + `min_dwell_s: 60` can end a run before the 2D collection fires** (due at T+420 s with the shipped 600 s/180 s). | A synthesis that ran but has no data — which then hits O6 and deadlocks. | Refuse to advance early until `_spec_fired`, or clamp `min_dwell_s ≥ duration − spec_lead_s`. |
| O9 | **MEDIUM** | **Subtracted `.dat` files drop the metadata footer**, including the simulator's `simulated=1` flag. | Mock and real results are indistinguishable downstream — in the manifest, the campaign history and the Slack reports. | Re-emit the sample's footer in `_write_dat`. |
| O10 | **MEDIUM** | **Switching the project folder mid-session splits the writers.** Monitor loops keep the absolute paths captured at start, but the manifest write inside the same loop uses the live `_project_root`. Reduction writes to `data_directory.parent` — a third manifest. | Data lands in the old folder; provenance lands in the new one. | Stop the monitors on `set_project` with a clear log line, or resolve folders from the live root each cycle. |
| O12 | **MEDIUM** | **A truncated or zero-byte manifest resets to empty and processing continues.** Salvage handles only the "valid JSON + trailing bytes" case, and `save_manifest` doesn't `fsync` before the rename. | 8 h of provenance survive only in a `manifest.corrupt-*` file nothing points the operator at. | `fsync` before `replace`; on salvage failure load the newest backup instead of an empty manifest. |
| O13 | **MEDIUM** | **The quality gate's LLM adjudication has no effect** — `rec["verdict"]` is overwritten one line later by `_effective_verdict`. And `_grader_loop` re-grades every file every cycle, so every borderline profile triggers a paid API call every interval all night. | Cost with no benefit; the code comment ("LLM has final say") is false. | Either honour the adjudication or drop the call; add a graded-file cache. |
| O14 | **MEDIUM** | **A failed measurement inflates the GP's noise 19×** (`_FAIL_LOSS = 1e3` enters `var(y)`), shifting proposals, and still consumes budget. | One bad fit degrades every subsequent suggestion. | Exclude `_FAIL_LOSS` rows from the GP fit; count them separately. |
| O15 | **MEDIUM** | **Unbounded result stores.** The analyzer re-serializes *every* summary once per second over SSE; at 3 000 profiles that is a multi-MB payload per second. `_results`, `_handled`, quality's `_results` never prune. | Growing memory and CPU across a 24 h run. | Bound to ~500 entries; send only summaries newer than the client's sequence number (reduction's SSE is the model implementation). |
| O16 | **MEDIUM** | **Fit parameters rail to their bounds with no flag** (a railed `PDI = 0.600` was reproduced). | A boundary artefact is reported to the optimizer as a measurement. | Add `diagnostics["at_bound"]`; scale confidence down when non-empty. |
| O18 | **LOW** | On Windows the manifest lock is a silent no-op with six concurrent writers → clean, invisible lost updates. | `RUNNING_ON_WINDOWS.md` exists, so this is a supported platform. | `msvcrt.locking` fallback, or the portable directory lock from O4. |

---

## Verified correct (spot-checks worth knowing)

- **Manifest writes are atomic** — unique temp name (PID + random) in the same
  directory, then `os.replace`. `ENOSPC` mid-`json.dump` cannot corrupt it.
- **Stale lock files are harmless** — `flock` is released by the kernel when the
  holder dies.
- **Child port collisions are pre-checked** with an actionable message naming the
  port — the best-handled item found.
- **`.raw` writes are atomic** (`.part` + `replace` + short-write check), and
  `read_detector_image` hard-fails on a size mismatch rather than padding.
- **Non-positive i0/bstop skip the file** rather than emitting a plausible-wrong
  `.dat`; overlapping normalisation terms are collapsed with a warning.
- **Recipe-id extraction** (leftmost role tag, longest-id-first matching) behaves
  as documented; a background file is never treated as a sample.
- **Simulator isolation** — synthetic frames cannot land in a folder holding real
  `.raw` files, every written folder is marked, and the simulator is constructible
  only under `backend="mock"`.
- **Blank-before-synthesis ordering** works and is tested; one flush serves as
  both clean-out and the next blank.
- **Reduction's monitor loop is built for multi-day operation** — integrator
  reused, per-file exceptions contained, exponential backoff, `gc.collect()` per
  cycle. Its SSE implementation (bounded per-client queue, slow-client drop,
  bounded replay, `finally` cleanup) is the pattern the other apps should adopt.
- **Matplotlib figures are closed on every path checked** — no figure leak.
- **The event bus degrades without spinning**, and quality + the reactor's folder
  watcher have polling backstops.

---

## Recommended order for what remains

1. **O3 + O4** — manifest write cost (measured 609 ms locked writes at 20 k
   entries) and the silent `flock` failure on a network share. O4 is the
   highest-variance item left: on an NFS/SMB project folder the run produces no
   provenance at all and nothing says so.
2. **O7** — only the first rolling batch trains the optimizer, and it is the
   least-settled one.
3. **O10 + O11** — project-folder switching and orphaned children.
4. **O13 + O14 + O16** — wasted LLM calls, GP noise inflation from failed fits,
   and unflagged railed fit parameters.

O1, O2, O5, O6 and O8 are done, so a transient fault no longer silently costs a
whole night. What remains degrades quality or performance rather than stopping
the run.
