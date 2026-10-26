"""
src/optimizer/guide.py: the Analyser's Guide tab, one page per goal, method and
concept. Plain text; the method descriptions and acquisition formulas come from
the registries (src/fitting, src/optimizer/methods), so a page never disagrees
with the method it describes.
"""
from __future__ import annotations

BENCH = "docs/benchmarks/FITTER_BENCHMARK_2026-10.md"

ORDER = ["Scoring", "Goals", "Fitting", "Next recipe", "AI next recipe", "Knowledge", "Report"]

FAMILY = {"catalogue_bic": "Standard", "ls_sphere": "Standard", "ml_regressor": "ML",
          "llm_assisted": "Agentic", "random": "Standard", "gp_ei": "ML", "gp_ucb": "ML",
          "turbo": "ML", "bax": "ML", "pareto_ehvi": "ML"}

PAGES = {
    "score": {"title": "How the score works", "group": "Scoring",
        "what": "Every measured recipe gets one number, lower is better, that the next-recipe "
                "method tries to minimise. PDI here is σ/R̄ of the fitted Schulz distribution, not "
                "the DLS polydispersity index. A fit of another shape than expected is held: it "
                "counts against the budget but does not steer the search until you accept it. A fit "
                "below the minimum confidence still informs the model, with a low weight, but cannot "
                "be the answer or end the run.",
        "maths": "score = ((R − R_t) / tol)² + w · (PDI / cap)\n"
                 "R_t = target diameter / 2,  tol = diameter tolerance / 2  (the optimiser works in radius)\n"
                 "one tolerance off the target counts as 1;  w = 1",
        "when": "Hit a size. The region and trade-off goals use the same size error but judge it "
                "against a band or a front.",
        "refs": "docs/PARAMETER_SPACE_AND_CONVERGENCE.md"},
    "target-hit": {"title": "Hit a size", "group": "Goals",
        "what": "Find one recipe whose particles meet the target diameter within the tolerance and "
                "the PDI cap, then stop.",
        "when": "You know the size you want.",
        "not": "You want every recipe that works: use Map the in-spec region."},
    "level-set": {"title": "Map the in-spec region", "group": "Goals",
        "what": "Find the whole set of recipes that meet the spec, not just one (BAX). Runs to the "
                "budget.",
        "when": "You want a robust operating window.",
        "not": "You only need one good recipe; Hit a size is faster."},
    "pareto": {"title": "Size vs PDI trade-off", "group": "Goals",
        "what": "Map the lowest PDI achievable at each size across a diameter range: the Pareto "
                "front (EHVI). Runs to the budget.",
        "when": "You want to know what is possible before choosing a target."},
    "benchmark": {"title": "Compare methods", "group": "Goals",
        "what": "Race next-recipe methods on the ground-truth simulator: same recipes, same score, "
                "several repeats. Only methods that optimise the same score are raced (Random, "
                "GP + EI, GP + UCB, TuRBO).",
        "when": "Before a beamtime, to choose a method with evidence.",
        "not": "It never drives the reactor."},
    "catalogue_bic": {"group": "Fitting",
        "when": "Default. Particles that may not be plain spheres.",
        "not": "You need sub-second fits per curve: use the sphere fit.",
        "maths": "Each catalogue model is fitted; scale a and background b are solved exactly:  I ≈ a·F(q) + b\n"
                 "BIC = χ² + k·ln n   (lowest wins; k counts every free parameter)\n"
                 "R_eq = √(5/3)·R_g of the fitted shape  (= R for a sphere)",
        "scored": "Clean held-out: 100 % shapes, 0.3 % median size error. Hard held-out: 55 % shapes, "
                  "52 % sizes within 10 %. About 9 s per curve.",
        "refs": BENCH + " · Schwarz (1978), Ann. Stat. 6, 461 (BIC)"},
    "ls_sphere": {"group": "Fitting",
        "when": "You know the particles are spheres and want speed.",
        "not": "Rods, core-shell or aggregates: the size it reports is then meaningless.",
        "maths": "P(q) = [3 (sin qR − qR cos qR) / (qR)³]², averaged over a Schulz or log-normal "
                 "size distribution; least squares in log I",
        "scored": "Spheres: about 0 % size error. Other shapes: 50 % to 3000 % error. 0.2 s per curve.",
        "refs": BENCH},
    "ml_regressor": {"group": "Fitting",
        "when": "A quick first look at sphere-like samples.",
        "not": "Sizes outside its library (radius 1 to 18 nm, so diameter 2 to 36 nm) or non-spheres.",
        "maths": "k-nearest neighbours against a library of Schulz-sphere curves; confidence from the "
                 "absolute fit residual",
        "scored": "Hard held-out: 20 % shapes (spheres only), 31 % median size error.",
        "refs": BENCH},
    "llm_assisted": {"title": "AI-assisted structure", "group": "Fitting",
        "when": "Unusual curves, interactions or aggregation, where expert judgement helps.",
        "not": "Without an AI login it runs as Catalogue + BIC.",
        "maths": "features + context + plot → AI names up to 3 catalogue models → each fitted → "
                 "lowest BIC wins\n(the AI never sets the numbers)",
        "scored": "Not yet measured with the real AI: python tools/fitter_benchmark.py --split "
                  "hard_heldout --methods llm_assisted",
        "refs": "docs/design/LLM_FITTER_PLAN.md · SasAgent, arXiv 2509.05363"},
    "random": {"group": "Next recipe", "when": "As the reference every method must beat.",
        "not": "A real campaign.", "scored": "Measure it: Compare methods."},
    "gp_ei": {"group": "Next recipe",
        "when": "Hit a size; 5 to 30 runs; up to about 6 recipe parameters.",
        "not": "Many parameters or long campaigns: TuRBO.", "scored": "Measure it: Compare methods.",
        "refs": "Jones, Schonlau, Welch (1998), J. Global Optim. 13, 455"},
    "gp_ucb": {"group": "Next recipe", "when": "You want an explicit exploration knob (κ).",
        "not": "You have no reason to tune κ; GP + EI needs no setting.",
        "scored": "Measure it: Compare methods.", "refs": "Srinivas et al. (2010), ICML"},
    "turbo": {"group": "Next recipe",
        "when": "More parameters or a long campaign; searches near the best recipe.",
        "not": "Very few runs: the trust region has no time to adapt.",
        "scored": "Measure it: Compare methods.", "refs": "Eriksson et al. (2019), NeurIPS"},
    "bax": {"group": "Next recipe", "when": "Map the in-spec region.",
        "not": "Hit a size: it spends runs on the band edge, not on one hit.",
        "refs": "Neiswanger, Wang, Ermon (2021), ICML (InfoBAX)"},
    "pareto_ehvi": {"group": "Next recipe", "when": "Size vs PDI trade-off.", "not": "A single target.",
        "refs": "Emmerich, Giannakoglou, Naujoks (2006), IEEE TEC"},
    "ai_advisor": {"title": "AI Advisor", "group": "AI next recipe",
        "what": "The optimiser picks every recipe. Every 5 or 10 runs the AI reviews progress and may "
                "suggest a narrower working range. A range inside the search range (which sits inside "
                "the reactor's limits) is applied from the next recipe and can be undone, or waits for "
                "you with \"ask me first\". A range outside it goes back to the AI once with the limits, "
                "then is dropped. Measuring never stops for a review.",
        "when": "You want AI judgement without changing how recipes are chosen.",
        "maths": "review every N runs -> {note, bounds?, cited}\nbounds valid if inside the search range, "
                 "at least 10 % of each span wide, and holding a recipe the pumps can make",
        "scored": "Not yet measured on real campaigns."},
    "ai_copilot": {"title": "AI Co-pilot", "group": "AI next recipe",
        "what": "After the cold start the optimiser shortlists 5 recipes: its own pick first, then the "
                "best by Expected Improvement. The AI picks one and cites the cards or the data it used. "
                "An unknown choice, no citation, no AI login, a timeout or a bad answer: the optimiser's "
                "pick, recorded as a fallback.",
        "when": "The recommended AI level: every option it sees was proposed by the optimiser.",
        "maths": "shortlist = [optimiser pick] + top-k EI;  answer {choice, reason, cited, expected}\n"
                 "invalid -> shortlist[1] (the optimiser's own pick)",
        "scored": "Race it in Compare methods (needs the AI login)."},
    "ai_pilot": {"title": "AI Pilot", "group": "AI next recipe",
        "what": "The AI writes each recipe after the cold start. It must give all five settings as "
                "numbers inside the search range, the pump windows, the hard caps, the working range "
                "and a trust region around the best recipe, must not repeat a measured recipe, and must "
                "cite. One retry with the reason, then the optimiser's pick. Reviews cannot be turned "
                "off, and Start asks you to confirm the limits.",
        "when": "Method research, after it has beaten Random and GP + EI in Compare methods.",
        "not": "A beamtime before that.",
        "maths": "trust region: best ± 25 % of each setting's span / 2",
        "scored": "Race it in Compare methods (needs the AI login)."},
    "harness": {"title": "How the AI is harnessed", "group": "AI next recipe",
        "what": "A fixed JSON answer; the context is chosen by code (target, measured runs, the "
                "shortlist, the frozen approved cards when \"every AI pick\" is on); every answer is "
                "validated and falls back safely; the answer is cached on the exact prompt so a replay "
                "is reproducible; every decision is shown in Report, Decisions and traced (kind "
                "\"design\"). The AI call runs in the background, so the page and the loop never wait "
                "on it.",
        "refs": "docs/design/DESIGN_HARNESS_PLAN.md · Meta-Harness, arXiv 2603.28052"},
    "starting": {"title": "Starting recipes", "group": "Next recipe",
        "what": "Before the optimiser has data it needs a cold start: recipes spread over the whole "
                "search range so it can learn the trends of all five settings. Your own recipes go "
                "first, then recipes from knowledge (inside the range the ticked cards allow), then "
                "the spread. After the cold start the optimiser picks from the measured results and "
                "searches the full range again, so a wrong card cannot trap the search.",
        "maths": "cold start = your own + from knowledge + spread out  (about 2 × the 5 settings = 10)\n"
                 "spread out = Sobol points over the search range (seed 0)\n"
                 "from knowledge = Sobol points inside the cards' range (seed 1)",
        "when": "Every live campaign. Preview shows the exact recipes before the pumps start."},
    "knowledge": {"title": "Knowledge for campaigns", "group": "Knowledge",
        "what": "One knowledge base per chemistry (e.g. Pd nanoparticles), in the Knowledge tab, with a Fitter part and a Design loop part. The design loop part holds cards from literature (web "
                "search, papers) and your notes, and finished campaigns. Drafted cards wait in "
                "Drafts until you approve them. Setup picks the base by name; what its parts use "
                "is ticked in the Knowledge tab. The base's version is frozen at Start.",
        "when": "Literature and your notes: where the starting recipes from knowledge go. Past "
                "campaigns: a warm start, their measured runs train the optimiser's model (scored "
                "against the new target) without using the budget.",
        "not": "Safety limits. Those live only in the reactor limits; a card can narrow where the "
               "starting recipes go but never widen the search. Text from papers or web pages is "
               "never passed on as an instruction: only approved cards (setting, effect, numeric "
               "range, source) are used.",
        "refs": "The Fitter part: see Knowledge for the AI fitter. Guinier's own files stay in ai_knowledge/rules and reference."},
    "verdicts": {"title": "Your verdicts", "group": "Knowledge",
        "what": "Good call or Bad call, with one line why, on any run in Report, Decisions. It is kept "
                "with the run and, when the campaign used a knowledge base, in the base at once. With an "
                "AI level, \"Your verdicts\" ticked and \"every AI pick\", the AI reads them (and may "
                "cite them) at every pick or review.",
        "not": "A verdict on the run you are replaying: the replay leaves a campaign's own verdicts out."},
    "replay": {"title": "Did the knowledge help? (replay)", "group": "Knowledge",
        "what": "The set's finished campaigns are replayed. For each, an emulator is fitted to its own "
                "measured runs (a GP for diameter and one for PDI); every method then runs a fresh "
                "campaign against it with that campaign's target and budget, several times: Random, "
                "GP + EI, GP + EI with the other campaigns as a warm start, GP + EI with starting "
                "recipes from the cards, and (if asked) AI Co-pilot with cards and verdicts.",
        "maths": "score per method = runs to reach the spec (not reached = budget + 1), best score after 10\n"
                 "propose improvement: tune on the older campaigns, keep a change only if it also wins on\n"
                 "the most recent campaign (held out); the operator approves it",
        "not": "Proof. An emulator is only as good as the runs it was fitted on; with one or two "
               "campaigns the result is indicative; if every method reaches an easy spec in the "
               "cold start, the test cannot tell them apart.",
        "refs": "src/design_knowledge/replay.py · Meta-Harness, arXiv 2603.28052"},
    "fit_knowledge": {"title": "Knowledge for the AI fitter", "group": "Fitting",
        "what": "AI-assisted structure reads the Fitter part of the knowledge base chosen in Setup "
                "(with no base: Guinier's catalogue, corrections, SOPs and guidelines). Knowledge, "
                "Fitter, What it uses ticks it part by part. Guinier's knowledge (shared with the "
                "assistant): the model catalogue (always), operator corrections, group SOPs, SAXS "
                "guidelines, literature passages found for each curve, expert cases. The base's own "
                "knowledge: your notes about the samples and data, and your judged fits, the Right / "
                "Wrong-model calls under the plot in the Fitting tab. For "
                "each curve the AI is shown the most similar judged curves, never the curve's own call. "
                "Two or more calls saying the same correction draft a note, used only once you approve it.",
        "maths": "order of authority: corrections > the base's notes > SOPs > guidelines\n"
                 "similar = rms difference of mean-centred log I on a shared log-q grid (<= 0.6 decades)\n"
                 "the AI only proposes; every candidate is fitted and BIC picks the winner",
        "when": "Frozen at Start, with a version stamp on every fit. Knowledge, Fitting evaluation refits "
                "your judged curves with and without each kind of knowledge and counts how often each "
                "finds the right model; Propose improvement changes a setting only if it also wins on "
                "the held-out half and you approve it.",
        "not": "A way to force a model: the data still decide. Text from notes, papers and cases is "
               "passed as data, never as instructions.",
        "refs": "src/fitting/fit_knowledge.py · docs/design/LLM_FITTER_PLAN.md"},
    "ai_safety": {"title": "When the AI fitter is wrong, stuck or broken", "group": "Fitting",
        "what": "The AI only names shapes; physics fits them and BIC chooses, so a wrong (hallucinated) "
                "shape loses to a better one, and shapes outside the catalogue are dropped. Each AI "
                "call times out after 45 s with no retries; the design loop waits at most 3 min for an "
                "AI fit, then uses the quick Catalogue + BIC fit. After 3 failed AI fits in a row "
                "(errors, timeouts, unusable answers, fits over 150 s) the AI fitter pauses for 30 min "
                "and every profile gets Catalogue + BIC only, with no AI call and no token spent; the "
                "status line says AI FITTER PAUSED and why. At most 60 AI calls an hour.",
        "maths": "pause after 3 failures in a row, for 30 min; then one trial fit\n"
                 "per call: 45 s, no retries, at most 900 output tokens; at most 2 calls per fit\n"
                 "design loop waits at most 3 min for the AI fit",
        "when": "Always on with AI-assisted structure; nothing to set. Limits can be changed with the "
                "SWAXS_FITTER_AI_* settings.",
        "refs": "src/fitting/ai_guard.py · src/fitting/llm_structure.py"},
    "views": {"title": "Report views", "group": "Report",
        "what": "Convergence: best score so far. Surrogate: the GP's prediction and uncertainty "
                "across two recipe parameters. Trajectory: every recipe tried. Level set: the "
                "predicted in-spec region. Pareto: best PDI per size.",
        "refs": "docs/PARAMETER_SPACE_AND_CONVERGENCE.md"},
}


def pages(fitters: list[dict], optimisers: list[dict]) -> list[dict]:
    """Ordered pages: {id, group, title, family, rows: [[label, text]], maths, settings}."""
    meta = {m["id"]: m for m in list(fitters or []) + list(optimisers or [])}
    out = []
    for grp in ORDER:
        for pid, g in PAGES.items():
            if g["group"] != grp:
                continue
            m = meta.get(pid, {})
            title = g.get("title") or m.get("label") or pid
            maths = g.get("maths", "")
            acq = m.get("acq") or {}
            if acq.get("formula"):
                maths = f"{acq.get('name', '')}\n{acq['formula']}\npicks {acq.get('picks', '')}".strip()
            rows = [[k, v] for k, v in (("What it does", g.get("what") or m.get("desc", "")),
                                         ("When to use", g.get("when", "")),
                                         ("When not to", g.get("not", "")),
                                         ("How it scored", g.get("scored", "")),
                                         ("Read more", g.get("refs", ""))) if v]
            settings = [f"{p.get('label')}: default {p.get('default')}"
                        + (f" ({p['help']})" if p.get("help") else "")
                        for p in (m.get("params") or [])]
            out.append({"id": pid, "group": grp, "title": title, "family": FAMILY.get(pid, ""),
                        "rows": rows, "maths": maths, "settings": settings})
    return out
