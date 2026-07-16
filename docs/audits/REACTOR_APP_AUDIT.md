# Reactor App — Full Audit

Scope: the Flow Synthesis app end-to-end — the Flask shell (`reactor/app.py`),
the control logic (`src/reactor/`), the beamline layer (`src/beamline/`), the UI
(`reactor/templates/index.html`), and `reactor/config.yml`. Covers correctness,
safety, the closed-loop role, config surface, test coverage, and open items.
Companion to `REACTOR_CONTROLS_AUDIT.md` (button-level).

## 1. Architecture

Thin Flask shell + all logic in `src/`, matching the platform rule:
- `reactor/app.py` — routes, SSE stream, conditions-folder watcher, hub event bus.
- `src/reactor/controller.py` — the `ReactorController` state machine (run/flush/
  arming/estop), the single owner of run state and the control loop.
- `src/reactor/hardware.py` — `PumpBank` (mock/real Mitos pumps) + `TempController`
  (temperature gate, now backed by the beamline).
- `src/reactor/recipe.py` — recipe validation + setpoint conversion.
- `src/reactor/intake.py` — folder-watcher stable-file decision.
- `src/beamline/driver.py` — SPEC/bServer layer (mock/real): temperature, counters
  (bstop/I₀), 2D collection, with a hard serialization guard.

Verdict: clean separation. Pumps and the beamline are independent hardware
channels, which is what lets pump actions run concurrently with X-ray collection.

## 2. State machine

`idle → arming → running → flushing → ready`, with `estop` reachable from any
state and `vent`/`reset` returning to `idle`. Reviewed transitions:
- Arming supports three modes (temperature-gate, timed wait, ramp-from-25 °C);
  the temperature setpoint is commanded once at `_begin_next`.
- A run ends on measurement-complete signal, manual stop/abort, or duration.
- After a run, flush runs for its duration, then advances to the next queued
  recipe or idles. Per-condition state flags (`_spec_fired`, `_bkg_fired`,
  `_meas_series`) are reset at the right transitions (`_enter_running` /
  `_enter_flush`). No stuck-state paths found.

## 3. Safety (reviewed + hardened earlier)

- **E-stop truncation** — `idle_all()`/`zero_pumps()` guard each pump so one dead
  port can't stop the others; `estop()` records the state before touching
  hardware. ✅
- **Lost/hung pump** — `RealPump.tick` flags `fault`+`stale` after repeated poll
  failures so the safety check E-stops instead of trusting stale readings. ✅
- **E-stop latency** — the control loop polls hardware OUTSIDE the lock, so a
  faulting pump can't delay an operator E-stop. ✅
- **Serial routing** — exact serial match (FTDI suffix tolerated), ambiguity
  raises, and startup refuses a possibly-wrong port. ✅
- **safety_check** covers arming/running/flushing: pump ERROR/lost, over-temp
  (`T_max`), per-pump-max, and per-pump pressure ceiling → E-stop. ✅
- **E-stop is pumps-only w.r.t. the beamline** — it sends NOTHING to SPEC and
  leaves temperature as-is, so an in-progress X-ray collection finishes and SPEC
  is never disturbed. ✅

## 4. Beamline / SPEC integration

- Temperature is commanded (`csettemp` ramp) at arming and read back (a counter)
  to gate the pumps; bstop/I₀ are read for the live chart. Reads are throttled.
- Per condition the reactor fires **two** SPEC collects — `{recipe_id}_sample`
  during the run and `{recipe_id}_bkg` during the flush, each `spec_lead_s`
  before its phase ends — tagged so averaging separates them and subtraction
  pairs them by recipe_id (closes the loop with the pipeline).
- **Hard SPEC guard**: one lock serializes all SPEC access; `collect` holds it
  for the whole acquisition, so no command (temperature, a 2nd collect) can
  overlap it. Live reads skip (or, with `read_during_collect`, run concurrently)
  rather than block, keeping the control loop responsive.
- Data collection uses a fillable `.txt` macro run via `qdo` (preferred), or a
  named-command fallback. Exposure / frames / keywords / lead are live app inputs.

## 5. Closed-loop role

Condition intake (folder watcher, stable-file gated) → run → SPEC collect (recipe_id
tagged) → pipeline → analyzer → optimizer writes the next condition. The reactor
writes `<recipe_id>.done.json` feedback including the delivered-flow trace.

## 6. Findings this audit

- **FIXED — backend switch dropped the beamline.** `switch_backend()` rebuilt
  `TempController` without re-passing `beamline=`, so after a pump Mock/Real
  toggle the temperature silently lost its SPEC command/read wiring. Now re-wired;
  regression test added (`test_backend_switch_keeps_beamline_wired`).
- **NOTE — the Mock/Real pill toggles PUMPS only.** The beamline (SPEC) backend
  is set separately in `config.yml → spec.backend`. Intentional (separate
  hardware), but easy to forget: for a real run set BOTH the pill to Real *and*
  `spec.backend: real`. Consider surfacing the beamline backend in the UI later.
- **Dead code (from controls audit): `stop()`/`prime()` removed; the
  "Stop → flush" button uses `abort()`.** ✅ resolved.

## 7. On-rig confirmations (config, not code)

Set in `config.yml → spec:` and verify on the beamline before a live run:
- `backend: real`, `base_url`, and `data_dir` = the pipeline's 2D save folder.
- `temp_counter` (the counter that actually reports temperature).
- `macro_file` (+ `macro_out_file`) → your real collection macro, using the
  placeholders `{path} {recipe_id} {temperature} {exposure} {frames}`; or the
  `collect_cmd`/`newfile_cmd` fallback.
- `read_during_collect` only if the bServer allows counter reads mid-scan.
- Confirm reduction carries the `{recipe_id}_sample`/`_bkg` prefix through to the
  reduced `.dat` names (so the loop's pairing works on live data).

## 8. Test coverage

`tests/test_reactor_safety.py`, `test_beamline.py`, `test_background_pairing.py`,
`test_loop_naming.py`, `test_autopilot_loop.py` cover: guarded E-stop, lost-pump
fault, serial matching, REMOTE-control enforcement, ramp arming, flow-trace in
the done file, sample+background SPEC triggers, the hard SPEC guard (commands
blocked / reads skip / `read_during_collect`), E-stop-is-pumps-only, backend
switch rewiring, recipe_id pairing, and the closed-loop file handshake. Full
suite green (114).

## Verdict

The reactor app is structurally sound, safety is well covered and tested, and the
beamline integration is correct and appropriately guarded. One real bug (backend
switch dropping the beamline) was found and fixed here. The remaining items are
operational config to confirm on the beamline, not code defects.
