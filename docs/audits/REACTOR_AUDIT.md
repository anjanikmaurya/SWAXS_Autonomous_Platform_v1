# Reactor app — deep audit (September 2026)

Scope: `reactor/app.py`, `reactor/templates/index.html`, `reactor/config.yml`,
all of `src/reactor/` (controller, hardware, recipe, config, intake, drivers),
and `src/beamline/driver.py`. Four lenses, all requested: hardware safety on
the real rig, multi-day unattended stability, campaign data integrity, and a
per-control pass over every button and input in the UI.

**Nothing in this document has been fixed.** It is a register for review. Each
finding carries the evidence that produced it, and every finding marked
*proven* was demonstrated by running the code, not by reading it — the probe is
quoted so you can re-run it.

Severity means consequence on a real beamtime, not code tidiness:

| | meaning |
|---|---|
| **HIGH** | can damage the rig or the sample, or can silently destroy a campaign |
| **MED** | loses one condition, or lets the operator believe something false |
| **LOW** | untidiness, or a cost that only shows up over days |

---

## Summary

17 substantive findings, 8 of them proven by execution.

| # | Severity | Lens | One line |
|---|---|---|---|
| [R1](#r1) | HIGH | safety | Stopping the app from the hub never hands the rig back — `atexit` does not run on SIGTERM |
| [R2](#r2) | HIGH | campaign | One Stop during a blank flush permanently kills background collection for the session |
| [R3](#r3) | HIGH | safety | The over-temperature interlock can be blind for hours, with the alarm suppressed by design |
| [R4](#r4) | HIGH | safety | `ct 0.1` fires ~86,400×/day and may open the shutter each time |
| [R5](#r5) | HIGH | UI | The E-stop's "could NOT idle" message lands in a tiny slot in another card, and gets wiped |
| [R6](#r6) | HIGH | UI | A dead supervisor and a placeholder temperature look identical to healthy ones |
| [R7](#r7) | HIGH | persistence | Saved pump limits and conditions folder are never loaded at startup |
| [R8](#r8) | MED-HIGH | safety | Every run-setting except exposure accepts zero and negative numbers |
| [R9](#r9) | MED-HIGH | safety | A pump's own `max_flow` is enforced at intake only, never while running |
| [R10](#r10) | MED-HIGH | campaign | Vent during a run discards the run with no record and no event |
| [R11](#r11) | MED-HIGH | campaign | An arm timeout drops the condition silently and stalls the queue |
| [R12](#r12) | MED-HIGH | campaign | A rejected condition file is a log line only — the optimizer never hears |
| [R13](#r13) | MED | config | The shipped arming default contradicts the documentation |
| [R14](#r14) | MED | safety | Every `sensor_min` is 0, so the documented low-flow rejection can never fire |
| [R15](#r15) | MED | fidelity | Flow-fault detection is 15× faster in mock than on the rig |
| [R16](#r16) | MED | UI | Five controls fail silently; two are never disabled when they cannot work |
| [R17](#r17) | MED | stability | No disconnect indicator — a dead app leaves "running, 240 °C" on screen forever |

Plus eight LOW items in [§ Minor](#minor).

**If only three things get fixed before the next beamtime: R1, R2, R3.**

---

## HIGH

### R1 — Stopping the app from the hub never hands the rig back {#r1}

`reactor/app.py:221`

```python
import atexit as _atexit
_atexit.register(lambda: _ctrl.shutdown())
```

`shutdown()` is the function that idles the pumps, closes the shutter and
releases SPEC remote control so beamline staff can drive SPEC again. It is
wired to `atexit`, and **`atexit` handlers do not run on SIGTERM**. The hub
stops every app with `src/proc_lifecycle.kill_tree`, which sends SIGTERM and
escalates to SIGKILL after 5 s. Neither signal runs `atexit`.

So pressing **Stop** on the reactor card in the hub leaves:

* every pump holding its last commanded flow, with nothing supervising it —
  the control loop is gone, so `_safety_check` is gone too;
* the fast shutter wherever it was;
* SPEC still under this app's remote control, so staff cannot drive it.

Verified: `grep -nE "signal\.(signal|SIGTERM)" reactor/app.py` → no matches.
The watchdog app has exactly the handler this needs (`watchdog/app.py:1816`);
the reactor never got one.

> Fix: install SIGTERM/SIGINT handlers that call `_ctrl.shutdown()` and then
> re-raise the default action, mirroring watchdog. While there, decide whether
> shutdown should wait out an in-flight acquisition — today the process can
> exit in the middle of a 100 s collect and leave partial frames.

---

### R2 — One Stop during a blank flush permanently kills background collection {#r2}

**Proven.** `src/reactor/controller.py:647` and `:934`

With the shipped `background_when: "before"`, starting a recipe stages it in
`self._pending` and runs a *blank* flush first. `_end_flush` is the only code
that clears `_pending`. Every other way out of that flush leaves it set:

```
Stop -> flush (abort)  -> state=idle      _pending='rXYZ'   <-- STRANDED
EMERGENCY STOP         -> state=estop     _pending='rXYZ'   <-- STRANDED
Vent all pumps         -> state=idle      _pending='rXYZ'   <-- STRANDED
Reset                  -> state=flushing  _pending='rXYZ'   <-- STRANDED
```

Two consequences, and the second is the serious one:

1. The staged condition is **lost** — never run, never recorded, never fed
   back. The optimizer waits for a `.done.json` that will not arrive.
2. `_begin_next` only stages a blank when `self._pending is None`. With a
   stranded value it takes the other branch forever, so **no later condition
   ever gets a background**:

```
r002: flush kind='flush' bkg_recipe_id='' -> background collected? NO
r003: flush kind='flush' bkg_recipe_id='' -> background collected? NO
r004: flush kind='flush' bkg_recipe_id='' -> background collected? NO
_pending still holding: r001
```

Nothing clears it. After `reset()` + `vent_all()` + `estop()` +
`clear_queue()` the stranded recipe is still there; only restarting the app
recovers. Meanwhile the run log looks entirely normal — every condition
reports RUN START and RUN END — and the damage only appears much later, in
the background app, as conditions with no blank to subtract.

Probe: `/tmp/probe6.py` in the session; reproduce with
`c.submit(...); c.start(); c.abort()` then inspect `c._pending`.

> Fix: clear `_pending` on every exit from a blank flush, and push the staged
> recipe back onto the front of the queue rather than dropping it. A guard in
> `_begin_next` that logs loudly if `_pending` is unexpectedly set would have
> turned this from a silent campaign-wide failure into one warning line.

---

### R3 — The over-temperature interlock can be blind for hours, with the alarm suppressed {#r3}

`src/reactor/hardware.py:580` (`TempController.stale`) and
`src/beamline/driver.py:155` (`collect`)

`collect()` holds the SPEC lock for the whole acquisition. With
`read_source: "spec"` and `read_during_collect: false` — both shipped
defaults — `read_state()` returns `{}` for that entire window, so
`temp.current` freezes and `current > T_max` cannot become true. That much is
documented and warned about once per run.

What is not bounded is **how long**:

```
shipped acquisition = exposure 10.0s x frames 10 = 100s
_wait() after each macro line uses cmd_wait_s = 600.0 s
macro lines streamed        : 12
worst case lock hold        : 12 x 600s = 2.0 h
```

And for that whole period `stale` is *forced False* by `polling_paused`:

```python
if self.polling_paused:
    return False
```

That suppression is right for a normal 100 s collect and wrong without a
ceiling. A SPEC macro that hangs — a detector that never reports not-busy —
holds the lock for up to two hours during which the reactor is flowing
reagents, the thermal interlock has no reading, *and* the staleness alarm is
switched off. There is no state in which the operator is told.

> Fix, cheapest first: (a) alarm when `polling_paused` persists beyond, say,
> 2 × (exposure × frames) + a margin — a pause longer than the acquisition
> could take is not a pause, it is a hang; (b) lower `cmd_wait_s` to something
> a real macro line can justify; (c) ship `read_source: "epics"`, which is
> already implemented and keeps reading straight through a collection.

---

### R4 — `ct 0.1` fires ~86,400 times a day and may open the shutter {#r4}

`reactor/config.yml:274` and `:116`

```yaml
read_refresh_cmd: "ct 0.1"   # ⚠ obeys sauto — run "sauto off" if you don't
                             #   want ct opening the shutter
read_interval_s: 1.0
```

`TempController.tick` reads at 1 Hz, and `_do_read_counters` runs
`read_refresh_cmd` before every read. On the real rig that is a counting
operation **once per second, indefinitely** — including while the sample sits
in the beam between runs — each of which, by the config's own warning, may
open the fast shutter.

```
=> ~86,400 counting operations per DAY
```

Two costs: cumulative dose on a sample that is not being measured, and shutter
actuation wear. Neither is visible anywhere in the app.

The mitigation (`sauto off`) is buried in a YAML comment rather than in the
pre-beamtime checklist, and nothing checks it.

> Fix: raise `read_interval_s` to 5–10 s (the temperature plot does not need
> 1 Hz), or switch to `read_source: "epics"`, which needs no refresh command
> at all. Whichever is chosen, add `sauto off` to
> `docs/audits/PRE_BEAMTIME_READINESS.md` as a checked item.

---

### R5 — The E-stop's failure message lands in the wrong card, and gets wiped {#r5}

`reactor/app.py:600` is explicit about this being the one path where a false
success is unacceptable:

```python
# NEVER report a bare success here: if a pump could not be idled the operator
# must see it, not a green tick.
```

The backend does its part. The UI does not. `post()` puts every error into a
single element:

```js
async function post(u,b){try{const r=await api(u,b||{});
  if(r&&r.error)$('form-err').textContent=r.error;}catch(e){}}
```

`#form-err` is `font-size:var(--fs-xs)` at the bottom of the **Synthesis
recipe** card — a different card from the EMERGENCY STOP button, and one the
operator is not looking at during an emergency. There is no alert, no colour
change elsewhere, no persistence: `submitRecipe()` starts with
`$('form-err').textContent=''`, so queueing the next recipe erases
*"could not idle: top, oleylamine — CHECK THESE PUMPS IMMEDIATELY"*.

> Fix: a dedicated banner at the top of the page for E-stop failures, styled
> like `.restart-banner.lost`, that persists until dismissed. The banner
> machinery already exists.

---

### R6 — A dead supervisor and a placeholder temperature look healthy {#r6}

**Measured**, not inferred. Walking every leaf of `status()` and searching the
template for a reference to it:

```
status() fields NEVER referenced in the template:
    supervising            temperature.source
    loop_faults            temperature.stale
    last_fault             temperature.age_s
    pumps[].stale          temperature.trustworthy
    pumps[].v_delivered    temperature.tolerance
    pumps[].max_pressure   spec.enabled
    run_duration_setting   queue_len
```

Two of these groups matter a great deal.

**`supervising` / `loop_faults` / `last_fault`.** The controller goes to real
lengths to make a faulted control loop distinguishable from a healthy one —
`controller.py:1372` calls `supervising` "the ground truth". The UI never
reads it. If the loop thread dies, the page keeps rendering state, pump bars
and temperature from `status()`, all of which still return, and the operator
has no way to see that nothing is supervising the rig any more.

**`temperature.source` / `stale` / `trustworthy`.** The docstring on `source`
(`hardware.py:621`) says:

> Anything that displays a temperature has to distinguish this case

The UI displays `st.temperature.current.toFixed(2)` and nothing else. A frozen
ambient placeholder and a live thermocouple reading render identically, to two
decimal places.

This is the same defect class the Auto Watch audit kept turning up: something
computed carefully and then never shown.

> Fix: a supervision chip in the header driven by `supervising` + `last_fault`,
> and a qualifier next to the temperature ("live · 0.4 s" / "stale 38 s" /
> "not a measurement") driven by `source`/`stale`/`age_s`. `v_delivered` is
> also worth a line — it is the quantity a volume-limit trip acts on.

---

### R7 — Saved pump limits and conditions folder are never loaded at startup {#r7}

**Proven end-to-end.** A temp project holding both saved files, then importing
`reactor/app.py` exactly as the hub launches it:

```
saved on disk        : top min=2.0 max=7.0
in force after start : top min=0.0 max=50.0
saved conditions dir : /tmp/my_conditions
watched after start  : 1D/SAXS/Conditions

pump limits restored : NO  <-- reverted to config.yml
conditions folder    : NO  <-- reverted to config.yml
```

Why: `_load_limits()` and `_load_recipes_folder()` are called from exactly one
place, `/api/set_project`. The startup block calls only

```
['_sync_data_dir_from_hub', '_restore_run_settings', '_restore_auto_run']
```

and the hub POSTs `/api/set_project` only when the folder **changes** while
apps are already running (`hub/app.py:752`); on launch it just puts
`SWAXS_PROJECT` in the child's environment (`hub/app.py:315`). The reactor UI
never posts it either.

Consequences:

* `reactor/knowledge.md` states the conditions-folder override "is persisted in
  `reactor_settings.json` … and reloaded on the next start". It is not.
* Worse, **pump flow limits are the hard limits that feed recipe validation**.
  Every restart silently reverts them to `config.yml` — where all five
  `sensor_min` are `0.0` (see [R14](#r14)). An operator who narrowed a limit
  after a bad batch gets it back only if they happen to re-pick the folder.

> Fix: call both from the startup block. One line each, and it also makes the
> documented behaviour true.

---

## MED-HIGH

### R8 — Every run-setting except exposure accepts zero and negative numbers {#r8}

**Proven.** `set_run_settings` (`controller.py:287`) coerces with a bare
`float()` and stores whatever comes back:

```
live_duration  : -5.0
live_flush_rate: -50.0
live_arm_wait  : -30.0
```

Each has a distinct consequence:

| value | what happens |
|---|---|
| `run_duration < 0` | `_run_deadline` is already past; the run ends on the first tick, after firing a collect |
| `run_duration = 0` | falsy, so it silently falls back to the config default — typing 0 looks like it did something |
| `arm_wait_s < 0` | `_arm_ready_at` is in the past; **arming is skipped entirely** and pumps start at once |
| `flush_rate < 0` | a negative setpoint is passed to `set_pump_flow` and on to the driver |
| `flush_rate = 0` | the worst one — see below |
| `flush_duration = 0` | falsy → config default (1200 s) |

`flush_rate = 0` is accepted by the HTML (`min="0"`) and produces:

```
pump targets during a rate-0 'flush': {'pd_top_precursor': 0.0, 'oleylamine': 0.0,
                                       'top': 0.0, 'ode_dilution': 0.0}
flush pump commanded 0.0 uL/min for 1200 s — the line is never cleaned
```

The app logs `🧼 FLUSH START`, waits twenty minutes, logs `✓ flush complete`,
and collects the next condition's background on a capillary that was never
flushed. Every downstream stage sees a well-formed background. This is exactly
the Run20 `exposure_s = 0` failure mode — a zero that is structurally valid and
scientifically empty — in the fields next door to the one that was hardened
after Run20.

> Fix: the same treatment `exposure_s` got. Refuse non-positive
> `run_duration`, `flush_rate`, `flush_duration`; refuse negative `arm_wait_s`
> (`Recipe.from_dict` already does this — `set_run_settings` does not); keep
> the current value and say so in the log.

---

### R9 — A pump's own `max_flow` is enforced at intake only, never while running {#r9}

**Proven.**

```
accepted setpoints: {'pd_top_precursor': 60.0, 'oleylamine': 12.0,
                     'top': 12.0, 'ode_dilution': 36.0}
ode_dilution max_flow lowered to 5.0 AFTER the recipe was queued
commanded target  : 36.0 uL/min   (pump max is now 5.0)
state after _safety_check(): running
VERDICT: NOT CAUGHT — runs over its own max
```

`recipe_to_setpoints` checks `sensor_min`/`max_flow` at submit time.
`_safety_check` checks only the platform-wide `safety.per_pump_max`
(1000 µL/min), never `p.max_flow`. So a limit narrowed while a recipe is
queued — the natural reaction to noticing a pump is misbehaving — does not
apply to that recipe. On the shipped config the gap is 50 vs 1000 µL/min: a
20× headroom on the three small-sensor reagent pumps, with no runtime guard.

> Fix: add `p.target > p.max_flow` to the per-pump loop in `_safety_check`,
> next to the existing `per_pump_max` and `max_pressure` tests. Consider also
> re-validating the queue when `set_pump_limits` narrows a limit.

---

### R10 — Vent during a run discards the run with no record and no event {#r10}

**Proven.** `vent_all()` (`controller.py:600`) is documented as usable "from
ANY state", and the UI button is never disabled. Pressed while running:

```
state: running | runs in history: 0
after vent -> state: idle | history: 0 | events: ['reactor.vent']
VERDICT: run record written? NO — the condition vanishes
```

It idles the pumps, sets `state = "idle"` and `current = None` without going
through `_end_run`. No `history` entry, no `<recipe_id>.done.json`, no
`reactor.run_complete`, no manifest record. The operator's log line says
"vented all pumps — chamber pressure reset to 0" and does not mention that a
synthesis was terminated.

Because `auto_run` is deliberately left on, the campaign continues — with one
condition that ran, produced 2D data, and has no feedback. The optimizer
blocks on it or, worse, the analyzer pairs that data with the wrong record.

> Fix: if `state == "running"`, route through `_end_run(flush=…)` with
> `reason = "vented"` so the record, the feedback file and the event are all
> written, then vent. Or refuse the vent while running and say why — but
> silently discarding the run is the one option that should go.

---

### R11 — An arm timeout drops the condition silently and stalls the queue {#r11}

**Proven.** `controller.py:1133`

```
state          : idle
bus events     : NONE
history records: 0
```

The arm-timeout branch logs a good, detailed operator message and then calls
`_to_idle()`. It emits no `reactor.*` event, writes no feedback file, and
records nothing — so **Auto Watch cannot report it** and the optimizer is left
waiting for a condition that was silently abandoned.

`_to_idle()` also does not call `_begin_next()`, so anything else in the queue
sits there until the next `submit()` happens to kick it. In an autonomous run
that means the campaign pauses for as long as it takes the ML side to write
the next file — which, if the ML side is waiting for feedback on the condition
that just timed out, is forever.

This matters more than it looks because of [R13](#r13): the shipped config arms
on temperature, so a rig with no thermocouple wired hits this path on **every**
condition, 900 s apart.

> Fix: emit a `reactor.run_failed` (or `reactor.safety`) event carrying the
> recipe_id and reason, write a feedback file with `status: "arm_timeout"`, and
> call `_begin_next()` if auto-run is on — or deliberately disable auto-run and
> say so, which is the E-stop's policy and defensible here too.

---

### R12 — A rejected condition file is a log line only {#r12}

`reactor/app.py:419`

```python
except RecipeError as e:
    _emit(f"✗ rejected {f.name}: {e}", "error")
    _watch_handled[key] = sig
    _watch_lastsig.pop(key, None)
```

No bus event, no feedback file, and the file is **not** moved to
`Conditions/done`. So:

* Auto Watch never hears that the pipeline is producing invalid conditions.
* The optimizer gets no `.done.json` and no rejection signal, so it has no way
  to learn the proposal was refused and can propose the same out-of-bounds
  point again — indefinitely.
* Rejected files accumulate in the watched folder, and every poll re-globs and
  re-sorts them.

> Fix: write `<recipe_id>.rejected.json` alongside the normal feedback file,
> emit an event, and move the file to `Conditions/rejected/` so the watched
> folder stays clean.

---

## MED

### R13 — The shipped arming default contradicts the documentation {#r13}

```
reactor/config.yml  arming.default_mode : 'temperature'
reactor/knowledge.md: shipped default is `timed` with a 120 s wait
```

`reactor/knowledge.md` is what the AI assistant indexes and what an operator
reads at 3 a.m. The consequence of believing it is not cosmetic: on a machine
with no thermocouple, temperature arming cannot succeed, so every condition
waits out `temperature.timeout` (900 s) and is then dropped by the path in
[R11](#r11) — silently.

> Fix: decide which is right for the rig as it stands today and make the other
> match. If no thermocouple is wired, `timed` is the honest default.

---

### R14 — Every `sensor_min` is 0, so the documented low-flow rejection can never fire {#r14}

```
pd_top_precursor   sensor=LG16-1000  sensor_min=0.0  max_flow=1000.0
ode_flush          sensor=LG16-1000  sensor_min=0.0  max_flow=1000.0
oleylamine         sensor=LG16-0480  sensor_min=0.0  max_flow=50.0
top                sensor=LG16-0480  sensor_min=0.0  max_flow=50.0
ode_dilution       sensor=LG16-0480  sensor_min=0.0  max_flow=50.0
```

`recipe_to_setpoints` rejects a nonzero setpoint below `sensor_min` — the
config comment, `reactor/knowledge.md` and the UI all describe this as a hard
limit. With `sensor_min = 0.0` on all five it can never trigger. An LG16-0480
is a 1–50 µL/min device; the bounds allow `x_TOP = 0.001 × F_tot = 40`, i.e.
0.04 µL/min, which that sensor cannot meter.

The second net does not catch it either. `_flow_ok` judges any target below
`flow_sensitivity` (1.0 µL/min) by absolute error within ±1.0 µL/min, so a pump
delivering **zero** against a 0.04 µL/min setpoint is reported healthy. The
mixture is wrong, both guards pass, and the campaign records the recipe as if
it had been delivered.

> Fix: set each `sensor_min` to the installed sensor's real floor (the config
> comment already says "Set these to your real sensor min/max"). Consider also
> refusing to start with a `sensor_min` of 0 on a real backend, the way an
> unset `data_dir` is warned about.

---

### R15 — Flow-fault detection is 15× faster in mock than on the rig {#r15}

`bad_flow_tol: 3` counts *ticks*, and a tick is a different length per backend:

* `MockPump.tick` runs from the control loop at ~5 Hz → fault after ~0.8 s
* `RealPump.tick` only evaluates health on a successful status poll, every
  3 s → fault after ~12 s

So a mock rehearsal exercises a flow-fault detector fifteen times more eager
than the one that will run at the beamline. The platform is otherwise careful
about this — `controller.py:94` and `reactor/config.yml:313` both state there
is deliberately no time compression anywhere, so that a mock rehearsal is timed
exactly like the run it stands in for. This is the one place that is not true.

> Fix: make `bad_flow_tol` a duration (`bad_flow_s`) rather than a tick count,
> so both backends agree.

---

### R16 — Controls that fail silently, and controls that are never disabled {#r16}

Full per-control table in [§ Appendix](#appendix). The five that misbehave:

| control | problem |
|---|---|
| **Flush now** | `flush_now()` returns False unless idle/ready; the route returns `{"ok": false}` with **no `error` key**, so `post()` displays nothing. The button is **never disabled**. In 4 of 6 states it is a no-op that looks like a success. |
| **Tare · Pressure / Flow / Both** | `tare()` discards the reply entirely: `await api('/api/tare',…)` with no use of the result. `tare_pump` refuses unless idle/ready/estop and returns a reason; the operator never sees it. The buttons are never disabled, although the card text claims "Available only when idle". |
| **Reset** | `reset()` only acts in estop/ready. Pressed in idle or flushing it does nothing and the route returns `{"ok": true}`. |
| **Stop → flush** | `abort()` in idle/ready does nothing; route returns `{"ok": true}`. (Mitigated: the button *is* disabled outside arming/running/flushing.) |
| **Clear queue** | always reports success, even for an empty queue. |

The pattern is that `_simple()` and the bare `jsonify({"ok": True})` routes
were written for controls that cannot fail, and then reused for controls that
can.

> Fix: return `{"ok": false, "error": "<reason>"}` from `/api/flush`,
> `/api/reset` and `/api/abort` when the state refuses the action; surface
> `/api/tare`'s message the way `collectNow()` already surfaces its own; and
> disable **Flush now** and the tare buttons on the same state rule that
> already governs **Collect now**.

---

### R17 — No disconnect indicator {#r17}

`reactor/templates/index.html:780`

```js
const es=new EventSource('/api/stream');
es.onmessage=e=>{…};
```

There is no `es.onerror` and no client-side watchdog on the arrival of frames.
`render()` runs only when a message arrives, so if the app dies — crash, hub
Stop, laptop sleep — the page freezes on its last frame and keeps displaying
**running · 240 °C · pumps at setpoint** indefinitely. `EventSource` will retry
in the background, but nothing on screen changes in the meantime.

For an unattended overnight run this is the most likely way the operator forms
a false belief about the rig.

> Fix: `es.onerror` sets a "disconnected" state on the header pill, plus a
> timestamp check — if no frame has arrived for >5 s, grey the page and say so.

---

## Minor {#minor}

| # | Item |
|---|---|
| R18 | `self.history` (`controller.py:815`) and `MockBeamline.collections` (`driver.py:388`) grow without bound — one record per run and one dict per acquisition. Small per item, but nothing trims them over a multi-day campaign. |
| R19 | `shutdown()` checks `is_collecting()` before closing the shutter but then calls `beamline.close()` (release remote control) unconditionally, and does not wait for an in-flight acquisition. |
| R20 | `/api/recipes_folder` accepts any string without checking it exists; the watcher then **creates** the directory. A typo silently redirects the campaign to an empty folder that will never receive conditions, with no error anywhere. |
| R21 | `TempController.set_temperature` swallows every exception from `beamline.set_temperature`, so a failed `csettemp` is invisible — including the end-of-run cooldown and the vent-to-0. In `timed` arming the pumps then start regardless, at an unknown temperature. |
| R22 | `/api/stream` rebuilds `_ctrl.status()` twice a second **per connected browser**, each call taking the controller lock and calling `beamline.is_collecting()`. No cap on clients; five tabs left open is 10 status builds/s competing with the control loop. |
| R23 | Dead code: `if True:` wrapping the whole of `_tick_once` (`controller.py:1111`); the `elif etype == "fit.complete": pass` branch in `_on_bus_event` (`app.py:355`). |
| R24 | `spec.simulator.poni` is a hard-coded absolute path to one machine (`/Users/akmaurya/Desktop/Data_local/Auto_Run/poni`). On any other machine it silently falls back to synthetic geometry, which makes the recovered particle size wrong without saying so. |
| R25 | `/api/set_project` accepts a path without checking it is a directory (the hub checks, but the route is reachable directly). |

---

## What was checked and found sound

An audit that lists only defects hides its own coverage. These were examined
closely and are right:

* **The E-stop path.** Reagents are stopped *before* taking the lock, with a
  documented measurement (7.8 s) behind that choice; every pump is guarded
  independently; `confirm_idle()` reads back REMOTE control so a `P0` ignored
  by a pump in manual mode is surfaced rather than counted as success; the
  E-stop is re-run under the lock to catch the race; auto-run is disabled so
  the folder watcher cannot restart into an unresolved fault.
* **The control-loop fault handler.** E-stop runs first, in its own guard, with
  a second message for the case where the E-stop itself fails, and a final
  block that E-stops if the loop ever exits with `_alive` still true.
* **The mock/real switch.** Normalised identically in both layers; new hardware
  is built completely before anything is swapped, so a failed real connection
  cannot leave real ports open while the app reports mock; refuses mid-run and
  mid-collection; the 2D simulator is bound to the class, not to a flag, so no
  config value can produce synthetic data on real hardware.
* **Recipe intake.** `decide_intake` waits for a stable size+mtime before
  parsing, so a file caught mid-write is retried rather than lost, and remembers
  by signature so a corrected rewrite is picked up.
* **Measurement-signal correlation.** `signal_measurement_complete` ignores a
  signal carrying a different `recipe_id`, so a late average from the previous
  condition cannot truncate the current run.
* **`_end_run` on a run that never started.** Refuses to write a record, which
  is what stops the optimizer training on a synthesis that did not happen.
* **Pump serial mapping.** Matching by serial number with a hard refusal to
  fall back to a stale COM port, and a duplicate-port check — both of which
  prevent driving reagents down the wrong line.
* **Browser-side memory.** All strip-chart buffers are trimmed to the 600 s
  window; the log box is capped at 500 lines.
* **The spec-settings freeze.** Correct, well-reasoned, and correctly
  surfaced in the UI (`locked` / `lock_reason` are among the few status fields
  the template does read).

---

## Appendix — every control, traced {#appendix}

| control | → route | → controller | validated? | failure visible? |
|---|---|---|---|---|
| Mock / Real toggle | `POST /api/backend` | `switch_backend` | state + collecting guards | ✅ confirm + alert |
| Conditions folder · Set | `POST /api/recipes_folder` | — | none ([R20](#minor)) | ⚠ shows old path as if set |
| T_reac, F_tot, x_ODE, x_TOP, x_oley | `POST /api/recipe` | `submit` → `validate` | ✅ bounds + caps | ✅ `#form-err` |
| ＋ Run manually | `POST /api/recipe` | `submit` | ✅ | ✅ |
| ▶ Run autonomously | `POST /api/auto_run` | `set_auto_run` | n/a | state via SSE |
| arm mode radios | `POST /api/run_settings` | `set_run_settings` | ✅ enum-checked | n/a |
| arm_wait_s | `POST /api/run_settings` | `set_run_settings` | ❌ negatives ([R8](#r8)) | ❌ |
| run_duration | `POST /api/run_settings` | `set_run_settings` | ❌ 0 and negatives ([R8](#r8)) | ❌ |
| flush_rate | `POST /api/run_settings` | `set_run_settings` | ❌ 0 and negatives ([R8](#r8)) | ❌ |
| flush_duration | `POST /api/run_settings` | `set_run_settings` | ❌ 0 ([R8](#r8)) | ❌ |
| flush_pump | `POST /api/run_settings` | `set_run_settings` | ✅ name-checked | n/a |
| Exposure / frames / lead / tags / save folder | `POST /api/spec_settings` | `set_spec_settings` | ✅ incl. `exposure > 0` | ✅ lock note |
| 📷 Collect now | `POST /api/collect_now` | `collect_now` | ✅ state + collecting | ✅ ✓/✗ inline |
| Start / ⏩ Start pumps now | `POST /api/start` · `/api/start_now` | `start` · `start_now` | ✅ state | button disabled; `start_now` error → wrong card |
| ■ Stop → flush | `POST /api/abort` | `abort` | ✅ state | ⚠ always `ok:true` ([R16](#r16)) |
| Flush now | `POST /api/flush` | `flush_now` | ✅ state | ❌ silent no-op ([R16](#r16)) |
| Reset | `POST /api/reset` | `reset` | ✅ state | ⚠ always `ok:true` ([R16](#r16)) |
| 🟦 Vent all pumps | `POST /api/vent` | `vent_all` | ❌ runs from any state | ❌ discards the run ([R10](#r10)) |
| 🛑 EMERGENCY STOP | `POST /api/estop` | `estop` | n/a | ⚠ message in the wrong card ([R5](#r5)) |
| Apply limits | `POST /api/pumps` | `set_pump_limits` | ✅ range + factor | ✅ `#lim-msg` |
| 🗑 Clear queue | `POST /api/queue/clear` | `clear_queue` | n/a | ⚠ always `ok:true` |
| Tare · Pressure/Flow/Both | `POST /api/tare` | `tare_pump` | ✅ state + kind | ❌ reply discarded ([R16](#r16)) |
| Clear (charts) | — | — | client-only | n/a |
| Theme | — | — | client-only | n/a |

---

*Method: static reading of all 4,300 lines in scope, plus six executable
probes against a live `ReactorController` on the mock backend and one
end-to-end import of `reactor/app.py` under the hub's launch conditions.
Findings marked "proven" quote that output verbatim. No visual verification
was possible — there is no headless browser in this environment — so every UI
finding is structural (control → handler → route → controller), never
appearance.*
