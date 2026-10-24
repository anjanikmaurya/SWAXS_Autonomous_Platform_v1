"""
src/optimizer/guide.py: the Analyser's Guide tab, one page per goal, method and
concept. Plain text; the method descriptions and acquisition formulas come from
the registries (src/fitting, src/optimizer/methods), so a page never disagrees
with the method it describes.
"""
from __future__ import annotations

BENCH = "docs/benchmarks/FITTER_BENCHMARK_2026-10.md"

ORDER = ["Scoring", "Goals", "Fitting", "Next recipe", "Knowledge", "Report"]

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
        "what": "One knowledge set per chemistry, in the Knowledge tab: cards from literature (web "
                "search, papers) and your notes, and finished campaigns. Drafted cards wait in "
                "Drafts until you approve them. Setup step 4 chooses the set and which parts a "
                "campaign uses; the set's version is frozen at Start.",
        "when": "Literature and your notes: where the starting recipes from knowledge go. Past "
                "campaigns: a warm start, their measured runs train the optimiser's model (scored "
                "against the new target) without using the budget.",
        "not": "Safety limits. Those live only in the reactor limits; a card can narrow where the "
               "starting recipes go but never widen the search. Text from papers or web pages is "
               "never passed on as an instruction: only approved cards (setting, effect, numeric "
               "range, source) are used.",
        "refs": "General SAXS knowledge for fitting stays with Guinier (ai_knowledge/rules, reference)."},
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
