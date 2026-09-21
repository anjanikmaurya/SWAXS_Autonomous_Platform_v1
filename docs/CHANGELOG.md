# Changelog

Notable changes, newest first. Point-in-time detail lives in `docs/audits/`;
this file is the running summary. Dates are when the work landed on `main`.

---

## September 2026

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
