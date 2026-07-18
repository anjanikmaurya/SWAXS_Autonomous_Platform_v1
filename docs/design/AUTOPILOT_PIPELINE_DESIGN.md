# Design: Autopilot pipeline orchestrator

**Status:** proposal (build AFTER the current beamtime). No runtime code paths change
until implemented. Goal: make autonomous runs user-friendly and low-failure by
collapsing reduce → average → subtract into one orchestrated, event-driven sequence,
while keeping the interactive apps for inspection.

---

## 1. Why

The audit (`docs/audits/AUTONOMOUS_RUN_READINESS.md`) showed the loop's fragility is
almost entirely **operational orchestration**, not science:

- 3 monitors (reduction, viewer-average, background) + the analyzer campaign each
  start **manually** in separate processes; forgetting any one stalls the loop.
- Stages hand off through **shared folders** with polling watchers → races, and
  **project-root drift** between processes (each app resolves the root itself).
- The event bus is **telemetry-only**; nothing actually chains the stages.
- Failure in one file's stage can silently stall or mis-pair downstream.

Consolidating the per-shot data path into one in-process sequence removes this whole
class of problems.

## 2. Goals / non-goals

**Goals**
- One process, one **Start**, one config, one unified log for the reduce→average→
  subtract path.
- **Event-driven** off the reactor's collection (deterministic per `recipe_id`), not
  blind folder polling.
- In-process function calls (no cross-process folder races); single project root.
- Fail-soft, idempotent, resumable; strong config validation / pre-flight.
- A single-shot **dry-run** that exercises the whole chain and reports per-stage.

**Non-goals**
- Do **not** remove or merge the interactive apps. Viewer / Background / Quality /
  Analysis stay as the human "microscope" for inspection and manual re-processing.
- Do not change the reduction/averaging/subtraction **science**; reuse it verbatim.

## 3. Architecture

```
 reactor.collect(recipe_id, sample|bkg)  ──emits──►  CollectionComplete(recipe_id, role, path)
                                                          │
                                             AutopilotOrchestrator (headless, 1 process)
                                                          │  (in-process calls into src/)
      reduce(raw) ─► average(group) ─► subtract(sample,bkg) ─► [truncate] ─► subtracted.dat
                                                          │  emits ProfileReady(recipe_id, path)
                                             Analyzer/Optimizer  ─►  next recipe  ─► reactor
```

- The orchestrator is a **thin sequencer** that calls existing `src/` logic. It owns
  the project root once, reads one config, and processes each shot to a subtracted
  profile, then signals the analyzer/optimizer.
- The interactive apps remain; they can read the same folders any time to inspect.

## 4. Prerequisite refactor (small, follows the "logic in src" rule)

Some pipeline logic currently lives in app files and must move to `src/` so both the
orchestrator and the interactive apps share ONE implementation:

- **Subtraction math** → new `src/background/core.py`: move `_subtract`,
  `_interpolate_onto`, `_auto_scale`/`_auto_adjust_scale`, `_qc_metrics`,
  `truncate_rebin`, `_write_dat`, `_process_one` out of `background/app.py`.
  `background/app.py` becomes a thin shell importing them (like the other apps).
- **Averaging** already in `src/plot_reduction.py` (`read_folder`, `average_and_save`)
  — reuse as-is.
- **Reduction** already in `src/reduction/core.py` (`Experiment`, `run_pipeline`,
  `find_new_raw_files`) — reuse as-is.
- Shared naming already in `src/loop_naming.py` — reuse.

This refactor is behavior-preserving and independently testable against the golden
pipeline regression.

## 5. Module layout

```
src/pipeline/
  __init__.py
  orchestrator.py     # AutopilotOrchestrator: subscribe → sequence → emit; fail-soft
  config.py           # load + VALIDATE autopilot.yml (pre-flight)
  steps.py            # reduce_one(), average_group(), subtract_pair() — wrap src/*
  record.py           # one per-shot pipeline record (stages, timings, status) → manifest
src/background/core.py # (moved) subtraction + truncation science
autopilot/            # optional thin UI shell (port 5009): Start/Stop, unified log,
  app.py              #   per-shot status table, pre-flight + dry-run buttons
  templates/index.html
```

The reactor gains a lightweight hook to announce completed collections (see §6).

## 6. Trigger: event-driven off the reactor

Preferred over folder polling because the reactor already knows the `recipe_id` and
the exact path it wrote.

- Reactor `_fire_spec_collection` already records `self._last_collect`. Add a callback
  `collection_cb(recipe_id, role, path)` invoked when a collection finishes.
- The orchestrator registers that callback (in-process if co-hosted, else via the hub
  event bus `reactor.collected`). On each event it:
  1. waits for the `.raw` to be **stable** (reuse `src/reactor/intake.decide_intake`),
  2. runs the sequence for that shot,
  3. when both `sample` and `bkg` for a `recipe_id` are subtracted, emits
     `ProfileReady` for the analyzer.
- **Fallback:** a folder-poll mode (watch `2D/SAXS`) for manual/off-loop use, reusing
  `find_new_raw_files`. Same sequence, different trigger.

## 7. Per-shot sequence (fail-soft, idempotent)

For each shot (or sample↔bkg pair):
1. **Reduce** the `.raw` → `1D/<DET>/Reduction/<stem>.dat` (`run_pipeline`).
2. **Average** its group by `recipe_id`+role keyword → `1D/<DET>/Averaged`
   (`average_and_save`).
3. **Pair + subtract** sample vs bkg by `recipe_id` (nearest-index fallback), scale
   method from config → `1D/<DET>/Subtracted` (+ optional ML truncate/rebin).
4. Write one **pipeline record** (per-stage status, timings, files, QC) to the
   manifest; emit `ProfileReady`.

Rules:
- **Idempotent:** skip a stage whose output already exists + is newer than input;
  `decide_intake` guards partial writes.
- **Fail-soft:** a stage error flags that shot and continues the loop; never stalls
  the whole run. Errors surface in the single log + record.
- **One project root**, resolved once at Start; validated in pre-flight.

## 8. Config: minimal inputs, strong defaults

`autopilot.yml` (or a section of the project config):

```yaml
autopilot:
  detectors: [saxs]              # or [saxs, waxs]
  sample_keyword: "sample"
  bkg_keyword:    "bkg"
  scale_method:   "auto_highq"   # or "fixed" + scale
  scale:          1.0
  truncate:                      # ML output (reuse background truncation)
    enabled: true
    q_min: 0.03
    q_max: 0.6
    n_points: 549
    spacing: linear
    q_unit: A                    # analyzer auto-converts A^-1 → nm^-1
  optimizer:
    target_size_nm: 5.0
    tolerance_nm:   0.5
    min_confidence: 0.5          # QC gate: don't feed the optimizer below this
```

Everything else (data_directory, poni, corrections) stays in the project `config.yml`
that reduction already reads. Minimal net new inputs.

## 9. Pre-flight validation (turn the checklist into code)

On Start, refuse with a clear message unless:
- project root is set and exists; `config.yml data_directory == <root>/2D`; `poni/`
  present with the named files;
- `1D/<DET>/{Reduction,Averaged,Subtracted}` creatable;
- reactor reachable and `spec.data_dir` resolves to the same physical `2D/SAXS`
  reduction scans (the `hub_path_map` check);
- optimizer target/bounds set.
This replaces the manual go/no-go list with one automated gate.

## 10. Unified status + one screen

Single status object/log for the whole loop: per `recipe_id` → {collected, reduced,
averaged, subtracted, analyzed, next-proposed} with timestamps + any error. One SSE
stream, one table. The operator watches one screen instead of four app logs.

## 11. Quality/confidence gate before the optimizer

Fixes the audit risk: the orchestrator only calls `campaign.tell(...)` when the fit
`confidence ≥ optimizer.min_confidence` (and/or QC verdict is acceptable). Bad
subtractions are recorded but not allowed to poison the campaign.

## 12. Dry-run mode

A **Dry run** button pushes one shot (a chosen real `.raw`, or a synthetic profile)
through reduce→average→subtract→analyze and reports pass/fail + timing per stage,
without touching the reactor. This is the pre-beamtime confidence check, in software.

## 13. Phased build plan

1. **Refactor** subtraction/truncation into `src/background/core.py`; `background/app.py`
   imports it. Verify golden regression unchanged. (low risk, high value)
2. **`src/pipeline/steps.py`** wrapping reduce/average/subtract as pure functions +
   unit tests on a tiny fixture dataset.
3. **`orchestrator.py`** folder-poll mode first (simplest), with idempotency +
   fail-soft + pipeline record + unified log.
4. **Config + pre-flight validation**; **dry-run**.
5. **Event trigger:** reactor `collection_cb` → orchestrator; deprecate the 3 manual
   monitors for the autonomous path (keep them for manual use).
6. **Confidence gate** into the optimizer feed.
7. Thin `autopilot/app.py` UI (Start/Stop, pre-flight, dry-run, status table) + hub
   registration.

Each phase is shippable and testable on its own; the interactive apps keep working
throughout.

## 14. Testing

- Reuse/extend the golden pipeline regression (`tests/test_demo_pipeline_regression.py`)
  to run through the orchestrator and match the same numbers.
- Unit tests for `steps.py` (each stage), idempotency (re-run = no-op), fail-soft
  (one bad file doesn't stall), pairing by recipe_id, and the confidence gate.
- A dry-run smoke test in CI on the demo dataset.

## 15. Risks / trade-offs

- **Refactor surface:** moving subtraction into `src/` touches the background app —
  mitigated by the golden regression + keeping the app a thin importer.
- **Two triggers** (event + folder) add a little complexity; keep folder mode as the
  simple fallback and default to events on the rig.
- **Single point of failure:** one orchestrator process — mitigate with fail-soft per
  shot, a heartbeat in the status, and auto-restart via the hub.
- Do **not** attempt during beamtime; this is a between-beamtimes improvement informed
  by what actually breaks on the rig first.
