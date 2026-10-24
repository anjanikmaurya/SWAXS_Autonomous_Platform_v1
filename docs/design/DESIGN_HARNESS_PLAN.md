# AI next-recipe (design) agent: harness plan

> **Status, 2026-10-24.** Built: traces and version stamps on every AI
> decision, the three levels with validation and fallback
> (`src/optimizer/ai_design.py`), knowledge sets and their freezing
> (`src/design_knowledge/`), the simulator race including AI Co-pilot and
> Pilot. NEVER BUILT yet: the replay benchmark of past real campaigns, the
> per-source ablation, the approve-only improvement loop, and verdicts on
> decisions (Phase 4).

The AI next-recipe levels (Advisor, Co-pilot, Pilot) get the same harness as
the AI fitter (`docs/design/LLM_FITTER_PLAN.md`), following Meta-Harness
(arXiv 2603.28052): keep full traces, score on a fixed benchmark with a
held-out split, and accept a harness change only if the held-out score improves.

## Where knowledge lives: the Knowledge tab writes, Setup reads

**Knowledge tab** (one set per chemistry): everything is a card (setting from
`src.optimizer.space.NAMES`, what it says, source label: paper, your run, your
note, your verdict, campaign).

1. Add knowledge: Search the web (query from the target and `chemistry.yml`,
   editable; abstracts, full text only if open access), Add paper (PDF or DOI,
   the AI drafts cards), Add note (one line, one card). Your runs, Good/Bad
   calls and finished campaigns are added automatically.
2. Waiting for your approval: unapproved cards are never used.
3. All cards, filterable by source.

**Setup** asks four questions: What do you want? · What do you already know?
(pick the set, one line) · Who picks the next recipe? (Optimiser · Optimiser,
AI checks · AI decides; the optimiser method under Advanced) · How are curves
fitted? (Best of all shapes · AI suggests the shape; other fitters under
Advanced). Folders sit in the Ready-to-start card. At Start the set is frozen
and versioned, so every decision in the campaign uses the same knowledge.
Cards are used for the starting recipes, and by the AI at every pick.
**Report → Did the knowledge help?** shows the with/without replay.

## Principle: statistics propose, the AI chooses, code checks

| Step | Who | What |
|---|---|---|
| 1 State | code | campaign history, GP fit, target, limits |
| 2 Shortlist | optimiser | top-k candidates by acquisition, with predicted size, PDI and uncertainty |
| 3 Knowledge | code | top cards, design cases and past campaigns from the chosen knowledge set, retrieved by chemistry and recipe similarity |
| 4 Choose | AI | JSON: Co-pilot = shortlist index; Pilot = a recipe. Always cited IDs, reason, expected size and PDI |
| 5 Validate | code | `ParameterSpace.valid`, pump limits, trust region (Pilot), citations exist; otherwise the optimiser's top-1 is used and the fallback is logged |
| 6 Record | code | trace with kind `design`; the measured outcome is joined in when the run is analysed |

Advisor never picks a recipe. Its review suggestions (e.g. new search bounds) are applied automatically when they lie inside the safety limits (reactor limits and search bounds), logged with Undo; one outside them goes back to the AI with the limits once, then is dropped. The operator can choose "ask me first" instead. Measuring never stops for a review.

## Version stamp

Every decision and condition file carries the knowledge-set version, the
`design_harness.yml` hash, the prompt version and the AI model. Same campaign
state plus same stamp gives the same prompt (temperature 0, cached), so replays
are reproducible.

## Benchmark

| Part | What it tests | Split |
|---|---|---|
| Replay of real campaigns | Does knowledge built from literature and experience help on real chemistry? The GP is refit on the runs measured so far; a chosen recipe is scored by the GP of the full campaign | tune on some campaigns, judge on others (held out) |
| Simulator race (`src/optimizer/study.py`) | Safety and efficiency: never worse than GP + EI by a margin, no invalid recipes | seeds 0 to 9 practice, 10 to 19 held out |

Scores: runs to reach spec, best score after N runs, fallback rate, citation
rate, how often the AI beat its own prediction.

Arms, each with and without each knowledge source: Random, GP + EI,
GP + EI + warm start, Co-pilot (cards, cases, campaigns on/off), Pilot.

The simulator alone is a misleading test for literature-based knowledge, since
its chemistry is invented. Replay is the honest test; until there are at least
two real campaigns for a set, the Evaluation page says so.

## Improvement loop

1. The harness settings live in one file per set, `design_harness.yml`
   (shortlist size, cards and cases per decision, sources, prompt version,
   trust region, supervisor interval).
2. A proposed change (by the operator, or drafted by the AI from traces of
   poor decisions) is scored on the practice split.
3. It is kept only if the held-out score improves and the operator approves it;
   the set version is then bumped.

## In the Analyser

- Setup, step 3: knowledge set and sources.
- Knowledge → Evaluation: harness settings, offline results, Run evaluation,
  Propose improvement.
- Report → Decisions: AI picks, agreement with the optimiser, fallbacks, beat
  prediction, set version; each decision shows "Based on" and a Good/Bad call
  that becomes a design case.

## Build order

1. Traces and version stamp on the existing optimiser decisions (no AI).
2. Replay benchmark plus simulator race with GP + EI and warm start.
3. Co-pilot with the validation and fallback above.
4. Ablation per source, then Pilot.
5. Improvement loop with approval.
