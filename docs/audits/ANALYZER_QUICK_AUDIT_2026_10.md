# Analyser quick audit, October 2026 (AN1 to AN14)

> **Status, 2026-10-25.** Quick audit after the Phase 1 to 4 rebuild, the AI
> levels, knowledge bases with a Fitter part and the new Setup layout. 14
> findings: 2 HIGH, 6 MEDIUM, 6 LOW. **AN1, AN5 and AN7 fixed 2026-10-25** (the fitter
> belongs to the campaign), then **AN6, AN8, AN9** (disk growth); the rest are open. All are filed in
> [OPEN_DEFECTS.md](OPEN_DEFECTS.md). The earlier deep audit of the loop itself is
> [AUTOFIT_AUDIT.md](AUTOFIT_AUDIT.md) (A1 to A9, fixed) and is not repeated here.
> Each finding below was checked against the code; AN6 was also measured.

Scope: `analyzer/app.py` (3,719 lines), `analyzer/templates/index.html`
(2,818 lines), `src/optimizer/` (campaign, methods, ai_design, intake_rules,
guide), `src/fitting/` (llm_structure, fit_knowledge), `src/design_knowledge/`.
Questions: is it accurate, efficient, robust, clear, easy to use, stable for
days of unattended running, and is the backend organised?

## Summary

| Area | Verdict |
|---|---|
| Accuracy | Sound where tested (units, scoring, held results, seeds). Two gaps: a restart silently changes the fitter (AN1), and the first batch of a recipe trains the model (O7, still open, AN2). |
| Efficiency | Fine with the statistics fitters. The AI fitter runs on the watcher thread, so slow AI calls hold up every later profile (AN3); tests can compete with a live run (AN4). |
| Robustness | Good: every AI call has validation and a fallback, writes are atomic, the loop re-advances after errors. Gaps: the fitter can be changed mid-campaign through the API (AN5); Continue drops the fitter's frozen knowledge (AN7). |
| Days of running | Memory is bounded (results 600, log 300, AI decisions 300, verdicts 500). Disk is not: frozen fitter snapshots grow quadratically outside campaigns (AN6), the AI fitter cache and trace files are never pruned (AN8, AN9). |
| Clarity, ease of use | The new Setup order is clear. Remaining friction: calls on fits need a base (AN10), the Ready list does not warn about a fitter that changes after a restart (part of AN1), no keyboard focus ring (AN12). |
| Backend organisation | The weakest area. `app.py` holds 145 functions, 61 module globals and 62 routes across six domains, with campaign lifecycle logic that belongs in `src/` (AN13). The page is one 2,818-line file with about 1,900 lines of inline script (AN14). |

## Findings

| ID | Severity | Area | Finding (evidence) | Fix | Test |
|---|---|---|---|---|---|
| AN1 | **HIGH** | Accuracy, days of running | **A restart mid-campaign switches the fitter to the default.** `_active_fitter` starts as `fitting.DEFAULT_ID` (`ls_sphere`, app.py:110) and is only set by `POST /api/methods` (app.py:2307). `_restore_campaign` and Continue restore the optimiser from `meta["optimiser"]` but never `meta["fitter"]`. A campaign started with Catalogue + BIC or AI-assisted structure carries on after a hub restart with the sphere-only fitter: non-spheres are then sized as spheres and the held-shape rule changes meaning, with no message. | On restore and Continue, set `_active_fitter = meta["fitter"]` when it is available; log it; if it is not available, pause the campaign and say why. | Start with `catalogue_bic`, restore in a fresh module, assert the fitter is `catalogue_bic`. |
| AN2 | **HIGH** (carried, = O7) | Accuracy | **The first batch profile of a recipe trains the model.** `_pending.pop(rid)` (app.py:1441) consumes the condition on the first matching file; later, better-settled batches are fitted and recorded but never used. | Accumulate per recipe until the reactor's done marker (or N batches), then `tell()` once with the highest-confidence fit, recording which batch. | Three batches with rising confidence: the told value is the third. |
| AN3 | MEDIUM | Efficiency | **The AI fitter blocks the watcher.** `_analyze_file` runs on the single watcher thread (app.py:1978, 2040). An AI-assisted fit is one AI call (60 s timeout, 2 client retries, so up to about 3 min) plus up to 5 model fits of up to 6 s each, then possibly a second AI round. During that time no other profile is fitted, and a recipe's several batch files queue behind it. | Run AI-assisted fits in a small worker pool (1 to 2) with the result fed back under the campaign lock; or fit the first batch with Catalogue + BIC and refine with the AI in the background. Log the time per fit. | A stub AI that sleeps 5 s: a second profile is still fitted within 1 s. |
| AN4 | MEDIUM | Efficiency, stability | **Knowledge tests run during a live campaign.** `_fk_start` and `_eval_start` (app.py:3671, 3191) do not check for a running campaign. The fitter test refits up to 20 curves × 4 arms (AI calls plus model fits) on the same CPU as live fitting. | While a campaign runs, refuse with "run the test after the campaign", or run at low priority with a pause between curves. | Start a campaign, POST evaluate: 409 with the reason. |
| AN5 | MEDIUM | Robustness | **The fitter can be changed mid-campaign through the API.** `/api/methods` refuses a new optimiser while a campaign exists (app.py:2310) but accepts a new fitter (app.py:2300 to 2307). The page locks the control; scripts or a second tab do not. | Refuse a fitter change while a campaign runs, like the optimiser (409). | POST `{fitter}` while running: 409, fitter unchanged. |
| AN6 | MEDIUM | Days of running (disk) | **Frozen fitter snapshots grow quadratically outside campaigns.** `_fk_snapshot` freezes with `save_root=_project_root` on every AI fit outside a campaign (app.py:3448). Every call on a fit changes the hash, so the next fit writes a new snapshot holding every judged fit. Measured: 200 calls gave 200 files, 9.5 MB; 1,000 would be about 240 MB under `.swaxs_state/fit_knowledge/`. | Save snapshots only at Start (and Continue); outside a campaign freeze in memory. Keep at most the last 20 snapshots not referenced by a campaign record. | 50 calls plus fits outside a campaign: 0 new files; Start: 1. |
| AN7 | MEDIUM | Robustness | **Continue drops the fitter's frozen knowledge.** The Continue `meta_keys` list (app.py:1277 to 1280) has `knowledge` but not `fit_knowledge`, so a continued run's AI fits use the base as it is now, not as it was at Start. | Add `fit_knowledge` to `meta_keys`. | Continue a run whose base changed since Start: the snapshot hash is the original. |
| AN8 | MEDIUM | Days of running (disk) | **The AI fitter cache is never pruned.** `.swaxs_state/llm_fit_cache/` gets one JSON per (curve, context, knowledge version, model); nothing removes old ones (llm_structure.py:211 to 241). Over a long beamtime with batch files and knowledge changes this is thousands of files. | Keep the newest N (e.g. 5,000) or 30 days, pruned at start and once a day. | Fill with N+10 entries: N remain, newest kept. |
| AN9 | LOW | Days of running (disk) | **AI trace files are never rotated.** `logs/assistant_traces/YYYY-MM-DD.jsonl` (src/ai/traces.py:106), one per day, kept forever; the AI levels and fitter add a line per decision. Small per day, unbounded over months. | Keep 90 days (setting), pruned at start. | Old dated files beyond the window are removed; today's kept. |
| AN10 | LOW | Ease of use | **Calls on fits need a knowledge base.** With None in Setup, the box under the plot says to choose one and offers nothing else, so a call made during a quick look is lost. | Offer "Save to:" with the list of bases (and "+ New base") right in the box. | Choose a base in the box: the call is saved there. |
| AN11 | LOW | Privacy, portability | **Judged fits store absolute file paths in a git-tracked file.** Each judged fit keeps `path` (e.g. `/Users/<name>/...`) in `ai_knowledge/design/<id>.json`, which is in git and shared on Export. It is used only to reload the curve for the Fitter evaluation. | Store the path relative to the project root plus the project name; strip paths on Export; the evaluation says which curves it could not find. | Export: no absolute paths; evaluation reloads from the project-relative path. |
| AN12 | LOW (carried) | Ease of use | **No keyboard focus ring** in the Analyser (`:focus-visible` absent; also noted in the register's accessibility item). | Add the shared `:focus-visible` rule. | `test_ui_theme_uniform` checks the rule exists. |
| AN13 | LOW (structure, high value) | Backend organisation | **`app.py` is not a thin shell.** 3,719 lines: 145 functions, 61 module globals, 62 routes (knowledge 29, campaign 14, fit knowledge 2, AI 2, study 2, the rest 13). Campaign lifecycle (start, resume, Continue, advance, expiry, late results, records), cold-start assembly, the runs summary and the AI worker threads live here, against the project's one rule. Every test must import the whole app to reach them. | Move in steps, each with the golden tests unchanged: (1) a new module `session` in `src/optimizer/` (proposed, not yet written): one `CampaignSession` object owning campaign, meta, pending, pause, lock, save and restore (replaces about 20 globals); (2) a new module `records` in `src/optimizer/`: campaign and fit records, manifest entries; (3) Flask blueprints `analyzer/routes/{campaign,knowledge,fitter,ai,results}.py`; `app.py` keeps the sys.path block, wiring and startup. | The existing analyzer tests pass unchanged after each step; a new test asserts `app.py` stays under 800 lines. |
| AN14 | LOW (structure) | Clarity of code | **One 2,818-line page with about 1,900 lines of inline script** and global state shared by every tab (`_kset`, `_kStepSet`, `_fk`, `_fkPart`, `_aiLevel`, ...). Changes in one tab break others (this audit's own layout pass hit two such races). | Split the script into per-tab files served as static JS (`setup.js`, `fitting.js`, `report.js`, `knowledge.js`, `guide.js`) with one small state module; keep the jsdom harness as the guard. | The harness passes unchanged against the split files. |

## What was checked and is sound

- **AI safety and fallback.** Every AI pick is validated (search range, pump
  windows, hard caps, trust region for Pilot, citations) and falls back to the
  optimiser's pick; the call runs outside the campaign lock and a pause or abort
  meanwhile drops its answer (app.py:175 to 225). The AI fitter may only name
  catalogue models, and statistics pick the winner.
- **Bounded memory.** Results 600, log 300, AI decisions 300, verdicts 500,
  judged fits 1,000, notes 200; campaign history is bounded by the budget.
- **Atomic writes** for knowledge bases (one lock for both parts), campaign state
  and condition files; nothing in a base is deleted, only retired.
- **Frozen knowledge**: a campaign uses the design part and the fitter part as
  they were at Start, with version stamps on every decision and fit.
- **Restart**: the campaign, its AI state and the design knowledge resume; the
  gaps are the fitter (AN1) and Continue's fitter knowledge (AN7).
- **Tests**: about 1,830 pass, including 143 page checks in the jsdom harness,
  route tests for every knowledge and fitter endpoint, and the restart tests.

## Recommended order

1. AN1 and AN5 (one change: the fitter belongs to the campaign), with AN7.
2. AN6 and AN8 (disk growth over a long beamtime).
3. AN2 (O7) and AN3 (which batch trains the model; fitting off the watcher).
4. AN4, then AN13 in its three steps, then AN14.
