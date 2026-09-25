# Changelog

Notable changes, newest first. Point-in-time detail lives in `docs/audits/`;
this file is the running summary. Dates are when the work landed on `main`.

---

## September 2026

### Assistant robustness: contain tool failures + retry transient errors

Systematic hardening so a single tool bug can't fail a whole turn: the chat loop
now wraps every tool call — a tool that raises (bug, bad input, missing data) is
caught and fed back to the model as a "Tool error…" result to recover from,
instead of propagating and crashing the turn. The Anthropic client is configured
with max_retries=4 + a 120 s timeout so transient gateway errors (429/5xx/hangs)
retry with backoff rather than failing. Together with the empty-answer guarantee,
tool_use-by-content detection, thread-safe plotting, and the sandboxed run_python,
the assistant degrades gracefully instead of going silent or 500-ing. Held by
`test_assistant_never_empty.py::test_a_raising_tool_does_not_crash_the_turn`.

### Assistant: execute tool_use by content + forceful final answer

Some turns showed the "couldn't compose a summary" fallback. Root cause: the tool
loop only executed tools when `stop_reason == "tool_use"`, but a gateway can
return tool_use blocks with a different stop_reason ("end_turn"/"stop") — the loop
then treated that round as final, dropped the tool_use, and ended with no text.
Now tool_use is detected by CONTENT (any tool_use block runs), so the loop always
completes to a real answer. The forced-summary fallback calls also now append an
explicit "write your final answer now, no tools" instruction and use a generous
token budget, so recovery produces the actual reply instead of the placeholder.
Held by `test_assistant_never_empty.py::test_tool_use_with_wrong_stop_reason_is_still_executed`.

### Assistant never returns an empty answer + plot-kwarg robustness

The assistant sometimes ended a turn with no text ("no response"), and the Porod
plot branch raised on an unexpected `sigma` kwarg. Fixes: chat() now guarantees a
non-empty reply — if the tool loop ends with no text (model returned only tool
blocks, was cut off by max_tokens, or produced whitespace) it forces a tool-less
summary call and falls back to a plain message rather than silence; every plot
function tolerates extra kwargs the model passes (sigma/q_min/Rg…); and
generate_plot clips hand-sliced q/I/sigma to a common length instead of raising.
The streaming error event is also generic now (A3). Held by
`tests/test_assistant_never_empty.py`.

### Assistant plotting made thread-safe (fixes "Too many open files")

The assistant's plot tools failed under load with `matplotlib has no attribute
'get_data_path'` and then `[Errno 24] Too many open files`. Cause: pyplot keeps
global, non-thread-safe state, and the assistant plots on a threaded Flask
server — concurrent turns corrupted matplotlib and a figure orphaned by an error
leaked file descriptors until the process ran out. `src/ai/plots.py` now
serialises every plot entry point behind one lock and closes any half-built
figure on error; the assistant process also raises its file-descriptor soft
limit at startup. Verified: 200 concurrent plots leak no figures and no FDs. Held
by `tests/test_plots_thread_safety.py`.

### Mock SNR raised so the closed loop can converge

The autonomous loop never converged in mock mode even when a run hit the target
radius and PDI: convergence also requires the fit confidence ≥ 0.5, and the mock
subtracted curve was noisy enough at high q that a good fit scored only ~0.14.
Raised the simulator flux (1e6 → 2e7) so a normal acquisition (10×10 s) scores
~0.68 and clears the gate; the loop now converges on target instead of running
the whole budget. `simulate_frame` clip ceiling raised to match. Confidence still
scales with total counts (flux × exposure × frames), so very short acquisitions
stay (correctly) low-confidence. Held by `tests/test_mock_snr_confidence.py`.

### Hub waits for an app to be ready before reporting "Started"

Starting the reduction app showed a section failing to load in the tab and then
recovering a moment later. Cause: `_start_app` returned success the instant
`Popen` returned, but a sub-app isn't serving yet — it still has to import its
(for reduction, heavy: pyFAI/fabio) dependencies and bind the port. Anything that
opened the tab off that signal raced the still-initialising server. The hub now
polls `/api/health` after launch and only reports "Started" once the app answers
(bounded ~25 s; falls back to "still initialising" otherwise, and reports a
startup exit). Also: the reduction SSE indicator starts as "Connecting…" instead
of a false "Disconnected" flash on first load.

### Realistic mock counters + Timer (better demo plots)

The i0/bstop-vs-Timer metadata plot looked degenerate on simulated data: the
mock simulator wrote a flat 1,000,000-count i0 with a clean −0.2%/frame ramp,
`bstop = i0×0.62`, and no Timer, so every sample traced the same line and the
x-axis fell back to file timestamps. The simulator now writes per-sample i0
baseline variation (±1%), gentle beam decay + per-frame shot noise, a fixed
transmission (so bstop tracks i0 while bstop/i0 stays exact), and a real
per-frame `Timer` clock that flows through reduction into the `.dat` footer. The
beam-stability plot is meaningful in demos; real-data behaviour is unchanged.

### Flush no longer doubles in before-mode closed loops

Operator report: the flush after a synthesis ran ~2× the set duration. In
`background_when: "before"` the next condition isn't queued when synthesis ends
(closed loop), so the "flush doubles as the next blank" merge never fired — the
reactor did a full post-synthesis flush *and* then a full pre-synthesis blank
flush, two full flushes per cycle. Fixed: the line is already clean coming out of
the post-synthesis flush, so the pre-synthesis blank now does a short solvent
refresh (`flush.blank_rinse_s`, default 30 s) instead of a second full flush;
arming keeps the capillary clean while the background collection finishes. A dirty
line (cold start / post-abort) still does a full flush. Held by
`tests/test_flush_collapse.py`.

### Order-free reactor/optimiser startup (supersedes R28)

The reactor is now a **pure consumer**: it no longer clears its queue on boot
(shipped `run.clear_queue_on_restart: false`) and runs whatever is queued, in
order, whenever it comes up. The reactor and the optimiser can be started in
**either order** — a cold-start condition written before the reactor boots is
picked up when the watcher goes live, and a mid-campaign reactor restart resumes
the queue. Staleness moved to the producer: the optimiser sets aside leftover
conditions when it starts a NEW campaign
(`analyzer/app.py::_clear_conditions_for_new_campaign`). This fixes the stall
where starting the optimiser first got the first condition swept away as NOT RUN.
The old "every start begins empty" behaviour is still available as an opt-in
(`run.clear_queue_on_restart: true`). Held by `test_orderfree_*` in
`tests/test_reactor_audit_2026_09.py` and `tests/test_optimiser_new_campaign_clear.py`.

Also: the analyzer no longer starves when a stale/empty `Subtracted/Good/` folder
exists — auto gate mode keys on `Good/` having data, else reads the flat
`Subtracted/` folder (`tests/test_analyzer_gate_folder.py`).

### Reactor — deep audit and hardening

A full audit of the reactor app, `src/reactor/`, and `src/beamline/driver.py`
(`docs/audits/REACTOR_AUDIT.md`) found 25 issues plus later operator reports
R26–R29. **25 fixed, 1 accepted (R4), 1 deferred (R12).** Every fix is held by
a test in `tests/test_reactor_audit_2026_09.py`, run against the pre-fix commit
to confirm it catches the old behaviour. Highlights:

- **The rig is handed back on any exit (R1).** `shutdown()` — idle pumps, close
  shutter, release SPEC control — is now wired to SIGTERM/SIGINT as well as
  `atexit`, so stopping the app from the hub (which sends SIGTERM) no longer
  leaves pumps running unsupervised and SPEC locked.
- **Blank-flush recovery (R2).** Stop / E-stop / Vent during a pre-synthesis
  blank flush used to strand the staged condition and silently drop the
  background for every later condition. The staged recipe is now returned to
  the front of the queue.
- **Over-temperature interlock can no longer be blind indefinitely (R3).** The
  staleness alarm is suppressed only while a 2D acquisition is within its
  expected exposure × frames; a hung collect that overruns is reported.
- **Bounded run/collection settings (R8).** Zero and negative run duration,
  flush rate, flush duration and arm-wait are refused with a reason — closing
  the same class of "structurally valid, scientifically empty" hole as the
  Run20 zero-exposure bug.
- **Runtime pump-limit enforcement (R9), and a pump delivering ~nothing is a
  fault (R14).** On the real backend the app refuses to open ports while any
  `sensor_min` is 0.
- **Vent during a run writes the run record (R10); an arm timeout is reported
  and the queue continues (R11).**
- **Operator-facing surfacing:** a persistent fault banner for E-stop failures
  (R5); supervisor / temperature-source honesty on the page (R6); a
  disconnect indicator (R17); controls that say when they did nothing and
  disable when they can't work (R16).
- **Persistence:** saved pump limits and conditions folder load at startup
  (R7); run settings *and* the beamline data-collection settings (exposure,
  frames, trigger-before-end, tags, save folder) survive a restart (R29), and
  the "settings restored" banner names both sets.
- **R4 (accepted):** `read_refresh_cmd` (`ct 0.1`) is now throttled by its own
  `spec.refresh_min_interval_s`, separate from the read cadence. The operator
  kept it at `0.0` (count every read) for a live 1 Hz trace; `sauto off` or
  `read_source: "epics"` is the way to cut the dose. The read interval is back
  at 1 s so the temperature trace is smooth and the interlock reacts fast.

### Autonomous loop — pause, queue and restart semantics

Found by the operator after the audit closed:

- **Stopping autonomous mode now pauses the loop (R27).** `auto_run` was only
  read at intake, so a running campaign chained through the whole queue
  whatever the toggle said. Turning it off now lets the current condition
  finish and flush, then stops at `ready` with the queue kept. The button
  shows a distinct "Pausing after this condition" state.
- **The paused window is when you change beamline settings.** They unlock the
  moment the loop is genuinely stopped; re-arming resumes with the new values.
- **Condition files are the durable queue (R26).** They stay in the watched
  folder until the reactor is *finished* with a condition (ran, abandoned, or
  cleared), so turning autonomous off no longer silently consumes files, and a
  crash re-reads the queue instead of losing it. Intake ties break on filename.
- **A restart begins with an empty queue (R28)** by default
  (`run.clear_queue_on_restart: true`) — leftover files are set aside to
  `done/`, nothing deleted.

### Notifications consolidated

The reactor's "Leaving the beamline" notification card — backend and front end
— was removed. Auto Watch (port 5110) is the sole owner of every platform
notification; `docs/NOTIFICATIONS.md` and `reactor/knowledge.md` updated.

### Shared icon set across the apps and tabs

- A single SVG sprite (`assets/icons/`, built by `tools/build_icon_sprite.py`)
  now supplies each app's browser-tab favicon, its in-app wordmark, the hub
  cards, and in-UI glyphs (folder, start, stop, clear, warning, …). The build
  tool emits the sprite into every app's templates so `<use>` resolves locally.
- Tab favicons no longer cache for a day — they revalidate each load, so a
  rebuilt icon shows on the next tab load. Reduction adopted the alt-b mark.
- In-app emoji with a clean counterpart were converted; emoji with no matching
  glyph, and dynamic JS-built glyphs, were left as-is. The Assistant's inline
  chat was left untouched by request.

### Watchdog (Auto Watch)

- **Subtract ✓ boxes reset per condition.** "background average ✓ / sample
  average ✓" used to stay ticked for every condition after the first; they are
  now scoped to the condition in progress and fill as its own averages arrive.
- Left nav restyled to match the Subtraction app's flat left-accent rail; nav
  icons (Loop / Messages / Alerts) moved to the shared set; joined the shared
  layout spec (its 17 px wall-display base kept, deliberately).

### Average app — folder/path UI

- Folder path boxes now fill their row and show the whole path: fixed a flex
  `min-width:auto` floor, an `align-items:start` that stopped cells stretching,
  and made the folder cards span the full width. Browse became a compact icon
  button, matching Subtraction.
- Templates auto-reload, so UI edits show on a browser refresh without a full
  app restart.

### Subtraction — background scale (context)

The auto scale is a high-q weighted least-squares match (top 25 % window,
MAD-clipped), and the QC "under-subtraction" check reads the same window. Notes
on making the scale more robust for autonomous runs (monitor-anchored scale ≈ 1
with a bounded high-q correction, flatness/non-negativity objectives, and a
per-campaign scale-trajectory flag) are captured for future work — not yet
implemented.
