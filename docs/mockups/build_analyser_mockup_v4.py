#!/usr/bin/env python3
"""
docs/mockups/build_analyser_mockup_v4.py: the proposed Autonomous Analyser page.

Platform concept: the LEFT column chooses what is genuinely different, here the
campaign GOAL; the TABS on the right are the same for every goal
(Setup · Fitting · Report), each with one job, and nothing is shown twice.

    Goal (left)   Hit a size · Map the in-spec region · Size vs PDI trade-off ·
                  Compare methods (in silico)
    Setup         1 data folders · 2 target & limits (fields for the goal) ·
                  3 how it fits & searches: two choosers, each grouped under
                  Standard / ML / Agentic; the next-recipe list shows only the
                  methods that suit the goal · ready-to-start checklist
    Fitting       table with model · selected fit · why this model · re-fit with…
    Report        campaign strip · the views for the goal · decisions · downloads

Uses the REAL page's <head> styles, top bar, partials and shared stylesheet;
method names, descriptions, acquisition formulas and the goal → method filter
are read live from src/fitting and src/optimizer/methods. Static: no backend,
nothing runs or saves.

    python docs/mockups/build_analyser_mockup_v4.py  →  docs/mockups/analyser_mockup_v4.html
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TPL = ROOT / "analyzer" / "templates"
OUT = Path(__file__).with_name("analyser_mockup_v4.html")

import src.fitting as F                    # noqa: E402
import src.optimizer.methods as M          # noqa: E402
from src.analysis.nanoparticle import model_intensity  # noqa: E402

FAMILY_NAMES = {"standard": "Standard", "ml": "ML", "agentic": "Agentic"}
FIT_FAMILY = {"catalogue_bic": "standard", "ls_sphere": "standard",
              "ml_regressor": "ml", "llm_assisted": "agentic"}
FIT_ORDER = ["catalogue_bic", "ls_sphere", "ml_regressor", "llm_assisted"]
FIT_TAG = {"catalogue_bic": "recommended", "ls_sphere": "fastest", "ml_regressor": "spheres only"}
OPT_FAMILY = {"random": "standard", "gp_ei": "ml", "gp_ucb": "ml", "turbo": "ml",
              "bax": "ml", "pareto_ehvi": "ml"}
AI_LEVELS = [
    ("ai_advisor", "Advisor", "The AI suggests the starting bounds and reviews progress; the optimiser picks every recipe.", ""),
    ("ai_copilot", "Co-pilot", "The optimiser proposes its 5 best recipes; the AI picks one and says why.", "recommended for AI"),
    ("ai_pilot", "Pilot", "The AI proposes each recipe itself, within the bounds; the optimiser is the fallback.", "experimental"),
]
# left column: goal id, label, icon, one-line description, registry goal key, target fields
GOALS = [
    ("target-hit", "Hit a size", "swaxs-ui-target", "Find one recipe inside the spec, then stop.", "target"),
    ("level-set", "Map the in-spec region", "swaxs-ui-grid", "Find every recipe that meets the spec.", "range"),
    ("pareto", "Size vs PDI trade-off", "swaxs-ui-chart", "Map the best achievable PDI across a size range.", "range"),
    ("benchmark", "Compare methods", "swaxs-quality", "Race methods on the simulator. No reactor, no beam.", "race"),
]
REPORT_VIEWS = {"target-hit": ["Convergence", "Surrogate", "Trajectory"],
                "level-set": ["Level set", "Convergence", "Trajectory"],
                "pareto": ["Pareto front", "Convergence", "Trajectory"],
                "benchmark": ["Comparison", "Convergence per method"]}


def _inline(text: str) -> str:
    for name in ("_theme_boot.html", "_icon_sprite.svg", "_icon_helpers.html", "_app_mark.svg"):
        p = TPL / name
        text = text.replace('{%% include "%s" %%}' % name, p.read_text() if p.exists() else "")
    css = (ROOT / "assets" / "icons" / "swaxs-tokens.css").read_text()
    text = text.replace('<link rel="stylesheet" href="/static/swaxs-tokens.css">', f"<style>{css}</style>")
    return text.replace('<link rel="icon" href="/app-icon">', "")


def _ico(i):
    return f'<svg class="ico" aria-hidden="true"><use href="#{i}"/></svg>'

def _esc(s) -> str:
    return html.escape(str(s or ""))


def _inline(text: str) -> str:
    for name in ("_theme_boot.html", "_icon_sprite.svg", "_icon_helpers.html", "_app_mark.svg"):
        p = TPL / name
        text = text.replace('{%% include "%s" %%}' % name, p.read_text() if p.exists() else "")
    css = (ROOT / "assets" / "icons" / "swaxs-tokens.css").read_text()
    text = text.replace('<link rel="stylesheet" href="/static/swaxs-tokens.css">', f"<style>{css}</style>")
    return text.replace('<link rel="icon" href="/app-icon">', "")


def _ico(i):
    return f'<svg class="ico" aria-hidden="true"><use href="#{i}"/></svg>'


# ── example data for the Fitting tab ─────────────────────────────────────────

# file, recipe, model, R (nm), PDI, confidence, flags, scored? (expected shape: sphere; min confidence 0.5)
ROWS = [
    ("r014_sample_sub.dat", "r014", "sphere", 2.48, 0.09, 0.86, "", "yes"),
    ("r013_sample_sub.dat", "r013", "sphere", 2.71, 0.12, 0.81, "", "yes"),
    ("r012_sample_sub.dat", "r012", "core-shell sphere", 3.05, 0.11, 0.64, "shell 0.8 nm", "yes"),
    ("r011_sample_sub.dat", "r011", "sphere@stickyhardsphere", 3.60, 0.18, 0.47, "aggregation", "confidence"),
    ("r010_sample_sub.dat", "r010", "sphere", 2.12, 0.08, 0.88, "", "yes"),
    ("r009_sample_sub.dat", "r009", "cylinder", 6.90, 0.14, 0.52, "rod-like", "shape"),
]
SCORED = {"yes": "yes", "shape": "no: shape", "confidence": "no: confidence"}


def _plot_svg() -> str:
    q = np.geomspace(0.1, 4.5, 140)
    I = model_intensity(q, 2.48, 0.09, 1.0, 2e-4, "schulz")
    rng = np.random.default_rng(2)
    d = np.abs(I * (1 + rng.normal(0, 0.03, q.size)))
    W, H, P = 560, 300, 36
    x = lambda v: P + (np.log10(v) - np.log10(q[0])) / (np.log10(q[-1]) - np.log10(q[0])) * (W - 2 * P)  # noqa: E731
    lo, hi = np.log10(d.min()) - .2, np.log10(d.max()) + .2
    y = lambda v: H - P - (np.log10(v) - lo) / (hi - lo) * (H - 2 * P)  # noqa: E731
    pts = " ".join(f"{x(a):.1f},{y(b):.1f}" for a, b in zip(q, I))
    dots = "".join(f'<circle cx="{x(a):.1f}" cy="{y(b):.1f}" r="1.8"/>' for a, b in zip(q[::2], d[::2]))
    return (f'<svg viewBox="0 0 {W} {H}" class="mk-plot"><rect x="{P}" y="{P}" width="{W-2*P}" '
            f'height="{H-2*P}" fill="none" stroke="var(--border)"/><g fill="var(--muted)">{dots}</g>'
            f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>'
            f'<text x="{W/2}" y="{H-8}" text-anchor="middle" font-size="12" fill="var(--muted)">q (nm⁻¹), log</text>'
            f'<text x="12" y="{H/2}" font-size="12" fill="var(--muted)" transform="rotate(-90 12 {H/2})" text-anchor="middle">I(q), log</text></svg>')


def page_fitting() -> str:
    rows = "".join(
        f'<tr class="{"sel" if i == 0 else ""}" title="{f}"><td>{r}</td><td>{_esc(m.replace("@stickyhardsphere", " + sticky HS").replace("core-shell sphere", "core-shell"))}</td>'
        f'<td>{2 * R:.2f}{"*" if m not in ("sphere",) and not m.startswith("sphere@") else ""}</td><td>{p:.2f}</td>'
        f'<td><span class="mk-conf {"hi" if c >= .6 else "mid" if c >= .3 else "lo"}">{c:.2f}</span></td>'
        f'<td class="{"sub" if s == "yes" else "mk-warn"}" title="{_esc(fl)}">{SCORED[s]}</td></tr>'
        for i, (f, r, m, R, p, c, fl, s) in enumerate(ROWS))
    return f"""
  <section class="page" id="page-fitting">
    <div class="mk-fit">
      <div class="card">
        <div class="card-hd">{_ico("swaxs-analysis")} Analysed profiles
          <span class="hd-right"><button class="btn btn-ghost btn-sm">Analyse existing profiles</button><button class="btn btn-ghost btn-sm">CSV</button></span></div>
        <input placeholder="Filter by file, recipe or model">
        <div class="tablewrap"><table class="mk-tbl"><thead><tr><th>Recipe</th><th>Model</th><th>D (nm)</th><th>PDI (σ/R̄)</th><th>Conf.</th><th>Scored</th></tr></thead>
          <tbody>{rows}</tbody></table></div>
        <div class="sub">* equivalent diameter (2·√(5/3)·Rg) for non-spheres. A fit of another shape than expected, or below the confidence limit (Setup step 2), is not scored until you accept it. ↑ ↓ step through fits.</div>
      </div>
      <div class="card">
        <div class="card-hd">{_ico("swaxs-autofit")} r014_sample_sub.dat <span class="hd-right"><select><option>Re-fit with…</option><option>Catalogue + BIC</option><option>AI-assisted structure</option><option>ML regressor</option></select></span></div>
        <div class="kpis">
          <div class="kpi"><span>Model</span><b>sphere</b></div>
          <div class="kpi"><span>Diameter</span><b>4.96 nm</b></div>
          <div class="kpi"><span>PDI (σ/R̄)</span><b>0.09</b></div>
          <div class="kpi"><span>Confidence</span><b>0.86</b></div>
          <div class="kpi"><span>R<sub>g</sub> (Guinier)</span><b>1.95 nm</b></div>
        </div>
        <div class="plottools"><div class="scalebar"><button class="on">Log-log</button><button>Kratky</button><button>Guinier</button><button>Semilog Y</button><button>Linear</button></div>
          <label><input type="checkbox" checked> Residuals</label><label><input type="checkbox"> Error bars</label></div>
        {_plot_svg()}
        <div class="mk-verdict"><span class="lbl">Your verdict</span>
          <button class="btn btn-ghost btn-sm">Agree</button>
          <select class="mk-mini" style="width:auto"><option>Wrong model…</option><option>is core-shell</option><option>is cylinder</option><option>is aggregated</option><option>other (add note)</option></select>
          <button class="btn btn-ghost btn-sm">Add note</button>
          <a href="#" class="mk-i mk-ext" style="margin-left:auto">Ask Guinier about this fit ↗</a></div>
        <div class="sub">Your verdict is saved as a fitting case in the shared SAXS knowledge: the AI fitter and Guinier see similar cases next time.</div>
        <div class="card fold"><div class="card-hd" onclick="this.parentElement.classList.toggle('shut')"><span class="chev">▾</span> Why this model</div>
          <div class="foldbody"><table class="mk-tbl"><thead><tr><th>Candidate</th><th>χ² (reduced)</th><th>BIC</th><th>R<sub>eq</sub></th></tr></thead><tbody>
            <tr class="sel"><td>sphere</td><td>1.06</td><td>337</td><td>2.48</td></tr>
            <tr><td>sphere@hardsphere</td><td>1.05</td><td>341 (+4)</td><td>2.47</td></tr>
            <tr><td>core-shell sphere</td><td>1.06</td><td>348 (+11)</td><td>2.49</td></tr>
            <tr><td>ellipsoid</td><td>1.10</td><td>352 (+15)</td><td>2.51</td></tr></tbody></table>
            <div class="sub" style="margin-top:6px"><b>AI (Agentic):</b> evenly spaced minima, first at q·R = 4.49, Porod −4, flat low-q: compact spheres; core-shell kept as an alternative.</div></div></div>
        <div class="card fold shut"><div class="card-hd" onclick="this.parentElement.classList.toggle('shut')"><span class="chev">▾</span> Parameters ± errors</div>
          <div class="foldbody"><div class="sub">radius 2.48 ± 0.02 nm · radius_pd 0.09 ± 0.01 · scale and background solved exactly</div></div></div>
        <div class="card fold shut"><div class="card-hd" onclick="this.parentElement.classList.toggle('shut')"><span class="chev">▾</span> Curve features</div>
          <div class="foldbody"><div class="sub">minima at q = 1.81, 3.20, 4.43 nm⁻¹ · Porod n = −3.9 · low-q slope −0.05 · Kratky peak at q·Rg 1.70</div></div></div>
      </div>
    </div>
  </section>"""



# ── Guide content (one page per item; registry fields are added at build time) ─

BENCH = "docs/benchmarks/FITTER_BENCHMARK_2026-10.md"
GUIDE = {
  "score": {"title": "How the score works", "group": "Scoring",
    "what": "Every measured recipe gets one number, lower is better, that the next-recipe method tries to minimise. PDI here is σ/R̄ of the fitted Schulz distribution, not the DLS polydispersity index. A fit of another shape than expected, or below the confidence limit, is not scored until you accept it.",
    "maths": "score = ((R − R_t) / tol)² + w · (PDI / cap)\nR_t = target diameter / 2,  tol = diameter tolerance / 2  (the optimiser works in radius)\none tolerance off the target counts as 1",
    "when": "Hit a size. The region and trade-off goals use the same size error but judge it against a band or a front.",
    "scored": "", "refs": "docs/PARAMETER_SPACE_AND_CONVERGENCE.md"},
  "target-hit": {"title": "Hit a size", "group": "Goals",
    "what": "Find one recipe whose particles meet the target diameter within the tolerance and the PDI cap, then stop.",
    "when": "You know the size you want.", "not": "You want to know every recipe that works: use Map the in-spec region."},
  "level-set": {"title": "Map the in-spec region", "group": "Goals",
    "what": "Find the whole set of recipes that meet the spec, not just one.",
    "when": "You want a robust operating window.", "not": "You only need one good recipe; Hit a size is faster."},
  "pareto": {"title": "Size vs PDI trade-off", "group": "Goals",
    "what": "Map the lowest PDI achievable at each size across a range: the Pareto front.",
    "when": "You want to know what is possible before choosing a target."},
  "benchmark": {"title": "Compare methods", "group": "Goals",
    "what": "Race next-recipe methods on the ground-truth simulator: same recipes, same score, several repeats.",
    "when": "Before a beamtime, to choose a method with evidence.", "not": "It never drives the reactor."},
  "catalogue_bic": {"group": "Fitting",
    "when": "Default. Particles that may not be plain spheres.", "not": "You need sub-second fits per curve: use the sphere fit.",
    "maths": "Each catalogue model is fitted; scale a and background b are solved exactly:  I ≈ a·F(q) + b\nBIC = χ² + k·ln n   (lowest wins; k counts every free parameter)\nR_eq = √(5/3)·R_g of the fitted shape  (= R for a sphere)",
    "scored": "Clean held-out: 100 % shapes, 0.3 % median size error. Hard held-out: 55 % shapes, 52 % sizes within 10 %. About 9 s per curve.",
    "refs": BENCH + " · Schwarz (1978), Ann. Stat. 6, 461 (BIC)"},
  "ls_sphere": {"group": "Fitting",
    "when": "You know the particles are spheres and want speed.", "not": "Rods, core-shell or aggregates: the size it reports is then meaningless.",
    "maths": "P(q) = [3 (sin qR − qR cos qR) / (qR)³]², averaged over a Schulz or log-normal size distribution; least squares in log I",
    "scored": "Spheres: about 0 % size error. Other shapes: 50 % to 3000 % error. 0.2 s per curve.", "refs": BENCH},
  "ml_regressor": {"group": "Fitting",
    "when": "A quick first look at sphere-like samples.", "not": "Sizes outside its library (radius 1 to 18 nm, so diameter 2 to 36 nm) or non-spheres.",
    "maths": "k-nearest neighbours against a library of Schulz-sphere curves; confidence from the absolute fit residual",
    "scored": "Hard held-out: 20 % shapes (spheres only), 31 % median size error.", "refs": BENCH},
  "llm_assisted": {"group": "Fitting",
    "when": "Unusual curves, interactions or aggregation, where expert judgement helps.", "not": "Without an AI login it runs as Catalogue + BIC.",
    "maths": "features + context + plot → AI names up to 3 catalogue models → each fitted → lowest BIC wins\n(the AI never sets the numbers)",
    "scored": "Not yet measured with the real AI: python tools/fitter_benchmark.py --split hard_heldout --methods llm_assisted",
    "refs": "docs/design/LLM_FITTER_PLAN.md · SasAgent, arXiv 2509.05363"},
  "random": {"group": "Next recipe", "when": "As the reference every method must beat.", "not": "A real campaign.",
    "scored": "Measure it: Compare methods.", "refs": ""},
  "gp_ei": {"group": "Next recipe", "when": "Hit a size; 5 to 30 runs; up to about 6 recipe parameters.", "not": "Many parameters or long campaigns: TuRBO.",
    "scored": "Measure it: Compare methods.", "refs": "Jones, Schonlau, Welch (1998), J. Global Optim. 13, 455"},
  "gp_ucb": {"group": "Next recipe", "when": "You want an explicit exploration knob (κ).", "not": "You have no reason to tune κ; GP + EI needs no setting.",
    "scored": "Measure it: Compare methods.", "refs": "Srinivas et al. (2010), ICML"},
  "turbo": {"group": "Next recipe", "when": "More parameters or a long campaign; searches near the best recipe.", "not": "Very few runs: the trust region has no time to adapt.",
    "scored": "Measure it: Compare methods.", "refs": "Eriksson et al. (2019), NeurIPS"},
  "bax": {"group": "Next recipe", "when": "Map the in-spec region.", "not": "Hit a size: it spends runs on the band edge, not on one hit.",
    "scored": "Measure it: Compare methods.", "refs": "Neiswanger, Wang, Ermon (2021), ICML (InfoBAX)"},
  "pareto_ehvi": {"group": "Next recipe", "when": "Size vs PDI trade-off.", "not": "A single target.",
    "scored": "Measure it: Compare methods.", "refs": "Emmerich, Giannakoglou, Naujoks (2006), IEEE TEC"},
  "ai_advisor": {"title": "AI Advisor", "group": "AI next recipe",
    "what": "The AI suggests the starting bounds and reviews progress every few runs; the optimiser picks every recipe. A suggestion inside the safety limits (reactor limits and search bounds) is applied automatically from the next recipe, logged and undoable; one outside them goes back to the AI with the limits, and is dropped if the second answer is still outside. Choose \"ask me first\" to approve each one instead. Measuring never stops for a review.",
    "when": "You want AI judgement with no change to how recipes are chosen.", "maths": "",
    "scored": "Not built yet.", "refs": ""},
  "ai_copilot": {"title": "AI Co-pilot", "group": "AI next recipe",
    "what": "The optimiser proposes its 5 best recipes; the AI picks one, using the chemistry and past results, and says why.",
    "when": "The recommended AI level: the acquisition maths still bounds every choice.",
    "maths": "candidates = top-k by acquisition;  choice = AI(candidates, history, chemistry);  fallback = top-1",
    "scored": "Not built yet; will be raced against GP + EI in Compare methods.", "refs": ""},
  "ai_pilot": {"title": "AI Pilot", "group": "AI next recipe",
    "what": "The AI writes each recipe itself, within the bounds; the optimiser is the fallback.",
    "when": "Experiments in method research only.", "not": "A beamtime, until it beats Random in Compare methods.",
    "scored": "Not built yet.", "refs": ""},
  "knowledge": {"title": "Knowledge for campaigns", "group": "Knowledge",
    "what": "One knowledge set per chemistry, kept in the Knowledge tab: cards from literature (web search, papers) and your notes, your verdicts on past AI picks, and past campaigns. You add knowledge there; AI-drafted cards wait in Drafts until you approve them. Setup step 4 only chooses which set and which parts a campaign uses, and freezes them at Start. AI fitting uses the general SAXS knowledge instead.",
    "when": "Starting recipes: any method. Every pick: AI Advisor, Co-pilot or Pilot only. Verdicts are used by the AI only.",
    "maths": "per decision:  pick the 5 most relevant pieces (chemistry match, similarity to this curve or recipe)\n→ the AI must name the pieces it used  → shown in Report, Decisions",
    "scored": "Each source stays on only if replaying past campaigns with it reaches good recipes faster than without it.",
    "not": "Safety limits. Those live only in the reactor limits; a card can narrow the search but never widen it. Text from papers or web pages is never passed to the AI as an instruction: only approved cards (setting, effect, numeric range, source) are.",
    "refs": "Design knowledge: Analyser → Knowledge tab, one set per chemistry system. General SAXS knowledge for fitting: shared with Guinier."},
  "harness": {"title": "How the design agent is harnessed", "group": "Knowledge",
    "what": "The AI next-recipe levels work inside a fixed harness, the same one the AI fitter uses: a fixed answer format, code-chosen knowledge, validation with a safe fallback, a version stamp and a full record of every decision.",
    "maths": "Co-pilot:  shortlist = top-k by acquisition  →  AI returns {index, cited IDs, reason, expected size and PDI}\nPilot:     AI returns a recipe; it must pass ParameterSpace.valid, the pump limits and the trust region\ninvalid, uncited or timed out  →  the optimiser's top-1 is used and the fallback is logged",
    "when": "Always, whenever an AI level chooses recipes.",
    "scored": "Offline: replay of past real campaigns (tuned on one, judged on another) and the simulator race. Online: Report, Decisions shows how often the AI agreed with the optimiser, fell back, and beat its own prediction.",
    "refs": "Meta-Harness (Lee et al., arXiv 2603.28052): keep full traces and accept a harness change only on held-out improvement."},
  "views": {"title": "Report views", "group": "Report",
    "what": "Convergence: best score so far. Surrogate: the GP's prediction and uncertainty across two recipe parameters. Trajectory: every recipe tried. Level set: the predicted in-spec region. Pareto front: best PDI per size.",
    "refs": "docs/PARAMETER_SPACE_AND_CONVERGENCE.md"},
}
GUIDE_ORDER = ["Scoring", "Goals", "Fitting", "Next recipe", "AI next recipe", "Knowledge", "Report"]


def page_guide(fits, opts) -> str:
    names = {**{k: (("AI-assisted structure" if k == "llm_assisted" else v["label"]), v.get("desc", "")) for k, v in fits.items()},
             **{k: (v["label"], v.get("desc", "")) for k, v in opts.items()}}
    fam = {**FIT_FAMILY, **OPT_FAMILY, "ai_advisor": "agentic", "ai_copilot": "agentic", "ai_pilot": "agentic"}
    nav, pages = [], []
    for grp in GUIDE_ORDER:
        ids = [i for i, g in GUIDE.items() if g["group"] == grp]
        nav.append(f'<div class="gd-grp">{grp}</div>')
        for i in ids:
            g = GUIDE[i]
            title = g.get("title") or names.get(i, (i, ""))[0]
            what = g.get("what") or names.get(i, ("", ""))[1]
            maths = g.get("maths", "")
            acq = (opts.get(i) or {}).get("acq") or {}
            if acq.get("formula"):
                maths = f"{acq['name']}\n{acq['formula']}\npicks {acq.get('picks', '')}"
            params = (opts.get(i) or {}).get("params") or []
            tag = f'<span class="fam">{FAMILY_NAMES[fam[i]]}</span>' if i in fam else ""
            rows = [("What it does", what), ("When to use", g.get("when", "")), ("When not to", g.get("not", ""))]
            body = "".join(f"<dt>{k}</dt><dd>{_esc(v)}</dd>" for k, v in rows if v)
            if maths:
                body += f"<dt>The maths</dt><dd><pre>{_esc(maths)}</pre></dd>"
            if params:
                body += "<dt>Settings</dt><dd>" + "<br>".join(
                    f"{_esc(p['label'])}: default {p['default']} ({_esc(p.get('help', ''))})" for p in params) + "</dd>"
            if g.get("scored"):
                body += f"<dt>How it scored</dt><dd>{_esc(g['scored'])}</dd>"
            if g.get("refs"):
                body += f"<dt>Read more</dt><dd class='sub'>{_esc(g['refs'])}</dd>"
            nav.append(f'<button class="gd-item" data-g="{i}">{_esc(title)}</button>')
            pages.append(f'<div class="gd-page" id="gd-{i}" style="display:none"><h3>{_esc(title)} {tag}</h3><dl>{body}</dl></div>')
    return f"""
  <section class="page" id="page-guide">
    <div class="gd"><nav class="gd-nav">{''.join(nav)}</nav><div class="card gd-body">{''.join(pages)}</div></div>
  </section>"""


def _i(gid, text="Details in the Guide"):
    return f'<a href="#" class="mk-i" data-guide="{gid}">{text} →</a>'



# ── Knowledge tab: design knowledge, organised in sets per chemistry system ─

from src.optimizer.space import NAMES as RECIPE_PARAMS  # noqa: E402

KSETS = [("pd", "Pd · ODE · oleylamine"), ("au", "Au rods · water")]
KSECTIONS = [("add", "Add knowledge", ""), ("cards", "Cards", "10"), ("cases", "Your verdicts", "3"), ("camps", "Campaigns", "2"),
             ("drafts", "Drafts", "1"), ("help", "Evaluation", "")]


def page_knowledge() -> str:
    sets = "".join(f'<button class="gd-item kn-set{" on" if i == 0 else ""}">{_esc(n)}</button>'
                   for i, (_k, n) in enumerate(KSETS))
    secs = "".join(f'<button class="gd-item kn-sec{" on" if i == 0 else ""}" data-k="{k}">{_esc(lab)}'
                   + (f' <span class="kn-n">{c}</span>' if c else "") + "</button>"
                   for i, (k, lab, c) in enumerate(KSECTIONS))
    return f"""
  <section class="page" id="page-knowledge">
    <div class="gd"><nav class="gd-nav">
      <div class="gd-grp">Knowledge sets</div>{sets}
      <button class="gd-item sub">+ New set</button>
      <div class="gd-grp">This set</div>{secs}
      <div class="kn-foot sub">Design knowledge only. General SAXS knowledge (rules, catalogue, literature, fitting cases) stays shared with Guinier.</div>
    </nav><div class="card gd-body">
      <div class="kn-head"><h3>Pd · ODE · oleylamine</h3>
        <span class="sub">version 4 · used by Run 2, Run 3</span>
        <span class="kn-hbtn"><button class="btn btn-ghost btn-sm">Export</button><button class="btn btn-ghost btn-sm">Import</button></span></div>

      <div class="kn-page" id="kn-add">
        <div class="mk-src">
          <div class="mk-srcbox"><b>{_ico("swaxs-ui-search")} Search the web for a target</b>
            <div class="cell"><input value="Pd nanoparticles 5 nm low PDI ODE oleylamine flow synthesis"></div>
            <div class="sub">Made from the target and chemistry.yml. Reads abstracts; full text only if open access.</div>
            <button class="btn btn-ghost btn-sm" onclick="mkWeb()">Search</button><div id="mkWebRes"></div></div>
          <div class="mk-srcbox"><b>{_ico("swaxs-ui-document")} Papers</b>
            <div class="mk-drop">Drop PDFs here, or paste DOIs</div>
            <div class="sub">The AI reads them and drafts cards.</div></div>
          <div class="mk-srcbox"><b>{_ico("swaxs-ui-log")} Your notes</b>
            <textarea rows="3" placeholder="above 300 °C the ODE boils in the coil"></textarea>
            <div class="sub">One line, one card. The AI matches each line to a recipe setting ({", ".join(RECIPE_PARAMS)}).</div></div>
        </div>
        <div class="btn-row"><button class="btn btn-accent btn-sm" onclick="mkBuild()">Make cards</button>
          <span class="sub" id="mkBuildMsg">New cards go to Drafts for your approval.</span></div>
      </div>

      <div class="kn-page" id="kn-cards" style="display:none">
        <div class="kn-chips"><button class="kn-f on" data-f="">All</button><button class="kn-f" data-f="literature">Literature</button><button class="kn-f" data-f="note">Your notes</button></div>
        <div class="sub">Every range is checked against the reactor limits. A card can narrow the search, never widen it; a safety limit belongs in the reactor limits, not in a card.</div>
        <table class="mk-tbl" id="knTbl"><thead><tr><th>ID</th><th>Parameter</th><th>Effect</th><th>Range</th><th>From</th><th>Source</th></tr></thead><tbody>
          <tr data-src="literature"><td>L12</td><td>T_reac</td><td>higher → larger particles, about +0.3 nm per 10 °C; PDI rises above 290 °C</td><td>240 to 300 °C</td><td><span class="kn-src">literature</span></td><td>Smith 2021, Fig. 3</td></tr>
          <tr data-src="literature"><td>L18</td><td>x_oley</td><td>more → smaller, narrower particles</td><td>0.05 to 0.30</td><td><span class="kn-src">literature</span></td><td>Lee 2019, Table 2</td></tr>
          <tr data-src="literature"><td>L32</td><td>x_TOP</td><td>more TOP narrows the distribution at 260 °C</td><td>0.10 to 0.40 <span class="mk-warn">clipped to 0.10 to 0.30 (reactor limit)</span></td><td><span class="kn-src">literature</span></td><td>Smith 2021, Fig. 4</td></tr>
          <tr data-src="literature"><td>L40</td><td>T_reac</td><td>seed-mediated growth above 280 °C widens PDI</td><td>above 280 °C</td><td><span class="kn-src">literature</span></td><td>Chen 2023 (web search)</td></tr>
          <tr data-src="note"><td>N01</td><td>F_tot</td><td>below 50 µL/min: residence time too long, aggregation</td><td>50 to 120 µL/min</td><td><span class="kn-src">your note</span></td><td>Run 1, r007</td></tr>
          <tr data-src="note"><td>N02</td><td>T_reac</td><td>above 300 °C the ODE boils in the coil</td><td>max 300 °C <span class="mk-warn">sounds like a limit</span> <button class="btn btn-ghost btn-sm mk-mini" onclick="alert('Opens the reactor limits with T_reac max = 300 °C for you to confirm.')">Make it a reactor limit</button></td><td><span class="kn-src">your note</span></td><td>A. Maurya</td></tr></tbody></table>
      </div>

      <div class="kn-page" id="kn-cases" style="display:none">
        <table class="mk-tbl"><thead><tr><th>Recipe</th><th>Chosen by</th><th>Verdict</th><th>Why</th></tr></thead><tbody>
          <tr><td>r013</td><td>AI Co-pilot</td><td>bad call</td><td>lower T did not narrow PDI (0.12, over spec)</td></tr>
          <tr><td>r011</td><td>AI Co-pilot</td><td>bad call</td><td>F_tot too low, aggregated (see N01)</td></tr>
          <tr><td>r006</td><td>GP + EI</td><td>good call</td><td>moved towards 5 nm at PDI 0.08</td></tr></tbody></table>
        <div class="sub">From Report → Decisions (Good call / Bad call). Your own verdicts count straight away.</div>
      </div>

      <div class="kn-page" id="kn-camps" style="display:none">
        <table class="mk-tbl"><thead><tr><th>Campaign</th><th>Runs</th><th>Best</th><th>Project</th><th>Use</th></tr></thead><tbody>
          <tr><td>Run 2</td><td>24</td><td>4.96 nm, PDI 0.09</td><td>this project</td><td>✓</td></tr>
          <tr><td>Run 1</td><td>18</td><td>5.30 nm, PDI 0.14</td><td>this project</td><td>✓</td></tr></tbody></table>
        <div class="btn-row"><button class="btn btn-ghost btn-sm">Add a campaign from another project…</button></div>
        <div class="sub">Finished campaigns join the set automatically. They give the ML optimisers a warm start and the AI its experience.</div>
      </div>

      <div class="kn-page" id="kn-drafts" style="display:none">
        <div class="kn-item"><div class="kn-kind">Draft card</div>
          <div><b>x_TOP</b>: raising TOP:Pd from 2 to 6 narrows the PDI (0.18 → 0.09) at 260 °C
            <div class="sub">drafted by the AI from Smith et al. 2021, Fig. 4</div></div>
          <div class="kn-act"><button class="btn btn-accent btn-sm">Approve</button><button class="btn btn-ghost btn-sm">Edit</button><button class="btn btn-ghost btn-sm">Reject</button></div></div>
        <div class="sub">Only AI drafts wait here; nothing is used until approved. Each draft shows the passage it came from, so you can check it.</div>
      </div>

      <div class="kn-page" id="kn-help" style="display:none">
        <details class="kn-adv"><summary><b>Advanced: harness settings</b> <span class="sub">version 4 · design_harness.yml · locked while Run 3 runs</span></summary>
        <div class="grid2">
          <div class="fg"><label>Shortlist size</label><div class="cell"><input type="number" value="5"></div></div>
          <div class="fg"><label>Cards per decision</label><div class="cell"><input type="number" value="3"></div></div>
          <div class="fg"><label>Cases per decision</label><div class="cell"><input type="number" value="3"></div></div>
          <div class="fg"><label>Pilot trust region</label><div class="cell"><select><option>inside GP 95 % band</option><option>within 20 % of the best recipe</option></select></div></div>
          <div class="fg"><label>Prompt version</label><div class="cell"><input value="design-1" readonly></div></div>
          <div class="fg"><label>AI model</label><div class="cell"><select><option>gateway default</option></select></div></div>
        </div></details>
        <div class="btn-row"><button class="btn btn-accent btn-sm">Run evaluation</button>
          <button class="btn btn-ghost btn-sm">Propose improvement</button>
          <a href="#" class="mk-i mk-tab" data-tab="report">Results in Report →</a></div>
        <div class="sub">Replays past campaigns with and without each part of the knowledge. A proposed change is kept only if it does better on a campaign it was not tuned on, and only after you approve it. Settings are locked while a campaign runs.</div>
      </div>
    </div></div>
  </section>"""

# ── Setup ────────────────────────────────────────────────────────────────────

def _opt(kind, mid, label, desc, tag="", extra=""):
    t = f' <i class="mk-tag {"warn" if tag == "experimental" else "rec" if "recommended" in tag else ""}">{_esc(tag)}</i>' if tag else ""
    return (f'<label class="mk-opt" data-kind="{kind}" data-id="{mid}"{extra}>'
            f'<input type="radio" name="mk-{kind}"><span><b>{_esc(label)}</b>{t}'
            f'<span class="sub">{_esc(desc)} {_i(mid)}</span></span></label>')


def chooser_fit(fits) -> str:
    out, last = [], None
    for mid in FIT_ORDER:
        if mid not in fits:
            continue
        fam = FIT_FAMILY[mid]
        if fam != last:
            out.append(f'<div class="mk-fam" data-fam-f="{fam}">{FAMILY_NAMES[fam]}</div>')
            last = fam
        label = "AI-assisted structure" if mid == "llm_assisted" else fits[mid]["label"]
        desc = fits[mid].get("desc", "").replace(" (coming soon)", "")
        out.append(_opt("fit", mid, label, desc, FIT_TAG.get(mid, "")))
    return "".join(out)


def chooser_opt(opts) -> str:
    out = []
    for fam in ("standard", "ml"):
        items = [m for i, m in opts.items() if OPT_FAMILY.get(i) == fam]
        out.append(f'<div class="mk-fam" data-fam-h="{fam}">{FAMILY_NAMES[fam]}</div>')
        for m in items:
            tag = "baseline" if m["id"] == "random" else ("recommended" if m["id"] == "gp_ei" else "")
            out.append(_opt("opt", m["id"], m["label"], m.get("desc", ""), tag))
    out.append('<div class="mk-fam" data-fam-h="agentic">Agentic</div>')
    for mid, label, desc, tag in AI_LEVELS:
        out.append(_opt("opt", mid, "AI " + label, desc, tag))
    return "".join(out)


def page_setup(fits, opts) -> str:
    return f"""
  <section class="page active" id="page-setup">
   <div class="mk-two">
    <div class="card">
      <div class="card-hd">{_ico("swaxs-ui-mode-autonomous")} <span id="mkGoalTitle">Hit a size</span> <span class="hd-right sub"><span id="mkGoalLine"></span> <a href="#" class="mk-i" id="mkGoalI" data-guide="target-hit">About this goal →</a></span></div>

      <div class="step"><div class="step-h"><span class="step-n">1</span> Data folders</div>
        <div class="fg mk-live"><label>Watched profiles</label><div class="cell"><div class="browse-row"><input value="1D/SAXS/Subtracted/Good"><button class="btn btn-ghost btn-sm">Browse…</button></div></div></div>
        <div class="fg mk-live"><label>Conditions</label><div class="cell"><div class="browse-row"><input value="1D/SAXS/Conditions" readonly><span class="mk-ok">same as the reactor</span></div></div></div>
        <div class="sub mk-race" style="display:none">Compare methods runs on the simulator only: no folders, no reactor.</div>
      </div>

      <div class="step"><div class="step-h"><span class="step-n">2</span> Target &amp; limits <span class="step-sub">{_i("score", "How the score works")}</span></div>
        <div class="grid2">
          <div class="fg g-target"><label>Target diameter (nm)</label><div class="cell"><input type="number" value="5"></div></div>
          <div class="fg g-target"><label>Tolerance ± (nm)</label><div class="cell"><input type="number" value="0.6"></div></div>
          <div class="fg g-range" style="display:none"><label>Min diameter (nm)</label><div class="cell"><input type="number" value="2"></div></div>
          <div class="fg g-range" style="display:none"><label>Max diameter (nm)</label><div class="cell"><input type="number" value="10"></div></div>
          <div class="fg"><label>Max PDI (σ/R̄, from the SAXS fit)</label><div class="cell"><input type="number" value="0.1"></div></div>
          <div class="fg mk-live"><label>Expected shape</label><div class="cell"><select><option>sphere (core-shell counts)</option><option>sphere only</option><option>any shape (equivalent size)</option></select></div></div>
          <div class="fg mk-live"><label>Min fit confidence</label><div class="cell"><input type="number" value="0.5" step="0.1"></div></div>
          <div class="fg"><label id="mkBudgetLbl">Max experiments</label><div class="cell"><input type="number" value="30"></div></div>
          <div class="fg g-race" style="display:none"><label>Repeats per method</label><div class="cell"><input type="number" value="5"></div></div>
        </div>
        <div class="fg mk-live"><label>Search range</label><div class="cell"><span class="mk-range">T 180 to 300 °C · F_tot 40 to 120 µL/min · x_ODE, x_TOP, x_oley each 0 to 0.3, sum ≤ 0.9</span>
          <span class="sub">from reactor/config.yml (bounds); hard safety caps T 320 °C, F_tot 150 µL/min. Change them in the Reactor app.</span></div></div>
        <div class="mk-read" id="mkRead"></div></div>

      <div class="step"><div class="step-h"><span class="step-n">3</span> How it fits &amp; searches</div>
        <div class="mk-choose">
          <div id="mkFitCol"><div class="mk-colh">Fitting</div><div class="mk-list" id="mkFitList">{chooser_fit(fits)}</div></div>
          <div><div class="mk-colh" id="mkOptH">Next recipe</div><div class="mk-list" id="mkOptList">{chooser_opt(opts)}</div></div>
        </div>
        <div class="mk-detail" id="mkDetail"></div>
      </div>

      <div class="step" id="mkKnow"><div class="step-h"><span class="step-n">4</span> Knowledge for this campaign
          <span class="step-sub">{_i("knowledge", "How it is used")}</span></div>
        <div class="fg"><label>Knowledge set</label><div class="cell mk-kset">
          <select id="mkKset" style="width:auto;min-width:260px" onchange="mkRecheck()"><option>Pd · ODE · oleylamine (10 cards)</option><option>Au rods · water (2 cards)</option><option>None</option></select>
          <a href="#" class="mk-i mk-tab" data-tab="knowledge">Add or edit in the Knowledge tab →</a></div></div>
        <div class="mk-use"><div class="mk-colh">Use</div><div class="mk-usel">
          <label class="mk-tick"><input type="checkbox" checked> Literature <span class="sub">4 cards · off: no paper-based starting range</span></label>
          <label class="mk-tick"><input type="checkbox" checked> Your notes <span class="sub">2 cards · off: your own observations are ignored</span></label>
          <label class="mk-tick k-ai"><input type="checkbox" checked> Your verdicts on past picks <span class="sub">3 · the AI learns which moves worked</span></label>
          <label class="mk-tick" id="mkPast"><input type="checkbox" checked> Past campaigns <span class="sub">Run 1, Run 2 · off: the optimiser starts cold</span></label>
        </div></div>
        <div class="mk-use"><div class="mk-colh">For</div><div class="mk-usel">
          <label class="mk-tick"><input type="radio" name="mk-kuse" checked> Starting recipes</label>
          <label class="mk-tick k-ai"><input type="radio" name="mk-kuse"> <span id="mkForAI">Starting recipes and every AI pick</span></label>
        </div></div>
        <div class="mk-colh">Starting recipes</div>
        <table class="mk-tbl mk-start"><thead><tr><th>Kind</th><th>How many</th><th>Where they go</th></tr></thead><tbody>
          <tr><td>From knowledge</td><td><input type="number" class="mk-n" id="mkNseed" value="3" min="0" max="10"></td><td class="sub">inside the range the ticked cards suggest</td></tr>
          <tr><td>Spread out</td><td><input type="number" class="mk-n" id="mkNsob" value="7" min="0" max="20"></td><td class="sub">evenly over the full search range (step 2), so a wrong card cannot trap the search</td></tr>
          <tr><td>Your own</td><td><input type="number" class="mk-n" id="mkNown" value="0" min="0" max="5" readonly></td><td class="sub">exact recipes you type, e.g. repeat Run 2's best as a check <button class="btn btn-ghost btn-sm mk-mini" onclick="mkAddOwn()">Add recipe</button></td></tr>
          <tr class="mk-tot"><td><b>Total</b></td><td><b id="mkNtot">10</b></td><td class="sub" id="mkNmsg">then the optimiser picks from the measured results</td></tr>
        </tbody></table>
        <div id="mkOwn"></div>
        <div class="btn-row"><button class="btn btn-ghost btn-sm" onclick="mkSuggest()">Preview starting recipes</button>
          <span class="sub">Optional. Shows the first recipes the reactor will make, before any data exists, and which cards chose them. Check them for a misread card (wrong units, odd temperature) before the pumps start; Start makes the same recipes whether or not you look.</span></div>
        <div id="mkStartPt"></div>
        <div class="sub">Frozen when the campaign starts, so every decision uses the same knowledge. Knowledge can only narrow the search range, never widen it; the reactor limits always apply.</div>
      </div>

      <div class="btn-row"><button class="btn btn-accent" id="mkStart">{_ico("swaxs-ui-start")} <span id="mkStartLbl">Start campaign</span></button>
        <a href="#" class="mk-i mk-live mk-cont" onclick="return false">or continue Run 3 (14 of 30 runs, knowledge v4 frozen; the set is now v5: keep v4 or switch)</a></div>
    </div>

    <div>
      <div class="card"><div class="card-hd">{_ico("swaxs-ui-pass")} Ready to start?</div>
        <ul class="mk-check" id="mkCheck"></ul>
        <div class="sub">Start stays disabled until every essential is green, and says which one is not.</div></div>
    </div>
   </div>
  </section>"""


def page_report() -> str:
    return f"""
  <section class="page" id="page-report">
    <div class="card">
      <div class="card-hd">{_ico("swaxs-ui-gauge")} Campaign <span class="hd-right sub"><span id="mkRepLine"></span> · knowledge v4 (frozen)
        <button class="btn btn-ghost btn-sm">Pause</button><button class="btn btn-ghost btn-sm mk-danger" onclick="confirm('Abort Run 3? The reactor finishes the current recipe and flushes.')">{_ico("swaxs-ui-stop")} Abort</button></span></div>
      <div class="bar"><i style="width:47%"></i></div>
      <div class="kpis">
        <div class="kpi"><span>Status</span><b>running</b></div>
        <div class="kpi"><span>Runs</span><b>14 / 30</b></div>
        <div class="kpi hit"><span>Best diameter</span><b>4.96 nm</b></div>
        <div class="kpi"><span>Δ to target</span><b>−0.04 nm</b></div>
        <div class="kpi hit"><span>Best PDI</span><b>0.09</b></div>
        <div class="kpi"><span>Best recipe</span><b>r014</b></div>
      </div>
    </div>
    <div class="card"><div class="card-hd">{_ico("swaxs-ui-chart")} Views <span class="hd-right">{_i("views", "What each view shows")}</span></div>
      <div class="viewtabs" id="mkViews"></div>
      <div class="ph">figure for the selected view</div></div>
    <div class="card"><div class="card-hd">{_ico("swaxs-ui-log")} Decisions <span class="hd-right">{_i("harness", "How AI decisions are checked")}</span></div>
      <div class="kpis mk-dq">
        <div class="kpi"><span>AI picked</span><b>6 of 14</b></div>
        <div class="kpi"><span>Same as optimiser's top</span><b>4 of 6</b></div>
        <div class="kpi"><span>Fallbacks</span><b>0</b></div>
        <div class="kpi"><span>AI picks beat prediction</span><b>3 of 6</b></div>
        <div class="kpi"><span>Knowledge</span><b>Pd v4</b></div>
      </div>
      <table class="mk-tbl"><thead><tr><th>Recipe</th><th>Chosen by</th><th>Why</th><th>Based on</th><th>Result</th><th>Your verdict</th></tr></thead><tbody>
        <tr><td>after r010</td><td>AI Advisor (review)</td><td>widen x_TOP 0.10 to 0.25 → 0.10 to 0.30: PDI stuck at 0.12</td><td><b>L32</b> Smith 2021</td><td>within limits: applied automatically</td>
          <td><button class="btn btn-ghost btn-sm mk-mini">Undo</button></td></tr>
        <tr><td>r014</td><td>GP + EI</td><td>EI 0.42, near the best recipe</td><td class="sub">acquisition</td><td>4.96 nm, PDI 0.09</td>
          <td class="sub">n/a</td></tr>
        <tr><td>r013</td><td>AI Co-pilot (5 shortlisted)</td><td>lower T to reduce PDI; same flow</td><td><b>L12</b> Smith 2021 · Run 2 r009</td><td>5.42 nm, PDI 0.12</td>
          <td><button class="btn btn-ghost btn-sm mk-mini">Good call</button> <button class="btn btn-ghost btn-sm mk-mini">Bad call</button> <a href="#" class="mk-i mk-ext">Ask Guinier ↗</a></td></tr></tbody></table>
      <div class="sub">Good call or Bad call (with one line why) is saved to the campaign's knowledge set (Knowledge tab).</div></div>
    <div class="card"><div class="card-hd">{_ico("swaxs-quality")} Did the knowledge help? <span class="hd-right">{_i("harness", "How this is tested")}</span></div>
      <div class="mk-note">Only 2 past campaigns (tuned on Run 1, judged on Run 2): indicative only until there are more.</div>
      <table class="mk-tbl"><thead><tr><th>Method</th><th>Runs to reach spec (replay)</th><th>Best score after 10</th><th>Simulator race (20 seeds)</th><th>Fallbacks</th></tr></thead><tbody>
        <tr><td>GP + EI</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">n/a</td></tr>
        <tr><td>GP + EI + past campaigns</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">n/a</td></tr>
        <tr><td>AI Co-pilot + cards</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">placeholder</td></tr>
        <tr><td>AI Pilot + cards</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">placeholder</td><td class="sub">placeholder</td></tr></tbody></table></div>
    <div class="card"><div class="card-hd">{_ico("swaxs-ui-download")} Downloads</div>
      <div class="btn-row"><button class="btn btn-ghost btn-sm">history.csv</button><button class="btn btn-ghost btn-sm">Figures</button><button class="btn btn-ghost btn-sm">All fit records</button><button class="btn btn-ghost btn-sm">Fitter benchmark</button></div>
      <div class="sub">From 1D/SAXS/Results/campaign_&lt;id&gt;/ and Results/Fit/.</div></div>
  </section>"""


def sidebar() -> str:
    btns = "".join(
        f'<button class="nav-btn{" active" if i == 0 else ""}" data-goal="{g}">'
        f'<span class="nav-icon">{_ico(icon)}</span> {_esc(label)}</button>'
        for i, (g, label, icon, _d, _k) in enumerate(GOALS))
    return (f'<div id="app"><nav id="sidebar"><div class="nav-section-label">Goal</div>{btns}'
            '<div class="nav-hint" id="mkHint"></div><div class="nav-hint" id="mkPair"></div>'
            '<div class="nav-hint">Mockup: layout only. Nothing runs or saves.</div></nav><div id="main">')


CSS = """
.card-hd .hd-right{text-transform:none;letter-spacing:normal;font-weight:400;font-size:13px}
body.swaxs-shell #main{overflow-y:auto}
.mk-two{display:grid;grid-template-columns:minmax(0,1fr) minmax(260px,330px);gap:14px;align-items:start}
.mk-fit{display:grid;grid-template-columns:minmax(320px,440px) minmax(0,1fr);gap:14px;align-items:start}
@media (max-width:1100px){.mk-two,.mk-fit{grid-template-columns:1fr}}
.mk-ok{color:var(--ok-text);font-size:13px;white-space:nowrap;align-self:center}
.mk-read{margin-top:8px;padding:8px 10px;border-radius:8px;background:var(--surface2);font-size:14px}
.mk-choose{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:900px){.mk-choose{grid-template-columns:1fr}}
.mk-colh{font-weight:700;font-size:15px;margin:2px 0 6px}
.mk-list{border:1px solid var(--border);border-radius:10px;padding:4px 0}
.mk-fam{padding:8px 12px 2px;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.mk-opt{display:flex!important;gap:10px;align-items:flex-start;padding:7px 12px;margin:0;cursor:pointer;grid-template-columns:none!important}
.mk-opt:hover{background:var(--surface2)}
.mk-start{max-width:720px}.mk-start .mk-n{width:70px}.mk-tot td{border-top:1px solid var(--border)}
.mk-own{display:grid;grid-template-columns:repeat(5,minmax(0,1fr)) auto;gap:6px;margin:4px 0;max-width:720px}
.mk-range{font-family:var(--mono);font-size:14px}
.mk-warn{color:var(--warn-text);font-size:13px}
.mk-note{border:1px solid var(--warn);border-radius:8px;padding:6px 10px;margin:0 0 8px;font-size:13px}
.mk-danger{color:var(--err-text)!important}
.mk-cont{margin-left:8px}
.mk-opt input{margin-top:4px;flex-shrink:0;width:auto}
.mk-opt>span{display:block}.mk-opt>span>.sub{display:none;margin-top:2px;line-height:1.4}
.mk-opt.on{background:color-mix(in srgb,var(--accent) 10%,transparent)}.mk-opt.on>span>.sub{display:block}
.mk-tag{font-style:normal;font-size:11px;padding:0 6px;margin-left:6px;border-radius:999px;border:1px solid var(--border);color:var(--muted)}
.mk-tag.rec{border-color:var(--ok);color:var(--ok-text)}.mk-tag.warn{border-color:#b45309;color:var(--warn-text,#b45309)}
.mk-detail{margin-top:10px}.mk-detail:empty{display:none}
.mk-detail .box{border:1px dashed var(--border);border-radius:10px;padding:10px 12px;margin-top:8px;font-size:14px;line-height:1.5}
.mk-detail code{display:block;margin:3px 0;white-space:pre-wrap}
.mk-check{list-style:none;padding:0;margin:0 0 8px}.mk-check li{padding:5px 0 5px 24px;position:relative;font-size:14px}
.mk-check li .ico{position:absolute;left:0;top:7px;width:16px;height:16px}
.mk-check li.ok .ico{color:var(--ok-text)}.mk-check li.warn .ico{color:var(--warn-text,#b45309)}
.mk-tbl{width:100%;border-collapse:collapse;font-size:14px}.mk-tbl th,.mk-tbl td{padding:5px 6px;border-bottom:1px solid var(--border);text-align:left}
.mk-tbl tr.sel td{background:color-mix(in srgb,var(--accent) 12%,transparent)}
.mk-conf{padding:1px 7px;border-radius:999px;font-weight:700}.mk-conf.hi{color:var(--ok-text)}.mk-conf.mid{color:var(--warn-text,#b45309)}.mk-conf.lo{color:var(--err-text)}
.mk-plot{width:100%;height:auto;margin:6px 0 10px}
.card .card.fold{margin:8px 0 0;padding:10px 12px}
.ph{display:flex;align-items:center;justify-content:center;height:180px;border:1px dashed var(--border);border-radius:8px;color:var(--muted);margin-top:8px}
.scalebar .on,.viewtabs .on{color:var(--accent);font-weight:700}
.mk-know{margin-top:12px;border:1px solid var(--border);border-radius:10px;padding:10px 14px}
.mk-know-h{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.mk-ktbl{width:100%;border-collapse:collapse;font-size:14px}.mk-ktbl th{text-align:left;font-weight:700;color:var(--muted);width:130px;padding:6px 8px 6px 0;vertical-align:top}
.mk-ktbl td{padding:6px 0;vertical-align:top}.mk-ktbl td:last-child{text-align:right;width:80px}
.mk-use{display:grid;grid-template-columns:56px minmax(0,1fr);gap:8px;align-items:start;margin:8px 0}.mk-use .mk-colh{margin:0}
.mk-usel{display:flex;flex-direction:column;gap:4px}
.mk-tick{display:inline-flex!important;gap:6px;align-items:center;margin:0 14px 0 0;grid-template-columns:none!important;font-weight:400!important}
.mk-tick input{width:auto}
.mk-cards{margin:0;padding-left:18px;font-size:13px;line-height:1.6}
.kn-meta{display:block;font-size:12px;font-weight:400}.kn-n{float:right;font-size:12px;color:var(--muted)}
.kn-foot{padding:10px 14px;line-height:1.45}
.kn-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}.kn-head h3{margin:0;font-size:18px}
.kn-hbtn{margin-left:auto;display:flex;gap:6px}
.kn-chips{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px}
.kn-f{border:1px solid var(--border);background:transparent;color:var(--txt);border-radius:999px;padding:2px 10px;font-size:13px;cursor:pointer}
.kn-f.on{border-color:var(--accent);color:var(--accent-text);font-weight:700}
.kn-src{font-size:12px;padding:1px 8px;border-radius:999px;background:var(--surface2);white-space:nowrap}
.kn-sub2{margin:12px 0 6px;font-weight:700}
.mk-dq{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin:6px 0 10px}
.kn-form{margin-top:14px;padding:10px 12px;border:1px dashed var(--border);border-radius:10px}
.kn-item{display:grid;grid-template-columns:110px minmax(0,1fr) auto;gap:12px;align-items:start;padding:10px 0;border-bottom:1px solid var(--border);font-size:14px;line-height:1.5}
.kn-kind{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.kn-act{display:flex;gap:6px;flex-wrap:wrap}
.mk-kset{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:4px 0 8px}
.mk-src{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin:6px 0 10px}
.mk-srcbox{border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;align-items:flex-start}
.mk-srcbox>*{max-width:100%}.mk-srcbox .cell,.mk-srcbox textarea,.mk-drop{width:100%;box-sizing:border-box}
.mk-srcbox b{display:flex;gap:6px;align-items:center}
.mk-drop{border:1.5px dashed var(--border);border-radius:8px;padding:14px;text-align:center;color:var(--muted);font-size:14px}
.mk-hit{display:flex;gap:6px;align-items:flex-start;font-size:13px;margin:2px 0}.mk-hit input{width:auto;margin-top:3px}
.mk-rv{border:1px solid var(--border);border-radius:10px;padding:8px 12px;margin:8px 0}
#mkKnow .mk-colh{margin-top:10px}
.mk-verdict{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0 4px;font-size:14px}
.mk-verdict .lbl{color:var(--muted);font-weight:700}
.mk-mini{padding:2px 8px;font-size:12px}
.mk-i{color:var(--accent-text);text-decoration:none;font-size:13px;white-space:nowrap;font-weight:600}
.mk-i:hover{text-decoration:underline}
.gd{display:grid;grid-template-columns:240px minmax(0,1fr);gap:14px;align-items:start}
@media (max-width:900px){.gd{grid-template-columns:1fr}}
.gd-nav{border:1px solid var(--border);border-radius:var(--radius);background:var(--surface);padding:6px 0;position:sticky;top:0}
.gd-grp{padding:10px 14px 3px;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.gd-item{display:block;width:100%;text-align:left;border:none;border-left:3px solid transparent;background:transparent;color:var(--txt);padding:6px 14px;font-size:14px;cursor:pointer}
.gd-item:hover{background:var(--surface2)}.gd-item.on{border-left-color:var(--accent);color:var(--accent-text);font-weight:700}
.gd-page h3{margin:0 0 10px;font-size:18px}.gd-page dl{margin:0;display:grid;grid-template-columns:130px minmax(0,1fr);gap:8px 14px}
.gd-page dt{font-weight:700;color:var(--muted);font-size:14px}.gd-page dd{margin:0;font-size:14px;line-height:1.55}
.gd-page pre{margin:0;padding:8px 10px;border-radius:8px;background:var(--surface2);white-space:pre-wrap;font-size:13px}
.fam{font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;margin-left:6px;color:var(--accent-text);vertical-align:middle}
"""


def script(fits, opts) -> str:
    goal_methods = {g: list(M.GOAL_METHODS.get(g, [])) for g, *_ in GOALS}
    ml_by_goal = {g: [m for m in ids if OPT_FAMILY.get(m) == "ml"] for g, ids in goal_methods.items()}
    data = {
        "fit": {k: {"label": ("AI-assisted structure" if k == "llm_assisted" else v["label"]),
                    "fam": FIT_FAMILY.get(k)} for k, v in fits.items()},
        "opt": {**{k: {"label": v["label"], "fam": OPT_FAMILY.get(k), "acq": v.get("acq"),
                       "params": v.get("params") or []} for k, v in opts.items()},
                **{mid: {"label": "AI " + lab, "fam": "agentic"} for mid, lab, _d, _t in AI_LEVELS}},
        "optLabel": {k: v["label"] for k, v in opts.items()},
        "goals": {g: {"label": lab, "desc": d, "kind": k} for g, lab, _i, d, k in GOALS},
        "goalMethods": goal_methods, "mlByGoal": ml_by_goal, "views": REPORT_VIEWS,
        "famName": FAMILY_NAMES,
    }
    return """<script>
(function(){
const D=%s, $=id=>document.getElementById(id);
const esc=s=>String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'})[c]);
let goal='target-hit', fit='catalogue_bic', opt='gp_ei';
window.showTab=function(t){document.querySelectorAll('.toptab').forEach(b=>b.classList.toggle('active',b.dataset.tab===t));
  document.querySelectorAll('.page').forEach(p=>p.classList.toggle('active',p.id==='page-'+t));};
window.toggleTheme=window.toggleTheme||function(){const h=document.documentElement;h.dataset.theme=h.dataset.theme==='dark'?'light':'dark';};
function mark(){
  document.querySelectorAll('.mk-opt').forEach(l=>{const on=(l.dataset.kind==='fit'?fit:opt)===l.dataset.id;
    l.classList.toggle('on',on);l.querySelector('input').checked=on;});
  const usesAI = fit==='llm_assisted' || opt.startsWith('ai_');
  $('mkKnow').style.display = goal!=='benchmark' ? '' : 'none';
  // verdicts and the second "For" need an AI next-recipe level (an AI fitter alone cannot read design cards)
  const aiNext=opt.startsWith('ai_');
  document.querySelectorAll('#mkKnow .k-ai').forEach(r=>r.style.display=aiNext?'':'none');
  if(!aiNext){const r=document.querySelector('input[name=mk-kuse]');if(r)r.checked=true;}
  // Random uses no data, so past campaigns cannot help it
  $('mkPast').style.display=opt==='random'?'none':'';
  // Advisor reviews progress; the optimiser still picks every recipe
  $('mkForAI').textContent=opt==='ai_advisor'?'Starting recipes and every progress review':'Starting recipes and every AI pick';
  document.querySelectorAll('.mk-ai-only').forEach(e=>e.style.display=usesAI&&goal!=='benchmark'?'':'none');
  // detail for the chosen next-recipe method
  let h='';
  if(opt.startsWith('ai_')){
    const ml=D.mlByGoal[goal]||[], lbl=opt==='ai_copilot'?'Shortlist from':(opt==='ai_pilot'?'Fallback optimiser':'Optimiser');
    h='<div class="box"><div class="grid2"><div class="fg"><label>'+lbl+'</label><div class="cell"><select>'+
      ml.map(m=>'<option>'+esc(D.optLabel[m])+'</option>').join('')+'</select></div></div>'+
      (opt==='ai_advisor'
        ?'<div class="fg"><label>Progress review</label><div class="cell"><select><option>every 5 runs</option><option>every 10 runs</option><option>off</option></select></div></div>'+
         '<div class="fg"><label>Apply suggestions</label><div class="cell"><select><option>automatically, if within the safety limits</option><option>ask me first</option></select></div></div></div>'+
         '<div class="sub">Measuring never stops for a review. Each suggestion is checked against the reactor limits and the search bounds. Within them: applied from the next recipe and logged in Report, Decisions, with Undo. Outside them: the AI is asked again, given the limits; if the second answer is still outside, the suggestion is dropped and the current settings stay.</div></div>'
        :'<div class="fg"><label>Progress review</label><div class="cell"><select><option>every 5 runs; measuring continues while it waits for you</option><option>every 10 runs; measuring continues while it waits for you</option>'+(opt==='ai_pilot'?'':'<option>off</option>')+'</select></div></div></div>'+
         '<div class="sub">Every AI decision and its reason appears in Report, Decisions. Bounds and pump limits are always enforced'+
         (opt==='ai_pilot'?'; the review cannot be turned off while the AI writes the recipes, and on the real rig Start asks you to confirm the limits first.':'.')+'</div></div>');
  } else { const m=D.opt[opt]||{};
    if(m.acq&&m.acq.formula) h='<div class="box"><b>'+esc(m.label)+'</b>: acquisition '+esc(m.acq.name)+'<code>'+esc(m.acq.formula)+'</code>'+
      (m.params||[]).map(p=>'<div class="fg"><label>'+esc(p.label)+'</label><div class="cell"><input type="number" value="'+p.default+'"></div></div>').join('')+'</div>'; }
  $('mkDetail').innerHTML=h;
  $('mkPair').innerHTML='<b>Fitting</b>: '+esc(D.fit[fit].label)+' ('+D.famName[D.fit[fit].fam]+')<br><b>Next recipe</b>: '+esc(D.opt[opt].label)+' ('+D.famName[D.opt[opt].fam]+')';
  checklist(usesAI);
}
function checklist(usesAI){
  const race=goal==='benchmark', li=(c,t)=>'<li class="'+c+'"><svg class="ico" aria-hidden="true"><use href="#swaxs-ui-'+(c==='ok'?'pass':'warning')+'"/></svg>'+t+'</li>';
  let h=race?li('ok','Simulator ready (ground truth from reactor/config.yml)')+li('ok','Methods to race: tick them in step 3 (AI Co-pilot and Pilot need the AI login)')
    :li('ok','Watched folder found: 14 profiles')+li('ok','Reactor reads the same Conditions folder')+li('ok','Reactor connected, Simulation mode');
  if(!race) h+=li('ok','Knowledge: Pd · ODE · oleylamine, 10 cards')+li('warn','1 new card waiting for approval in the Knowledge tab (not used until approved)');
  if(!race){const ks=($('mkKset')||{}).value||'';
    h+=ks.startsWith('None')?li('ok','No knowledge set: the optimiser starts cold')
      :(ks.startsWith('Pd')?li('ok','Knowledge set matches chemistry.yml (Pd · ODE · oleylamine)')
      :li('warn','Knowledge set "'+ks.split(' (')[0]+'" does not match chemistry.yml (Pd · ODE · oleylamine)'));
    h+=li('warn','1 new card waiting for approval in the Knowledge tab (not used until approved)');
    if(opt==='ai_pilot') h+=li('warn','AI Pilot is experimental: Start asks you to confirm the reactor limits');}
  h+=li('ok','Fitting method available');
  if(usesAI) h+=li('warn','No AI login found: the AI parts run without AI (Catalogue + BIC for fitting, the optimiser\\'s own pick for the next recipe)');
  $('mkCheck').innerHTML=h;
}
window.mkRecheck=function(){checklist(fit==='llm_assisted'||opt.startsWith('ai_'));};
window.mkWeb=function(){const hits=[['Size control of Pd nanocrystals in a continuous-flow reactor','2021','open access'],
  ['Oleylamine and TOP as ligands for monodisperse Pd','2019','abstract only'],['Seed-mediated Pd growth in ODE: temperature effects','2023','open access']];
  $('mkWebRes').innerHTML=hits.map(h=>'<label class="mk-hit"><input type="checkbox" checked><span>'+h[0]+' <span class="sub">'+h[1]+' · '+h[2]+'</span></span></label>').join('');};
function mkCount(){const g=id=>Math.max(0,+(($(id)||{}).value)||0),k=g('mkNseed'),s=g('mkNsob'),o=g('mkNown'),n=k+s+o;
  $('mkNtot').textContent=n;
  $('mkNmsg').textContent=n<5?'Too few: the optimiser needs about 10 (2 × the 5 settings) to learn the trends':(n>20?'Many: more than a third of a 30-run budget spent before the optimiser aims':(s<Math.ceil(n/2)?'Keep at least half spread out, so a wrong card cannot trap the search':'then the optimiser picks from the measured results'));
  $('mkNmsg').className=(n<5||n>20||s<Math.ceil(n/2))?'mk-warn':'sub';return {k,s,o,n};}
document.querySelectorAll('.mk-n').forEach(i=>i.oninput=mkCount);
window.mkAddOwn=function(){const box=$('mkOwn');if(box.children.length>=5)return;
  box.insertAdjacentHTML('beforeend','<div class="mk-own"><input placeholder="T (180 to 300 °C)"><input placeholder="F_tot (40 to 120)"><input placeholder="x_ODE (0 to 0.3)"><input placeholder="x_TOP (0 to 0.3)"><input placeholder="x_oley (0 to 0.3)"><button class="btn btn-ghost btn-sm mk-mini" onclick="mkDelOwn(this)">Remove</button></div>');
  $('mkNown').value=box.children.length;mkCount();};
window.mkDelOwn=function(b){b.parentElement.remove();$('mkNown').value=$('mkOwn').children.length;mkCount();};
window.mkSuggest=function(){const c=mkCount();
  $('mkStartPt').innerHTML='<div class="mk-read"><b>'+c.n+' starting recipes:</b> '+c.k+' in T 240 to 290 °C · F_tot 60 to 90 µL/min, '+c.s+' spread over the full search range'+(c.o?', '+c.o+' of your own':'')+
  '<div class="sub">'+(c.k?'the '+c.k+' from cards L12, N01, N02 and campaign Run 2 (r009, r014); ':'')+'the spread ones from a Sobol sequence; every recipe checked against the reactor limits</div>'+
  '<div class="btn-row"><button class="btn btn-accent btn-sm">Accept</button><button class="btn btn-ghost btn-sm">Edit</button></div></div>';};
window.mkBuild=function(){$('mkBuildMsg').textContent='4 new cards drafted. Approve them in Drafts.';
  const n=document.querySelector('.kn-sec[data-k=drafts] .kn-n');if(n)n.textContent='5';};
document.querySelectorAll('.kn-f').forEach(b=>b.onclick=()=>{document.querySelectorAll('.kn-f').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('#knTbl tbody tr').forEach(r=>r.style.display=!b.dataset.f||r.dataset.src===b.dataset.f?'':'none');});
function setGoal(g){goal=g;const G=D.goals[g],allowed=(g==='benchmark'?['random','gp_ei','gp_ucb','turbo']:(D.goalMethods[g]||[]));
  document.querySelectorAll('#sidebar .nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.goal===g));
  $('mkGoalTitle').textContent=G.label;$('mkGoalI').dataset.guide=g;$('mkHint').textContent=G.desc;$('mkGoalLine').textContent=G.desc;
  document.querySelectorAll('.g-target').forEach(e=>e.style.display=G.kind==='target'?'':'none');
  document.querySelectorAll('.g-range').forEach(e=>e.style.display=G.kind==='target'?'none':'');
  document.querySelectorAll('.g-race').forEach(e=>e.style.display=G.kind==='race'?'':'none');
  document.querySelectorAll('.mk-live').forEach(e=>e.style.display=G.kind==='race'?'none':'');
  document.querySelectorAll('.mk-race').forEach(e=>e.style.display=G.kind==='race'?'':'none');
  $('mkBudgetLbl').textContent=G.kind==='race'?'Experiments per run':'Max experiments';
  $('mkStartLbl').textContent=G.kind==='race'?'Run comparison':'Start campaign';
  $('mkOptH').textContent=G.kind==='race'?'Methods to race':'Next recipe';
  // the simulator race scores recipes against the ground truth directly, so no curve is fitted
  $('mkFitCol').style.display=G.kind==='race'?'none':'';
  $('mkRead').textContent=G.kind==='target'?'Aiming for 5.0 ± 0.6 nm diameter, PDI below 0.10, at most 30 runs.'
    :(G.kind==='race'?'Racing the ticked methods on simulated campaigns, 30 runs each, 5 repeats.':'Mapping diameters 2 to 10 nm with PDI below 0.10, at most 30 runs.');
  // only the next-recipe methods that suit this goal; AI levels for any live goal
  document.querySelectorAll('.mk-opt[data-kind=opt]').forEach(l=>{const id=l.dataset.id,ai=id.startsWith('ai_');
    l.style.display=(ai?(G.kind!=='race'||id!=='ai_advisor'):allowed.includes(id))?'':'none';
    l.querySelector('input').type=G.kind==='race'?'checkbox':'radio';});
  document.querySelectorAll('[data-fam-h]').forEach(h=>{const fam=h.dataset.famH;
    h.style.display=[...document.querySelectorAll('.mk-opt[data-kind=opt]')].some(l=>D.opt[l.dataset.id].fam===fam&&l.style.display!=='none')?'':'none';});
  if(!(allowed.includes(opt)||(opt.startsWith('ai_')&&G.kind!=='race'))) opt=allowed.find(m=>D.opt[m].fam==='ml')||allowed[0];
  if(G.kind==='race'){opt='gp_ei';setTimeout(()=>document.querySelectorAll('.mk-opt[data-kind=opt]').forEach(l=>{if(l.dataset.id==='random'||l.dataset.id==='gp_ei'){l.classList.add('on');l.querySelector('input').checked=true;}}),0);}
  $('mkViews').innerHTML=(D.views[g]||[]).map((v,i)=>'<button'+(i?'':' class="on"')+'>'+esc(v)+'</button>').join('');
  $('mkRepLine').textContent=G.label;
  mark();}
function guide(id){document.querySelectorAll('#page-guide .gd-item').forEach(b=>b.classList.toggle('on',b.dataset.g===id));
  document.querySelectorAll('.gd-page').forEach(p=>p.style.display=p.id==='gd-'+id?'':'none');}
document.querySelectorAll('#page-guide .gd-item').forEach(b=>b.onclick=()=>guide(b.dataset.g));
document.querySelectorAll('.kn-sec').forEach(b=>b.onclick=()=>{document.querySelectorAll('.kn-sec').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.kn-page').forEach(p=>p.style.display=p.id==='kn-'+b.dataset.k?'':'none');});
document.querySelectorAll('.kn-set').forEach(b=>b.onclick=()=>document.querySelectorAll('.kn-set').forEach(x=>x.classList.toggle('on',x===b)));
document.addEventListener('click',e=>{const i=e.target.closest('.mk-i');if(!i)return;e.preventDefault();e.stopPropagation();
  if(i.classList.contains('mk-ext')){alert('Opens Guinier (port 5109) with this item loaded.');return;}
  if(i.classList.contains('mk-tab')){showTab(i.dataset.tab);return;}
  showTab('guide');guide(i.dataset.guide);},true);
guide('catalogue_bic');
document.querySelectorAll('#sidebar .nav-btn').forEach(b=>b.onclick=()=>setGoal(b.dataset.goal));
document.querySelectorAll('.mk-opt').forEach(l=>l.onclick=e=>{e.preventDefault();
  if(l.dataset.kind==='fit')fit=l.dataset.id;else opt=l.dataset.id;mark();});
setGoal('target-hit');
})();
</script>""" % json.dumps(data)


def build(out: Path | None = None) -> Path:
    fits = {m["id"]: m for m in F.available()}
    opts = {m["id"]: m for m in M.available()}
    src = (TPL / "index.html").read_text()
    head = src[:src.index("</head>")]
    b0 = src.index("<body")
    top0, top1 = src.index('<div class="topbar">'), src.index('<div class="restart-banner"')
    pre = src[src.index(">", b0) + 1:top0]
    log = ('<div class="card fold" id="fold-log"><div class="card-hd" onclick="this.parentElement.classList.toggle(\'shut\')">'
           '<span class="chev">▾</span> Log</div><div class="foldbody"><div class="log sub">14:02 ✓ r014_sample_sub.dat: '
           'R=2.48 PDI=0.09 conf=0.86 (sphere)<br>14:02 → next recipe r015 (GP + EI)</div></div></div>')
    tabs = ('<div class="toptabbar"><button class="toptab active" data-tab="setup" onclick="showTab(\'setup\')">Setup</button>'
            '<button class="toptab" data-tab="fitting" onclick="showTab(\'fitting\')">Fitting</button>'
            '<button class="toptab" data-tab="report" onclick="showTab(\'report\')">Report</button>'
            '<button class="toptab" data-tab="knowledge" onclick="showTab(\'knowledge\')">Knowledge</button>'
            '<button class="toptab" data-tab="guide" onclick="showTab(\'guide\')">Guide</button></div>')
    side, setup, report, js, css = sidebar(), page_setup(fits, opts), page_report(), script(fits, opts), CSS
    page = (head + f"<style>{css}</style></head>\n<body class=\"swaxs-shell\">" + pre + src[top0:top1]
            + side + tabs + '<div class="wrap">' + setup + page_fitting()
            + report + page_knowledge() + page_guide(fits, opts) + log + "</div></div></div>" + js + "</body></html>\n")
    out = out or OUT
    out.write_text(_inline(page), encoding="utf-8")
    return out



if __name__ == "__main__":
    # The chosen layout: campaign goals in the left column, the three method
    # families as headings inside Setup step 3, and the Guide tab.
    p = build(OUT)
    print(p.relative_to(ROOT), p.stat().st_size // 1024, "KB")
