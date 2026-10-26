# Changelog

Notable changes, newest first. Point-in-time detail lives in `docs/audits/`;
this file is the running summary. Dates are when the work landed on `main`.

---

## October 2026

### Analyser quick audit (AN1 to AN14)

- `docs/audits/ANALYZER_QUICK_AUDIT_2026_10.md`: 14 findings, 2 HIGH (a restart
  switches the fitter to the default; the first batch trains the model, = O7),
  all filed in the register. Nothing fixed yet.

### Analyser: clearer Setup, named knowledge bases with a Fitter part

- **Setup** now reads top to bottom: Data folders, Target & limits, **Knowledge
  base** (pick one by name, e.g. Pd nanoparticles, with a two-line summary),
  **Fitting**, **Design loop**, Starting recipes. Fitting and Design loop are
  separate full-width lists, each ordered Standard, then ML, then Agentic.
- Case-by-case settings moved under **More options** in their step: Min fit
  confidence; optimiser tuning; AI review interval, apply mode, Claude model and
  thinking; your own starting recipes. Test AI connection stays visible.
- **Knowledge bases** have two parts, Fitter first, then Design loop. The AI
  fitter's sources, notes, judged fits, settings and evaluation now live in the
  base (`ai_knowledge/design/<id>.json`, key `fitter`), not per project; what the
  design loop uses (literature, notes, past campaigns, verdicts, every AI pick)
  is also saved with the base. Setup no longer has these ticks.
- Calls on fits go to the running campaign's base, else the one chosen in Setup.
  A project's older `fit_knowledge.json` can be copied into a base (the file is
  left as it was). Export / Import carry the Fitter part (notes as drafts).

### Analyser: knowledge for the AI fitter

- Setup step 3, with **AI-assisted structure**, now chooses what the AI fitter
  reads: **Guinier's knowledge** part by part (model catalogue, always; operator
  corrections; group SOPs; SAXS guidelines; literature passages found per curve,
  when the index is built; expert cases) and **this project's knowledge**
  (`<project>/fit_knowledge.json`): your notes and your judged fits.
- **Fitting tab**: Right / Wrong-model call under the plot, with the right
  catalogue model and a note. For each new curve the AI sees the most similar
  judged curves (never its own). Repeated corrections draft a note that waits for
  approval.
- Frozen at Start with a content hash; every fit records the version and what it
  used. Without a snapshot the fitter behaves exactly as before.
- **Knowledge, Fitting knowledge**: Project notes, Judged fits, and Fitting
  evaluation (refits the judged curves with Catalogue + BIC and with the AI given
  Guinier's knowledge only, plus notes, plus notes and judged fits; leave one
  out; approve-only improvement that must also win on the held-out half).
  `src/fitting/fit_knowledge.py`; tests `tests/test_fit_knowledge.py`.

### Analyser: choose the Claude model and thinking; test the AI connection

- The Analyser (and the AI fitter, knowledge drafting and loop advice) now log in
  and pick their model exactly as Guinier does, from one module,
  `src/ai/models.py`: `~/.claude/settings.json` is loaded first, and the default
  is a SLAC-gateway model id (`us.anthropic.claude-sonnet-4-6`). Before, they
  fell back to a bare `claude-sonnet-5`, which the gateway rejects, and read the
  default before loading the settings.

- The AI level's box in Setup step 3 now sets the **Claude model** (default from
  your settings, or Sonnet / Opus / Haiku 5.5, Fable 5.1) and **Thinking** (off,
  standard, deep). Both are recorded with every decision and shown in Report.
- **Test AI connection** (and `python tools/ai_check.py --all`) makes one tiny
  real call and says Connected with the model and time, or the error type; a
  credential is never shown. The Ready list uses the result.

### Analyser: verdicts, replay test and the approve-only improvement loop, Phase 4

- **Verdicts**: Good call / Bad call with one line why on every run in Report,
  Decisions (now listing every run, not only AI picks). Kept with the run and in
  the campaign's knowledge set at once; AI levels read them when Setup ticks
  Your verdicts (Knowledge tab, Your verdicts; one per run, Retire).
- **Did the knowledge help?** (`src/design_knowledge/replay.py`): each finished
  campaign of a set is replayed against an emulator fitted to its own measured
  runs; Random, GP + EI, + past campaigns, + recipes from cards, and optionally
  AI Co-pilot run fresh campaigns with its target and budget. Runs to reach the
  spec and best score after 10, with the caveats stated. Report and Knowledge,
  Evaluation; background job.
- **Harness settings per set** (recipes from knowledge, AI shortlist size, Pilot
  trust region), frozen with the campaign. **Propose improvement** tunes them on
  the older campaigns and proposes a change only if it also wins on the most
  recent one; nothing changes until the operator approves.
- Campaign entries now carry tolerance and PDI cap, so they can be replayed.
- Tests: `tests/test_replay.py` (10); page check 110.

### Analyser: AI Advisor, Co-pilot and Pilot, Phase 3

- `src/optimizer/ai_design.py` wraps the chosen optimiser. **Advisor**: the
  optimiser picks; reviews every 5 or 10 runs may narrow the working range,
  applied only inside the search range (undoable) or after the operator's
  approval. **Co-pilot**: the AI picks one of the optimiser's 5 (its own pick
  first) and must cite. **Pilot**: the AI writes the recipe, checked against
  the search range, pump windows, hard caps, working range, a trust region and
  repeats; one retry, then the optimiser's pick; reviews cannot be off; Start
  needs a confirmation.
- Harness: fixed JSON, code-chosen context (the frozen approved cards only when
  "every AI pick" is on), validation with fallback, cached answers, every
  decision recorded and traced (kind "design"). The AI call runs in the
  background, never under the campaign lock; a pause or abort drops its answer.
- Report, **Decisions**: AI picks, agreement with the optimiser, fallbacks,
  working range, Apply / Reject / Undo. State survives a restart.
- Compare methods can race AI Co-pilot and Pilot (needs the AI login; without
  it they run as GP + EI and say so). The Guide has a page per level.
- Tests: `tests/test_ai_design.py` (27, a scripted stand-in AI); page check 105.

### Analyser: knowledge sets and the Guide, Phase 2

- **Knowledge tab**: design knowledge per chemistry (`src/design_knowledge/`,
  stored in `ai_knowledge/design/`, not read by the assistant). Add knowledge by
  web search (OpenAlex), DOI, PDF, pasted text or your notes; everything lands
  in Drafts for approval; cards are typed (setting, effect, numeric range,
  source), checked against the search range (clipped, or outside and unused)
  and flagged when they read like a safety limit. AI drafting must quote the
  text for every card; without an AI login a sentence filter drafts instead.
  Versioned, nothing deleted (Retire), Export / Import (imports arrive as
  drafts). Finished campaigns join the set with their measured runs.
- **Setup step 4, Knowledge for this campaign**: the set (matching
  `chemistry.yml` preselected), Use Literature / Your notes / Past campaigns.
  **Starting recipes from knowledge** go inside the range the approved cards
  allow (3 by default, out of the spread); contradictory cards keep the full
  range. **Past campaigns** warm-start the optimiser's model (scored against the
  new target, no budget used; `CampaignController(prior=...)`). The set's
  version is frozen at Start and kept for resume and Continue.
- **Guide tab** (`src/optimizer/guide.py`): one page per goal, method and
  concept; every method links to its page.
- Tests: `tests/test_design_knowledge.py` (18), page check now 92 assertions.

### Analyser: goal column and the new Setup, Phase 1

- The Autonomous Analyser (5107) now has the platform's left column: the
  campaign **Goal** (Hit a size, Map the in-spec region, Size vs PDI trade-off,
  Compare methods), with the same Setup · Fitting · Report tabs for every goal.
- **Setup** is four numbered steps: Data folders (Conditions checked against
  the reactor's folder), Target & limits (PDI labelled σ/R̄ from the SAXS fit,
  new **Expected shape** and **Min fit confidence**, the search range shown
  read-only from the reactor config), How it fits & searches (method lists
  grouped Standard / ML / Agentic, filtered to the goal), and **Starting
  recipes** (spread out plus up to 5 of your own; Preview shows the exact cold
  start). A Ready-to-start card checks the setup.
- **Safety**: your own starting recipes are refused unless complete, finite,
  inside the search bounds and pump windows and under the reactor's hard caps;
  a refused Start changes nothing. Every recipe a simulated loop writes is
  checked with the reactor's own `validate()` in the tests.
- **Held results** (`src/optimizer/intake_rules.py`): a fit of another shape
  than expected counts against the budget but does not train the model or end
  the run until the operator accepts it (Fitting tab). The Fitting table shows
  diameter and a Scored column (yes / weak / held).
- **Pause / Resume** (Report tab): no new recipe while paused; results already
  in flight are still recorded; survives a restart. Abort moved beside it.
- Compare methods races only methods that optimise the same score (Random,
  GP + EI, GP + UCB, TuRBO), Random and GP + EI ticked by default, with the
  Setup target.
- Diameter everywhere in the UI (the optimiser still works in radius).
- API: `POST /api/campaign/pause`, `/accept`, `/preview`, `GET /api/search_range`;
  `/api/campaign/start` takes `seeds`, `expected_shape`, `confidence_min`.
- Tests: `tests/test_analyzer_campaign_controls.py`, the jsdom page check
  (now 76 checks).

### Structure fitter, Phase 4: the AI proposes, physics fits

- **LLM-assisted** in the Analyser is now a real structure fitter
  (`src/fitting/llm_structure.py`): the AI reads the curve's features, the
  pipeline context (recipe, `chemistry.yml`, normalisation, background and
  quality flags), a log-log and Kratky plot and the knowledge bundle (model
  catalogue, corrections, SOPs, SAXS guidelines, cached), and names up to 3
  catalogue structures. Each, plus a sphere baseline, is fitted by the robust
  fitter and the lowest BIC wins; a poor winner gets one residual check.
  Proposals are cached per curve, every decision is traced (kind "fitter"),
  and with no AI login or a bad answer it falls back to Catalogue + BIC.
- Benchmark hard sets with real-data problems; Catalogue + BIC 55 % of shapes
  on hard held-out curves, so there is room for the AI to show its value.
- Speed: the catalogue and LLM fitters no longer run the slow sphere fit first
  (48 s down to 5 s on an aggregated curve); each model has an enforced time
  budget (a long rod could previously run for minutes).
- Tests: `tests/test_fitter_phase4.py` (a stand-in AI, no login needed).

### Structure fitter, Phase 3: benchmark

`tools/fitter_benchmark.py` (logic `src/fitting/benchmark.py`) scores every
fitting method on simulated curves of the five catalogue shapes with known
truth, on separate practice and held-out sets, resumable. Held-out, 40 curves:
Catalogue + BIC 100 % shape correct, median size error 0.3 %; sphere fit and ML
regressor 20 % (spheres only) with 49 % and 38 % median size error. Results and
limits: `docs/benchmarks/FITTER_BENCHMARK_2026-10.md`. Test:
`tests/test_fitter_benchmark.py`.

### Structure fitter, Phase 2: robust fitter and Catalogue + BIC method

- `src/analysis/model_fit.py`: fits one catalogue model robustly (scale and
  background solved exactly at every step, sizes on a log scale, bounded least
  squares from several starts seeded by the curve's features, coarse-then-fine
  with a time budget per model). The old `sasmodels_fit` had returned a 50 Å
  sphere as 76 Å; this recovers sphere, cylinder, core-shell, ellipsoid and
  mass-fractal parameters within 2 %.
- New fitting method **Catalogue + BIC** in the Analyser: every catalogue shape,
  then the best with structure factors, lowest BIC wins; picked the true shape
  for 5 of 5 test curves. Reports an equivalent radius to the optimiser.
- The Analyser now records the real model name (not always
  `polydisperse_sphere`), draws any model's fit curve, writes the model's
  parameters ± errors into the Fit record, and asks for the AI comment once
  per curve (it was twice with LLM-assisted).
- Tests: `tests/test_fitter_phase2.py`.

### Structure fitter, Phase 1: context and features

Groundwork for the LLM-assisted structure fitter (plan:
`docs/design/LLM_FITTER_PLAN.md`). `src/analysis/features.py` computes the
model-free features of a curve (Guinier, Porod exponent, Kratky, p(r) Dmax,
invariants, low-q slope, and new oscillation-minima detection);
`src/analysis/fit_context.py` collects what the pipeline knows about a curve
(recipe, `chemistry.yml`, normalisation, background QC, quality flags);
`sasmodels_fit` now reports χ², BIC, AIC and 1σ parameter errors; the closed
model catalogue is `ai_knowledge/rules/saxs_models.yml`; Reduction records the
normalisation mode. Test: `tests/test_fitter_phase1.py`.

### Assistant knowledge organised; every answer traced

- **One layout** for everything the assistant knows (`ai_knowledge/README.md`,
  paths defined once in `src/ai/layout.py`, every source listed in
  `ai_knowledge/sources.yml`): `rules/` (corrections, group SOPs, and the base
  prompt, moved out of `assistant.py` into `system_prompt.md` unchanged),
  `reference/` (literature, user papers, beamline), `cases/` and `evals/` (to be
  filled), `index/` (search index, not in git). Group SOPs are now shared in git.
- Older clones migrate automatically on the assistant's first start; the index
  log's paths are rewritten so no paper is indexed twice.
- **Traces:** every answer writes one line to `logs/assistant_traces/` with the
  prompt parts used, passages retrieved, tool calls, token use and any error
  (`src/ai/traces.py`; local only, no credentials; `SWAXS_ASSISTANT_TRACES=0`
  turns it off). Groundwork for scoring and improving the assistant, after
  Meta-Harness (arXiv 2603.28052). Test: `tests/test_assistant_knowledge_layout.py`.

### Platform quick audit: 37 fixes (Q1 to Q37)

- **Security:** the assistant binds to localhost only; `run_python` runs under an
  audit-hook guard (no network, no programs, no reads or writes outside the
  project and temp); hub and assistant refuse cross-site POSTs.
- **Silent data errors:** Reduction no longer skips a SAXS frame because its
  WAXS twin exists, and matches beamline CSVs on whole name parts (`scan1` never
  matches `scan10`); Analysis converts Å⁻¹ files to nm⁻¹; Background truncation
  is per detector (WAXS off by default); auto Guinier iterates to q·Rg ≤ 1.3 and
  flags a fit that does not converge.
- **Monitors:** a quick Stop then Start can no longer run two loops (Reduction,
  Background); Run now and Watch exclude each other.
- **Loop:** an expired condition is withdrawn and a late result replaces the
  failure; the ML regressor reports low confidence on noise and railed sizes;
  the analyser follows the reactor's Conditions folder; the search space honours
  per-pump limits; Continue and restart keep the right ask count and pending fits.
- **Platform:** `SWAXS_HUB_PORT` reaches every app; the watchdog reports a lost
  bus and shows whether each message was actually sent; hub log handles close
  (Windows crash logs survive restarts); Windows launcher banner fixed.
- Full list: `docs/audits/PLATFORM_QUICK_AUDIT_2026_10.md`.

### Two loop fixes

- **Average: frames per average.** The page shipped N = 30 while the reactor
  collects `spec.frames` = 10 per acquisition, so no loop batch could ever
  complete. The default is now read from `reactor/config.yml` (10 if it cannot
  be read). Test: `tests/test_average_batch_deadlock.py`.
- **Assistant: corrections store.** Chat corrections were appended as JSONL
  lines into `ai_knowledge/corrections.json`, which then stopped parsing, so
  every authoritative correction silently dropped out and the next one added
  overwrote the rest. They now go to the per-user `corrections.jsonl` again,
  and an already affected file is repaired on read (array kept, stray lines
  moved). Test: `tests/test_corrections_channel.py`.
- README and QUICKSTART show the new SWAXS app icons (`tools/build_doc_icons.py`).

### Calibration, forms and Background layout

- **Calibration: Detector menu instead of a Pixel box.** pyFAI-calib2 crashed
  when given only a pixel size (no detector shape). The app now passes a pyFAI
  detector model (`--detector`): **Auto** picks it from the image shape, or
  choose any pyFAI model, grouped by manufacturer (`/api/detectors`). The GUI
  opens with the loaded file as a detached process, so a crash cannot affect
  the other apps (`src/preprocess/calib.py`, `tests/test_calib_launch.py`).
- **Forms: label beside its box** in every app, two fields per line, stacking
  below 1100 px (`assets/icons/swaxs-tokens.css`).
- **Background Subtraction: setup in numbered, logical order.** The file
  buttons (including List) sit together in step 3, and the old "ML output:
  truncate & rebin" card is now **Output q-range & rebinning**, near the end.
- **Hub:** the Bus badge moved to the right of the top bar.

### SQLite manifest store

- The manifest is now stored in `manifest.db` (SQLite, WAL mode,
  `src/manifest_store.py`) with indexed `files`, `analyses`, `background`,
  `quality`, `reactor_runs` and `events` tables. `src/manifest.py` keeps the same
  dict API, so no app changed.
- Every save is one transaction; `manifest.json` is still exported after each save
  for readers of the JSON.
- Rotating snapshots in `.manifest_snapshots/` (throttled to one per 600 s per
  process, plus a `preshrink-*` snapshot whenever a save would drop most of the
  file index). A missing, empty or unreadable DB is restored from the newest
  snapshot; an unreadable JSON with nothing to recover from raises
  `ManifestUnreadableError` instead of starting an empty store.
- `SWAXS_MANIFEST_BACKEND=json` restores the old single-file store. Tests:
  `tests/test_manifest_sqlite.py`, `tests/test_manifest_snapshots.py`,
  `tests/test_manifest_db.py`.

### Operator entered once, in the hub

The operator name moved from the reduction app to an **Operator** box in the hub
top bar. The hub saves it (`logs/hub_operator.txt`), writes it to the project
manifest as `project_meta.current_operator` and exports `SWAXS_USER_ID` to the
apps it launches. `src/operator_id.py` resolves it for provenance: explicit value,
then the manifest, then `SWAXS_USER_ID`, then the OS login.

### Browse… on every folder field

Folder fields across the apps open the shared folder picker (`swaxsBrowse` in
`_icon_helpers.html`). `src/folder_browse.py` serves `GET /api/browse` for the
apps that had no browser of their own; a relative path opens inside the project
folder. Read only.

### Docs refresh

Developer docs checked against the code: `docs/ARCHITECTURE.md` describes the
system as built, the hardening plan and the defect register carry a Done or Open
status with evidence for each item, completed audits carry a status block, and
the design docs state what is built.

### Reactor quick audit: four follow-up fixes

- **Flush pump changed mid-flush** stopped the NEW pump at the end of the flush and
  left the one actually flushing running. The flush now remembers its own pump
  (`_flush_active`) for stopping and supervision; a new choice applies from the
  next flush, and the log says so.
- **Idle pump still flowing during the flush** is now detected without needing a
  `volume_limit`: after `safety.flow_settle_s`, a non-flush pump above a few µL/min
  for `safety.bad_flow_s` warns once (E-stop with `safety.flow_fault_estop: true`).
- **Shutdown** idles the pumps, waits up to 1 s for the loop, idles again (a tick
  in progress could have commanded a flow), closes the pump ports, and only then
  waits for SPEC; a tick after shutdown commands nothing. Fits the hub's 5 s window.
- **Checklist**: an error inside a check stays inside its line; the beamline check
  fails on stale readings; expected pumps follow the flush pump chosen in the app.

### Reactor: all residual risks closed, hardware checklist, controller split

- **R6** the flush (the longest phase) is now supervised like the run: flow faults
  warn once (E-stop only with `safety.flow_fault_estop: true`), and a non-flush
  pump that keeps delivering past its `volume_limit` during the flush E-stops.
  Only volume delivered during the flush counts, so a run's ramp-down tail
  cannot trip it.
- **R5** "Start pumps now" asks for confirmation (showing the current and target
  temperature), and the log records the temperature the pumps started at.
- **Run all checks** on the Hardware test tab: a read-only checklist (pumps
  connected and answering, pressure, temperature is live, I₀/bstop arriving) with
  a fix hint for each failure. `src/reactor/checks.py`, `GET /api/checks`,
  `POST /api/checks/run`. Sends nothing to the hardware.
- **controller.py split** into `controller.py` (API, loop, status, shutdown),
  `sequence.py` (run order), `supervisor.py` (safety) and `collection.py` (SPEC
  shots). Methods moved verbatim (all 46 compared as parsed code: identical).
  A golden Simulation run recorded before the split is replayed by
  `tests/test_reactor_golden_run.py`; it matches exactly.

All eight July reactor risks are now closed in `docs/audits/OPEN_DEFECTS.md`.

### Reactor residual risks re-checked (R1 to R8)

The eight July reactor risks in `docs/audits/OPEN_DEFECTS.md` were checked against
the code. R1, R2, R3 were already fixed. Fixed now: **R7** `shutdown()` joins the
control loop and closes every pump serial port after idling (an open COM port
blocked the next start on Windows); **R8** every run record and manifest entry
carries `backend` (mock or real), so a campaign can tell simulated runs from real
ones; **R4** `run.end_on_measurement` documented as a no-op (measurement-end is
always on). R5 is by design. R6 (volume and flow checks during the flush) was
closed in the entry above. No setting in `reactor/config.yml` changed. Tests:
`tests/test_reactor_open_defects_2026_10.py`.

### Synthesis instrument contract (phase 0)

New `src/synthesis/` package: the contract every future synthesis route and probe
will implement (describe, connect, tests, setup, compile, execute, safe_state,
estop) and an `InstrumentSession` that gates Test → Setup → Ready → Run, refuses
out-of-limit recipes before anything moves, and always ends a failure or e-stop
in safe state. A simulated `ToyMixer` exercises it in
`tests/test_synthesis_contract.py`. Additive only: the reactor app, `src/reactor/`,
`src/beamline/` and `reactor/config.yml` are untouched, and a test fails if any of
them imports the new package. No instrument is registered; robot and well plate
routes stay placeholders until they are developed.

### Autonomous Synthesis: new page layout

The reactor page now uses the left column layout of the other multi-section
apps. Left column: the synthesis route (Flow reactor; robot and well plate are
placeholders), with all pump controls and the emergency stop at its foot. Three
tabs, each a numbered sequence: Hardware test (tare, pump limits, beamline test
shot with live bstop and I₀, reactor temperature), Autonomous setup (recipe
source, timing, data collection, run) and Live plots (flow, pressure, beamline,
run status). The Activity log is docked below and always visible. Every control,
element id, API call and the canvas plotting code are unchanged; forms put the
label beside its box, two per line, and the type scale matches the other apps.
"Run duration" reads "Synthesis duration". A runnable copy with a simulated
backend is `docs/mockups/synthesis_flow_mockup.html`, built from the real page by
`docs/mockups/build_flow_mockup.py`. Plan for the modular instrument platform:
`docs/design/SYNTHESIS_PLATFORM_PLAN.md`.

## September 2026

### No WAXS peak-finder on SAXS data → structure-factor interpretation

Operator rule: SAXS profiles were being auto-classified as WAXS ("sharp Bragg
peaks") and then overridden every turn — noisy and wrong. `route_modality` is now
detector-aware: a SAXS-detector curve is never voted WAXS; any peak is reported as
a structure-factor correlation (with its q). analysis_tier1 passes the detector,
keeps modality SAXS, and surfaces `structure_factor_peak_q`. The system prompt now
tells the assistant to interpret a mid-q SAXS peak as a possible structure factor
S(q) and recommend the matching sasmodels S(q) (hardsphere, hayter_msa, squarewell,
stickyhardsphere) as a product with the form factor — never as a WAXS Bragg peak.
SAXS peaks are further split into (a) a single broad structure-factor bump vs
(b) real SMALL-ANGLE LATTICE diffraction from a self-assembled mesophase (lipid
nanoparticles: lamellar 1:2:3, hexagonal 1:√3:√4:√7, cubic Pn3m/Im3m/Fd3m),
identified by the q-ratio sequence and fitted with lamellar / *_paracrystal
sasmodels — still SAXS, not WAXS.
Held by `test_saxs_data_is_not_rerouted_to_waxs` and
`test_analysis_guidelines.py::test_saxs_detector_is_never_classified_waxs`.

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
