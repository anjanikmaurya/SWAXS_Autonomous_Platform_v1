# Reactor App — Safety Audit

Scope: `src/reactor/` (controller, hardware, recipe), `src/beamline/`,
`src/simulator/`, `reactor/app.py`. Two focus areas: **mock↔real isolation** and
**control-logic correctness**.

Every defect below was **reproduced before being fixed**, and each has a
regression test in `tests/test_reactor_audit_fixes.py` (19 tests).

---

## Verdict

Isolation was **architecturally sound but unsafe at the boundaries**. The
simulator is instantiated in exactly one place (`MockBeamline.__init__`, guarded
by `simulator.enabled`), and `SpecBeamline` never touches it. The danger was in
string handling, default paths, and unguarded transitions — all now closed.

---

## CRITICAL — fixed

### 1. `SWAXS_REACTOR_BACKEND=REAL` gave a LIVE beamline with SIMULATED pumps

The pump layer compared `backend == "real"`; the beamline compared
`.lower() == "real"`. Reproduced:

| value | beamline | pumps |
|---|---|---|
| `real` | SpecBeamline | RealPump |
| `REAL` | **SpecBeamline** | **MockPump** ← split |
| `Real` | **SpecBeamline** | **MockPump** ← split |
| `real ` | MockBeamline | RealPump (after the first fix) ← split |

Real X-rays, real shutter, real detector files — while the operator believed the
pumps were simulated. The mirror case gave a fully simulated session to someone
who believed the rig was live.

**Fixed:** `.strip().lower()` + membership validation in `reactor/app.py` (exits
rather than guessing), `PumpBank.__init__`, `ReactorController.__init__`, and
`make_beamline`. All four now resolve identically.

### 2. A pump raising killed the control loop, leaving reagents flowing unsupervised

`PumpBank.set_all()` was unguarded and `_loop()` had no top-level handler.
Reproduced: with pump 3 of 5 failing, the loop thread **died**, `state` stuck at
`arming`, and two pumps stayed commanded at 48 and 8 µL/min. From that moment
there was no `_safety_check` (no over-temperature, over-pressure, volume or
flow-fault detection), no run deadline, no flush, no cooldown.

**Fixed:** `_loop` wraps each tick and E-stops on any escape; `set_all` is
guarded per-pump and returns the failures; `_enter_running` E-stops if any pump
refused its setpoint (a partial command means the wrong composition).

### 3. Venting silently cleared a latched E-stop

`vent_all()` unconditionally set `state = "idle"`. Venting is the natural reflex
after a fault; with auto-run on, the folder watcher then submitted the next
recipe straight back into the unresolved fault.

**Fixed:** the E-stop is latched through a vent (`reset()` is the deliberate way
out), and `estop()` now also disables `auto_run`.

### 4. The simulator could overwrite real beamtime data

Default `mock_data_dir: ""` meant mock output went to the hub project folder —
the same physical directory SPEC writes to — using the **identical** filename
convention, with `tmp.replace(path)` silently clobbering collisions. Synthetic
frames were forensically indistinguishable from real ones.

**Fixed:** three layers —
- `assert_safe_to_simulate()` refuses to write into any folder containing
  unmarked `.raw` files;
- every folder written gets a `SIMULATED_DATA.txt` marker;
- the metadata CSV carries a `simulated=1` column, so provenance survives into
  the reduced `.dat`.

---

## HIGH — fixed

| # | Defect | Fix |
|---|---|---|
| 5 | `switch_backend` installed the new pump bank **before** building the beamline/temp; a failure there (e.g. `requests` missing — it is not in `requirements.txt`) escaped with REAL pumps open while `self.backend` still said `mock` | Build pumps + beamline + temp *all* before swapping anything; close partial state and return an error |
| 6 | No interlock against an in-flight collection; `_fire_spec_collection` resolved `self.beamline` inside its worker thread, so a mock-dispatched collect could execute on real hardware | `is_collecting()` guard on `switch_backend`; the beamline and backend are bound once at thread entry and the collect aborts if they changed |
| 7 | `MockBeamline` had no `_do_close`, so a simulated acquisition kept writing `.raw` files into the watched folder after switching to real | `_do_close()` sets the simulator stop event |
| 8 | A dead temperature source froze `current` at the 25 °C default, making `current > T_max` permanently false — the thermal interlock was **silently disabled** | `TempController.stale` / `age_s()`; `_safety_check` reports staleness loudly during arming/running/flushing, and trips the E-stop if `safety.temp_stale_estop: true` |
| 9 | Aborting during `arming` emitted a `<recipe_id>.done.json` with `status: "ran"` carrying the **previous** run's `measured_flows` and `flow_series` — the optimizer would train on a synthesis that never happened | `_end_run` returns without a record when the pumps never started; `_begin_next` clears the measurement buffers |
| 10 | `/api/estop` returned `{"ok": true}` even when pumps failed to idle | `estop()` returns the failed list; the route reports `ok: false` + `failed_to_idle` |
| 11 | `POST /api/recipes_folder` raised `NameError: _watch_seen` (renamed to `_watch_handled`/`_watch_lastsig`) — the folder change was applied but never persisted | Clear the correct caches |

---

## Verified CORRECT (traced, no defect)

- The simulator is constructed in **one** place, guarded by `simulator.enabled`;
  `SpecBeamline` has no `simulator` attribute and inherits `set_recipe` /
  `set_project_root` as genuine no-ops.
- No cross-wiring of transports: `import serial` only inside `RealPump`,
  `import requests` only inside `SpecBeamline`. Mock objects cannot perform
  hardware I/O; `SpecBeamline` reads no mock counters.
- `idle_all` / `zero_pumps` guard each pump independently — one dead port cannot
  prevent the others from stopping.
- `estop()` records `state = "estop"` *before* touching hardware, so a throwing
  serial write cannot leave the system unlatched; it is reachable from every state.
- `estop()` deliberately sends nothing to SPEC, so a pump emergency stop cannot
  corrupt an in-progress X-ray acquisition.
- Pumps are matched by **serial number**, refusing to fall back to a stale COM
  port — the "reagent through the wrong line" hazard is genuinely closed.
- `recipe_to_setpoints` **rejects** rather than clamps out-of-window setpoints,
  and rejects NaN/±inf. Nothing reaches hardware on rejection.
- `flow_settle_s` cannot mask a sustained fault: it re-arms only on an actual
  setpoint change, and the only in-run write is the single `set_all`.
- The folder watcher waits for size+mtime stability, so a half-written condition
  file is never parsed.

---

## Residual risks — NOT yet fixed

Ordered by severity. None are new; all predate this audit.

1. **E-stop latency (HIGH).** `_end_run` holds `self._lock` across
   `update_manifest` (a cross-process `flock` with no timeout, shared with five
   other apps) and the cooldown `set_temperature` (which blocks on the SPEC lock
   for the whole acquisition, up to `cmd_wait_s: 600`). Measured E-stop latency
   with a blocking manifest: **7.8 s**. Fix: dispatch manifest/feedback/cooldown
   off-lock, and have `estop()` idle the pumps before acquiring the lock.
2. **Serial retry latency (HIGH).** `Py_P_Pump.read_status` retries 5× at ~1 s
   each while holding the per-pump lock, so a hung pump delays E-stop ~5 s and
   stretches the loop period. Fix: fewer retries, shorter timeout, and a
   pre-emptible abort flag.
3. **Negative flows accepted (HIGH).** `set_run_settings` / `flush_now` do not
   validate sign; `flush_rate: -500` reaches the pump and the clamp is one-sided.
   `arm_wait_s: -99` skips timed arming entirely.
4. **`run.end_on_measurement` is dead config (HIGH).** Assigned but never read —
   any `file.averaged` event ends the run, including one from re-averaging an
   old dataset.
5. **`start_now()` bypasses the temperature gate (MEDIUM)**, not just a timer.
6. **Volume limits and flow faults are not checked during `flushing` (MEDIUM).**
7. **`shutdown()` never joins the loop thread or closes serial ports (MEDIUM)** —
   on Windows the COM ports stay locked until the process fully exits.
8. **Run records carry no `backend` flag (MEDIUM)** — a campaign resuming from
   `manifest.json` cannot distinguish mock-derived observations from real ones.

---

## Pre-beamtime checklist

1. `spec.simulator.enabled: false` — **verify it**, do not assume.
2. `SWAXS_REACTOR_BACKEND` unset or exactly `real` (the app now refuses anything else).
3. Confirm the Mock/Real pill reads **Real** after switching, and that the log
   line says `pumps + beamline are LIVE`.
4. Confirm the temperature reading is changing — a frozen value means the
   interlock is blind (you will now see a STALE warning).
5. Confirm `spec.data_dir` resolves to the real beamline path, not a local folder.
