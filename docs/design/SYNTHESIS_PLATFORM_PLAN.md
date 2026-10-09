# Autonomous Synthesis: modular instrument platform (plan)

Status: **plan, partly built.** Phase 0 is built (October 2026): the contract in
`src/synthesis/` (types.py, instrument.py with the gated `InstrumentSession`,
registry.py, and a simulated `ToyMixer` in toy.py), tested by
`tests/test_synthesis_contract.py`. Nothing uses it yet: the reactor app and its
hardware code are unchanged, and no instrument is registered.
Phases 1 to 6 are **NEVER BUILT** yet (including `src/synthesis/workflow.py` and
the instrument modules). Robot and well plate routes are deliberately left for later.
**Built (October 2026):** the page layout of section 8, in its simplified form: left
column = synthesis route (Flow reactor; robot and well plate are placeholders) with
the pump controls and emergency stop at its foot; three tabs Hardware test ·
Autonomous setup · Live plots, each a numbered sequence; Activity log docked below.
A runnable copy with a simulated backend: `docs/mockups/synthesis_flow_mockup.html`
(rebuild with `python docs/mockups/build_flow_mockup.py`).
Scope: redesign of the Autonomous Synthesis app (`reactor/`, port 5108) and
`src/reactor/` + `src/beamline/` so that any synthesis hardware (flow reactor,
microfluidic chip, liquid handling robot, syringe well plate mixer, …) plugs in
through one contract, and several instruments can be **chained** into one
workflow (prepare, mix, react, measure).

Decisions taken with the operator:

| Question | Decision |
|---|---|
| Concurrency | **Chained workflows**: multi device step sequences run as one run |
| Structure | **One Synthesis app with plugins** (one module per instrument) |
| Framework | **Own Python interface** in `src/synthesis/`; vendor SDKs, SiLA or ophyd can be wrapped inside a module later |
| First step | **Framework first**: port the existing flow reactor and beamline onto it; new devices come after |

---

## 1. Principles

1. **One contract, many instruments.** The platform never contains device
   specific code. Everything an instrument can do is reached through the same
   small interface (section 2).
2. **Every module ships with a simulated twin.** Simulation mode is a property
   of the platform, not something each device reinvents.
3. **Recipes say what, modules decide how.** The optimiser output (condition
   files) stays device neutral. A module compiles it into its own step plan and
   refuses what it cannot make, before anything moves.
4. **Gated workflow.** Connect, Test, Setup, Ready, Run, Clean. Run is locked
   until the tests pass and setup is saved.
5. **Data is declared, not hand plotted.** Modules declare channels; plots,
   storage, limits and alerts are built from that declaration.
6. **Isolation.** Each driver runs in its own worker process. A stuck serial
   port or a crashing vendor SDK cannot freeze the UI or another instrument.
7. **Nothing changes downstream.** Condition files in, `reactor.runs` in the
   manifest, events on the bus: the optimiser, analyser, watchdog and assistant
   keep working unchanged.

---

## 2. The instrument contract

`src/synthesis/instrument.py` (new). A module subclasses `Instrument` and fills
in only what it supports.

```python
class Instrument:
    id: str                    # "flow_reactor", "beamline", "ot2", …
    kind: str                  # "reactor" | "dispenser" | "robot" | "measurement" | …

    # Describe ─ static, no hardware needed
    def describe(self) -> Description:
        """parameters (name, unit, limits, default), capabilities
        ("mix", "heat", "flow", "measure_saxs", …), channels (name, unit,
        safe range), tests (ids + titles), setup items."""

    # Lifecycle
    def connect(self, cfg) -> Result            # open ports, identify hardware
    def disconnect(self) -> None
    def run_test(self, test_id) -> TestResult   # pass/fail + measured value + fix hint
    def setup(self, item, values) -> Result     # calibrate, prime, tare, labware map
    def compile(self, recipe) -> Plan | Refusal # what → how; refuse if infeasible
    def execute(self, step, ctx) -> StepResult  # one plan step; yields telemetry
    def safe_state(self) -> None                # stop motion/flow, close valves, heaters off
    def estop(self) -> None                     # fastest possible stop, no questions
    def clean(self) -> Result                   # flush, rinse, park

    # Telemetry ─ called by the worker at the module's declared rate
    def read_channels(self) -> dict[str, float]
```

Supporting types (`src/synthesis/types.py`): `Parameter`, `Channel`, `TestSpec`,
`TestResult(ok, value, expected, hint)`, `Plan(steps, est_duration, consumables)`,
`Refusal(reason, parameter)`, `StepResult`.

A **simulated twin** is a second class with the same `describe()` and
deterministic physics (the current `MockPump` ramps, the mock temperature ramp,
the 2D simulator). Simulation and Hardware are chosen **per instrument**.

Registration: `src/synthesis/instruments/<id>/` with `module.py`, `sim.py`,
`config.yml` (defaults), `knowledge.md` (indexed by the assistant), and a
`tests/` folder. A registry file `synthesis.yml` lists the enabled instruments,
in the same spirit as `apps.yml`.

---

## 3. Recipes, plans and chained workflows

```
condition file (optimiser)  →  Recipe (device neutral)
                                   │
                    Workflow template (synthesis.yml)
              step 1  robot.prepare_plate   │
              step 2  mixer.mix              │  each step: instrument + capability
              step 3  flow_reactor.react     │  + which recipe fields it consumes
              step 4  beamline.measure       ▼
                         Run = compiled plans of every step, checked up front
```

* **Recipe**: today's `Recipe` (T, flow, fractions, …) generalised to a dict of
  named, unit tagged values plus `recipe_id`. Condition file format unchanged.
* **Workflow template**: an ordered list of steps. Each step names an
  instrument, a capability and its inputs. Steps hand a **sample record** to
  the next (sample id, location such as "well B3" or "loop 2", volumes).
* **Compile before run**: every step's module compiles its part first. If any
  refuses, the whole run is refused with the reason and nothing moves. This is
  where limit checks live (today's pump limits, R-series audit checks).
* **The current flow sequence becomes a template**:
  `flush → background shot → arm → run → sample shot → flush` is
  `flow_reactor.flush, beamline.measure(bkg), flow_reactor.arm,
  flow_reactor.react, beamline.measure(sample), flow_reactor.flush`.
  The beamline becomes an instrument module like any other.

`src/synthesis/workflow.py` runs a compiled run step by step, records every
transition, and on any failure calls `safe_state()` on **every** instrument in
the workflow, not only the one that failed.

---

## 4. The gated state machine (per instrument)

```
Disconnected → Connected → Tested → Set up → Ready ⇄ Running → Cleaning → Ready
                    ↑ any failure or e-stop → Fault (safe state) → reset → Connected
```

* **Tested** means every *required* test passed in this session (tests can be
  marked required or advisory). Results are kept with a timestamp; a test older
  than its declared validity (e.g. 12 h) has to be re run.
* **Set up** means the module's required setup items are saved for this
  project (calibration factors, plate map, priming done).
* A workflow can start only when **all** its instruments are Ready.
* Switching Simulation ⇄ Hardware drops the instrument back to Disconnected.

---

## 5. Telemetry and data

* Channels are declared in `describe()`: `name, unit, rate, safe_min/max,
  warn_min/max, plot_group`.
* The worker samples them and pushes to the app process. The app keeps a ring
  buffer (live plots), writes each run to disk, and forwards limit crossings to
  the supervisor and to Autonomous Watch.
* Storage per run, inside the project:

```
<project_root>/Synthesis/
├── runs/<run_id>/
│   ├── run.json          # workflow, compiled plans, recipe, result, versions
│   ├── telemetry.csv     # one column per channel, UTC timestamps
│   └── events.jsonl      # every command, reply, state change, operator action
├── setup/<instrument>.json   # saved calibration / setup per instrument
└── tests/<instrument>.jsonl  # test history
```

* `run.json` is summarised into the manifest (`reactor.runs`, unchanged key)
  so the assistant and analyser see the same record as today.

---

## 6. Safety, in three layers

1. **In the worker, next to the hardware**: each module enforces its own hard
   limits on every command (a flow above the pump maximum never reaches the
   serial port). This layer works even if the app is frozen.
2. **Platform supervisor (app process)**: watches declared channel limits,
   worker heartbeats and data staleness; trips `safe_state()` on breach.
   Generalises today's supervisor (temperature, pressure, volume, flow).
3. **Global emergency stop**: one button, always visible, broadcasts `estop()`
   to every connected instrument in parallel.

**Worker death** is the dangerous case (pumps keep pumping). The supervisor
starts a fresh short lived process that reconnects and calls `safe_state()`;
every module must make `safe_state()` work from a cold connect. Physical
interlocks remain the last line and are out of scope here.

---

## 7. Process model

```
Synthesis app (Flask, UI, workflow engine, supervisor, storage)
   │  JSON messages over multiprocessing pipes (stdlib, no new dependency)
   ├── worker: flow_reactor (serial pumps)
   ├── worker: beamline     (SPEC bServer HTTP, EPICS)
   └── worker: <future>     (robot SDK, …)
```

* One worker per instrument; the driver code runs only there.
* Heartbeat every second; three missed → Fault + cold `safe_state()`.
* A worker restart lands the instrument in Connected (tests must pass again
  before Ready).
* The hub's graceful shutdown (`POST /api/shutdown`) fans out `safe_state()` to
  every worker before exit, as today's `_shutdown_once` does for the reactor.

---

## 8. Layout

Left column layout (same shell as reduction, calibration, average, …), top bar
unchanged in order.

```
┌ Autonomous Synthesis · status · [extras]          ……  folder │ theme │ Port 5108 · ← Hub ┐
├──────────────┬─────────────────────────────────────────────────────────────────────────┤
│ WORKFLOW     │  Flow reactor              ● Ready          [Simulation | Hardware]        │
│  ▸ Flow loop │  Overview  Test  Setup  Run  Data  Diagnostics                          │
│              │ ─────────────────────────────────────────────────────────────────────── │
│ INSTRUMENTS  │  (tab content)                                                           │
│  ● Flow      │                                                                          │
│  ● Beamline  │                                                                          │
│  ○ Robot     │                                                                          │
│              │                                                                          │
│ QUEUE  3     │                                                                          │
│ ACTIVITY     │                                                                          │
│ ──────────── │                                                                          │
│ [EMERGENCY   │                                                                          │
│   STOP]      │                                                                          │
└──────────────┴─────────────────────────────────────────────────────────────────────────┘
```

**Left column**: the workflow (templates, the active one highlighted), the
instruments with a status dot (grey disconnected, amber needs test/setup,
green ready, blue running, red fault), queue count, activity log link, and the
global emergency stop pinned at the bottom.

**Per instrument tabs**

| Tab | Content |
|---|---|
| Overview | state, Simulation/Hardware, key live numbers, last test and setup age, what is blocking Ready |
| Test | checklist of tests with Run / Run all, pass/fail, measured vs expected, fix hint |
| Setup | the module's setup items (forms generated from `describe()`), saved per project |
| Run | manual run of this instrument's capabilities, using the same compile + gate path |
| Data | live plots built from channels, grouped by `plot_group`; past runs browser |
| Diagnostics | event timeline, re run any single test, guarded manual console, support bundle export |

**Workflow view** (click the workflow in the left column): the step chain as
cards (instrument + capability + status), the queue of recipes, Run
autonomously, and a combined live view of the channels each step marks as key.

**Forms are generated** from `describe()`, so a new instrument gets its Setup
and Run forms without new HTML. A module can still provide a custom panel for
something special (e.g. a clickable plate map).

---

## 9. Troubleshooting by design

* Every failure message names the instrument, the step, the last command and
  reply, and a fix hint (pattern already used by the calibration launcher).
* **Event timeline** per instrument and per run, filterable, from `events.jsonl`.
* **Support bundle**: one click zips config, setup, test history, the last N
  minutes of telemetry and events, plus versions, ready to send.
* **Dry run**: any workflow can be run against the simulated twins with the
  real config, so a recipe problem shows up before hardware time.
* The assistant indexes each module's `knowledge.md` and can read the timeline.

---

## 10. Migration, in phases

Each phase ends with the full test suite green and the current behaviour intact.

| Phase | Work | Proof |
|---|---|---|
| 0 | `src/synthesis/` contract, types, registry, a toy simulated instrument | contract tests; toy module passes connect/test/setup/compile/execute |
| 1 | Port flow rig (`PumpBank`, temperature) and beamline (`src/beamline/driver.py`) into two modules with sim twins; keep `ReactorController` as a thin shim over them | **golden sequence test**: in Simulation, the event sequence for a recipe is identical to today's; all `test_reactor_*` pass unchanged |
| 2 | Workflow engine + templates; today's flow sequence becomes the default template; compile before run | refusal tests (bad recipe never moves hardware); failure in any step → all instruments safe |
| 3 | Worker processes, heartbeats, cold `safe_state()` | kill a worker mid run → pumps reach safe state; UI stays responsive |
| 4 | New UI shell (section 8): left column, tabs, generated forms, channel plots, diagnostics, support bundle | UI policy tests (top row, icons, theme), jsdom harness, screenshots |
| 5 | Telemetry storage, manifest summary, Watch alerts from declared limits | run folder contents; manifest unchanged shape; alert fires on limit |
| 6 | Docs: `docs/ADDING_AN_INSTRUMENT.md` (template module + checklist), update REACTOR_SETUP / MAP / knowledge.md; remove the shim | a second toy instrument added by following the guide only |

After phase 6, each new device (robot, syringe well plate mixer, microfluidic
chip) is a self contained module plus its twin, with no platform changes.

---

## 11. Risks and open questions

* **Sample tracking across devices**: how a sample is identified and located
  between steps (well ids, loop positions, vial racks). Needed before the
  first chained workflow with a robot; propose a minimal `SampleRecord` in
  phase 2 and extend when the first plate device arrives.
* **Shared resources**: the beamline is one resource; if two workflows ever
  run, it needs a scheduler. Out of scope while workflows run one at a time.
* **Vendor SDK constraints**: some SDKs (e.g. robot vendors) run their own
  server or need Windows; the worker boundary is where an adapter to such a
  server lives.
* **Behaviour drift during the port**: mitigated by the golden sequence test
  and by keeping `ReactorController` until phase 6.
* **Audit findings**: `docs/audits/REACTOR_AUDIT.md` (R1 to R25) and
  `docs/CONTINUOUS_RUN_HARDENING_PLAN.md` are carried into the module tests so
  no fixed defect returns.
