# Reactor Control-Button Audit

Scope: the Flow Synthesis app control panel (`reactor/templates/index.html`),
its Flask endpoints (`reactor/app.py`), and the state machine behind them
(`src/reactor/controller.py`). This audits what each control **does**, the
states it is valid in, what it **should be used for**, and where two controls
overlap or an endpoint is unreachable.

## The reactor state machine (context)

```
idle ──Start──▶ arming ──(temp stable / timed wait)──▶ running ──run ends──▶ flushing ──▶ ready
  ▲                │                                                            │
  └──── Reset ◀─ estop ◀── EMERGENCY STOP (from any state) ; Vent → idle (from any state)
```

Every button ultimately acts on the 5 pumps (4 reagent + 1 flush/solvent). The
thing that distinguishes the "make it stop" controls is **not** the pump action
(several call the same `idle_all()`), but the **state they leave you in** and
**what they preserve** (current recipe, pending queue, auto-run).

## Per-button reference

| Button (label) | Endpoint | Controller method | Valid in states | Effect | Intended use |
|---|---|---|---|---|---|
| ＋ Run manually (add to queue) | `/api/recipe` | `submit()` | any (queues) | Validates + enqueues one recipe. Auto-starts **only** if Auto-run is on and system is idle/ready. | Hand-enter a single condition to run. |
| ▶ Run autonomously (toggle) | `/api/auto_run` | `set_auto_run()` | any | Flips auto-run; when on, the next queued/dropped recipe starts automatically. | Hand the reactor to the folder/ML loop. |
| Start / ⏩ Start pumps now | `/api/start` **or** `/api/start_now` | `start()` / `start_now()` | idle/ready → `start`; arming → `start_now` | Begins the next queued recipe; **during arming** the same button skips the remaining arming wait and starts the pumps now. Relabels automatically. | Begin a run; or override the arming wait. |
| ■ Stop → flush | `/api/abort` | `abort()` | arming, running, flushing | arming/running → stop reagents and go to **flush**; **flushing → idle** (stops the flush). | Cleanly end the current run and flush the line. |
| Flush now | `/api/flush` | `flush_now()` | idle, ready | Runs the flush/solvent pump at the set rate/duration. | Clear the line between runs. |
| Reset | `/api/reset` | `reset()` | estop, ready | Idles all, returns to **idle**. | Clear an E-stop (or a finished "ready") back to idle. |
| 🟦 Vent all pumps | `/api/vent` | `vent_all()` | any | `idle_all()` (P0 → chamber pressure 0), state → idle, **keeps** the queue and auto-run. | Release chamber pressure without abandoning the run plan. |
| 🛑 EMERGENCY STOP | `/api/estop` | `estop()` | any | `idle_all()`, state → **estop**, clears the current recipe. Locks out Start until Reset. | Fault / danger — stop everything immediately. |
| 🗑 Clear queue | `/api/queue/clear` | `clear_queue()` | any | Empties pending recipes (does not touch a running one). | Drop queued conditions. |
| Tare — Pressure / Flow / Both (per pump) | `/api/tare` | `tare_pump()` | idle, ready, estop | Zeroes the pump's pressure and/or flow sensor. | Calibrate a pump while stopped. |
| Apply limits (per pump) | `/api/pumps` | `set_pump_limits()` | any | Updates per-pump min/max flow used for validation + dashboard bars. | Constrain a pump's allowed flow range. |

## Uniqueness / overlap findings

**1. Two orphaned endpoints — dead controllers with no button.**

- `stop()` / `POST /api/stop` — an operator "manual stop of the running
  synthesis → flush" (reason `"manual stop"`, valid only in `running`). **No
  button calls it.** The "■ Stop → flush" button calls `abort()` instead.
- `prime()` / `POST /api/prime` — identical mechanism to Flush, just a different
  label/log tag (`kind="prime"`). **No button calls it**, yet the panel's help
  text (index.html) tells the user about a "Prime" button that doesn't exist.

**2. `stop()` and `abort()` are near-duplicates.** Both end a run and go to
flush. Differences: `abort()` also works during `arming` (and, during
`flushing`, idles instead), and logs reason `"aborted"`; `stop()` only works in
`running` and logs `"manual stop"`. The UI uses `abort()` exclusively, so the
`stop()` semantics never occur.

**3. `Flush now` and `Prime` are the same action** (`flush_now(kind=...)`) with
only a cosmetic label/log difference.

**4. Four controls call the same `idle_all()`** — E-stop, Vent, Reset, and
Abort-during-flush. They are *behaviourally distinct*, but only by their
resulting **state** and **what they preserve**, which the labels don't make
obvious:

| Control | Ends in state | Keeps queue? | Keeps current recipe? | Re-arm needed? |
|---|---|---|---|---|
| EMERGENCY STOP | estop | yes | no | must Reset first |
| Vent all pumps | idle | yes | no | no (Start again) |
| Reset | idle | yes | — | no |
| Stop → flush (during flush) | idle | yes | — | no |

**5. "■ Stop → flush" is mislabelled for one of its states.** During `flushing`
it *stops* the flush and idles — the label still says "→ flush", which is the
opposite of what happens in that state.

## Recommendations

1. **Resolve the stop/abort duplication.** Either delete `stop()` +
   `/api/stop` as dead code (recommended — `abort()` already covers the UI need),
   or, if a distinct "graceful stop vs abort" is genuinely wanted, wire a second
   button and make the semantics different in a meaningful way. Keeping one
   endpoint reachable and one dead invites drift.
2. **Decide Prime's fate.** ✅ Resolved: `prime()` + `/api/prime` removed and the
   "Prime" mention deleted from the help text (priming is not used as a separate
   operation — Flush covers the need).
3. **Disambiguate the "stop/safe" cluster with tooltips**, e.g.:
   - EMERGENCY STOP — "Fault/danger. Idles all pumps and locks the panel until
     Reset."
   - Vent — "Release chamber pressure between runs. Keeps your queue; press Start
     to continue."
   - Reset — "Clear an E-stop (or finished run) back to idle."
4. **Make the E-stop → Reset recovery explicit** in the UI (e.g. after E-stop,
   highlight Reset as the only way forward).
5. **Fix the "Stop → flush" label per state** — relabel to "Stop" generally, or
   swap it to "Stop flush" while in the `flushing` state.

## Verdict

The primary run controls (Start/Start-now, Run manually, Run autonomously) are
well-scoped and the context-aware Start is good UX. The gaps are: **two
unreachable endpoints** (`stop`, `prime`) — one of which the help text
advertises — a **stop/abort duplication**, and a **cluster of "make it safe"
buttons whose real differences (resulting state, what's preserved) aren't
conveyed by their labels.**
