#!/usr/bin/env python3
"""analyzer_check.py: which Analyser functions work, in simulation, with no beam.

Three levels, each prints a PASS / FAIL table:

    python tools/analyzer_check.py              # 1. every feature, by its tests (5 to 15 min)
    python tools/analyzer_check.py --loop       # 2. + simulated closed-loop campaigns (about 5 to 10 min)
    python tools/analyzer_check.py --loop --ai  # 3. + real AI calls (AI fitter, Co-pilot); costs a few calls

1. **Features**: runs the test groups behind each function (fitting, design loop,
   campaign controls, batches, restart, knowledge bases, AI levels, AI safety,
   the page itself) and names any failing test.
2. **Closed loop** (``--loop``): a real Analyser in a throw-away project, fed by
   the simulator's hidden recipe → size model (src/simulator/ground_truth.py, the
   optimum is R = 4.0 nm at T 240 °C, x_TOP 0.15). For each design-loop method it
   runs a campaign: the Analyser proposes a recipe, the simulator "makes" it, a
   noisy SAXS curve is written to Subtracted/, the Analyser fits it, the model
   learns, the next recipe follows. Checks: the fitted size matches the true size,
   the loop never stalls or errors, and the campaign gets near the target.
   No reactor, no beam, no waiting for real flushes (the reactor's real-time
   cycle is tested separately; see docs/SIMULATED_2D_DATA.md for the full-app run).
3. **Real AI** (``--ai``): the AI login, one AI-assisted fit of a simulated curve,
   and a short AI Co-pilot campaign. Shows whether the AI answered, fell back, or
   was paused by the guard, with the time taken.

Options: ``--only knowledge`` (some features only) · ``--methods random,gp_ei`` · ``--budget 12`` · ``--fitter ls_sphere``
(faster) · ``--keep`` (keep the throw-away project to look at it).
Exit code 0 when everything run passed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"

#: function -> the tests that prove it (file or file::-k expression)
FEATURES = [
    ("Fitting methods (sphere, Catalogue + BIC, ML, AI-assisted fallback)",
     ["tests/test_fitter_phase1.py", "tests/test_fitter_phase2.py", "tests/test_fitter_phase4.py",
      "tests/test_method_registry.py", "tests/test_analyzer_units.py"]),
    ("Design-loop methods (Random, GP + EI/UCB, TuRBO, BAX, Pareto) and Compare methods",
     ["tests/test_optimizer.py", "tests/test_study_runner.py", "tests/test_optimizer_diagnostics.py"]),
    ("Campaign controls (held results, own recipes, preview, pause, search range)",
     ["tests/test_analyzer_campaign_controls.py -k 'shape or held or seed or recipe or preview or pause or search_range or expected'"]),
    ("Which batch trains the model; AI fit refines in the background",
     ["tests/test_analyzer_campaign_controls.py -k 'batch or settle or refit or ai_fit or timed_out or tie'"]),
    ("Restart, resume and Continue (the fitter stays with the campaign)",
     ["tests/test_analyzer_resume.py", "tests/test_restart_param_divergence.py",
      "tests/test_analyzer_campaign_controls.py -k 'restart or continue or fitter_cannot'"]),
    ("Knowledge bases, design part (cards, literature, verdicts, warm start)",
     ["tests/test_design_knowledge.py"]),
    ("Knowledge bases, fitter part (Guinier sources, notes, judged fits, evaluation)",
     ["tests/test_fit_knowledge.py"]),
    ("AI levels (Advisor, Co-pilot, Pilot) with validation and fallback",
     ["tests/test_ai_design.py"]),
    ("AI fitter safety (3 min cap, timeouts, pause after failures, call budget)",
     ["tests/test_ai_guard.py"]),
    ("Did the knowledge help? (replay) and the improvement loop",
     ["tests/test_replay.py"]),
    ("Long runs (bounded memory and disk, backlog, gate folder)",
     ["tests/test_analyzer_audit.py", "tests/test_analyzer_fresh_run_backlog.py",
      "tests/test_analyzer_gate_folder.py", "tests/test_quick_audit_loop.py"]),
    ("Simulator to fit: 2D frames through reduction give the injected size",
     ["tests/test_simulator.py", "tests/test_simulator_closed_loop.py"]),
    ("The page runs (Setup, Fitting, Report, Knowledge, Guide; jsdom)",
     ["tests/test_analyzer_runtime.py", "tests/test_analyzer_ui.py"]),
]


def _table(rows, title):
    print(f"\n{title}\n" + "-" * len(title))
    w = max(len(r[0]) for r in rows) if rows else 10
    for name, status, note in rows:
        print(f"  {status:4}  {name:<{w}}  {note}")


# ── 1. features, by their tests ──────────────────────────────────────────────

def run_features(only: str = "") -> list:
    rows = []
    env = dict(os.environ, SWAXS_SLACK_WEBHOOK_URL="")
    for name, groups in FEATURES:
        if only and only.lower() not in name.lower():
            continue
        failed, passed, skipped, missing = [], 0, 0, []
        for g in groups:
            path, _, k = g.partition(" -k ")
            if not (ROOT / path).is_file():
                missing.append(path)
                continue
            cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfs", path]
            if k:
                cmd += ["-k", k.strip("'")]
            p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
            out = p.stdout + p.stderr
            for line in out.splitlines():
                if line.startswith("FAILED "):
                    failed.append(line[7:].split(" - ")[0])
            import re
            m = re.search(r"(\d+) passed", out)
            passed += int(m.group(1)) if m else 0
            m = re.search(r"(\d+) skipped", out)
            skipped += int(m.group(1)) if m else 0
            if p.returncode not in (0, 5) and not failed:      # 5 = nothing collected
                failed.append(f"{path}: pytest exit {p.returncode}")
        if failed:
            rows.append((name, FAIL, f"{len(failed)} failing: " + "; ".join(failed[:3])))
        elif passed == 0:
            rows.append((name, SKIP, "skipped here (a dependency is missing: "
                         + ("jsdom" if "page" in name else "see pytest -rs") + ")"))
        else:
            rows.append((name, PASS, f"{passed} tests" + (f", {skipped} skipped" if skipped else "")
                         + (f"; missing {', '.join(missing)}" if missing else "")))
        print(f"  {rows[-1][1]:4}  {name}", flush=True)
    return rows


# ── 2. simulated closed loop ─────────────────────────────────────────────────

def _load_analyzer(project: Path, tag: str):
    os.environ.update(SWAXS_PROJECT=str(project), SWAXS_NO_WATCH="1", SWAXS_NO_BUS="1",
                      SWAXS_SLACK_WEBHOOK_URL="")
    os.environ.pop("SWAXS_RESUME", None)
    spec = importlib.util.spec_from_file_location(tag, ROOT / "analyzer" / "app.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[tag] = m
    spec.loader.exec_module(m)
    m._project_root = str(project)
    m._gate_mode = "off"
    return m


def _curve(R, pdi, seed):
    import numpy as np
    from src.simulator.pattern import iq_curve
    rng = np.random.default_rng(seed)
    q = np.geomspace(0.08, 4.0, 250)
    I = iq_curve(q, R, pdi, scale=1000.0, bkg=0.05)
    s = 0.02 * I + 0.01
    return q, np.abs(I + rng.normal(0, s)), s


def _write_profile(project: Path, rid: str, q, I, s) -> Path:
    sub = project / "1D" / "SAXS" / "Subtracted"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / f"{rid}_sample_SAXS_subtracted.dat"
    f.write_text("# q_nm-1 I sigma\n" + "\n".join(f"{a:.6g} {b:.6g} {c:.6g}" for a, b, c in zip(q, I, s)) + "\n")
    fb = project / "reactor" / "feedback"
    fb.mkdir(parents=True, exist_ok=True)
    (fb / f"{rid}.done.json").write_text(json.dumps({"recipe_id": rid}))
    return f


def run_campaign(method: str, fitter: str, budget: int, keep: bool, ai_level: str = "") -> tuple:
    from src.simulator.ground_truth import truth_from_recipe
    project = Path(tempfile.mkdtemp(prefix=f"anlz_{method}_"))
    (project / "1D" / "SAXS" / "Conditions").mkdir(parents=True)
    t0 = time.time()
    an = _load_analyzer(project, f"anlz_check_{method}_{ai_level or 'x'}_{int(t0 * 1000)}")
    cl = an.app.test_client()
    an._BATCH_SETTLE_S = 0.0                     # the default: learn when the final fit is ready
    goal = {"bax": "level-set", "pareto_ehvi": "pareto"}.get(method, "target-hit")
    r = cl.post("/api/methods", json={"fitter": fitter, "optimiser": method}).get_json()
    if r.get("error"):
        return (method, FAIL, f"could not select: {r['error']}"), None
    # a tight spec, so the cold start alone rarely hits it and the method must work
    n_init = min(3, budget)
    body = {"target_size": 4.0, "tolerance": 0.2, "pdi_cap": 0.1, "budget": budget,
            "n_init": n_init, "goal": goal, "confidence_min": 0.3}
    if goal == "pareto":
        body.update(size_range=[6.0, 10.0], target_size=4.0, tolerance=1.0)
    if ai_level:
        body["ai"] = {"level": ai_level, "review_every": 5, "apply": "auto", "model": "", "thinking": "standard"}
    r = cl.post("/api/campaign/start", json=body).get_json()
    if not r.get("ok"):
        return (method, FAIL, f"start refused: {r.get('error')}"), None
    errs, fit_err, n, stalls = [], [], 0, 0
    while an._campaign is not None and an._campaign.status_str == "running" and n < budget + 3:
        if hasattr(an, "_ai_wait"):
            an._ai_wait(120)
        if not an._pending:
            stalls += 1
            if stalls > 3:
                errs.append("no pending recipe: the loop stalled")
                break
            time.sleep(0.5)
            continue
        rid, params = next(iter(an._pending.items()))
        truth = truth_from_recipe(params, seed_key=rid)
        q, I, s = _curve(truth["R_nm"], truth["pdi"], seed=n)
        f = _write_profile(project, rid, q, I, s)
        an._analyze_file(f)
        an._ai_fit_wait(240)
        res = (an._results.get(f.name) or {}).get("summary") or {}
        if res.get("radius"):
            fit_err.append(abs(res["radius"] - truth["R_nm"]) / truth["R_nm"])
        if rid in an._batches:                    # pretend the 3 min have passed
            an._batches[rid]["last"] -= 1e4
        an._settle_tick()
        n += 1
    for _seq, ln in list(an._log):
        if ln.get("tag") == "error":
            errs.append(ln["msg"][:120])
    st = an._campaign_status() if an._campaign is not None else {}
    best = st.get("best") or {}
    secs = time.time() - t0
    import statistics
    med = statistics.median(fit_err) if fit_err else None
    hist = an._campaign.history if an._campaign is not None else []
    hit = next((i + 1 for i, h in enumerate(hist) if h.get("size") is not None
                and abs(h["size"] - 4.0) <= 0.2 and (h.get("pdi") or 1) <= 0.1), None)
    bs = "n/a" if best.get("size") is None else f"{best['size']:.2f}"
    reached = (f"reached the spec at run {hit}"
               + (" (in the cold start, so the method was not needed)" if hit <= n_init else "")
               if hit else f"spec not reached in {n} runs")
    note = (f"{reached}; best R {bs} nm (target 4.0 ± 0.2, PDI ≤ 0.1); "
            + (f"fit error median {med * 100:.1f} %" if med is not None else "no fit"))
    ok = (not errs) and n >= min(budget, 3) and med is not None and med < 0.08
    if goal == "target-hit" and best.get("size") is not None and abs(best["size"] - 4.0) > 0.6:
        ok = False
        note += "; best not within 0.6 nm of the target"
    extra = {"ai": st.get("ai"), "ai_fitter": st.get("ai_fitter")}
    if not keep:
        shutil.rmtree(project, ignore_errors=True)
    else:
        note += f"; project kept at {project}"
    status = PASS if ok else FAIL
    if ok and goal == "target-hit" and not hit:
        status = WARN          # the loop works, but this method did not reach the spec
    return ((f"{ai_level or method} ({fitter})", status,
             note + f", {secs:.0f} s" + (f"; ERRORS: {'; '.join(errs[:2])}" if errs else ""))), extra


# ── 3. real AI ───────────────────────────────────────────────────────────────

def run_ai(budget: int, keep: bool) -> list:
    rows = []
    from src.ai.models import login_summary
    from src.optimizer import ai_design as A
    s = login_summary()
    if s["login"] == "none" or not s["package"]:
        return [("AI login", FAIL, "no AI login or no anthropic package: the AI parts run as Catalogue + BIC "
                 "and the optimiser (python tools/ai_check.py explains)")]
    r = A.test_connection("", "off")
    rows.append(("AI login and model", PASS if r["ok"] else FAIL,
                 f"{r.get('model')} answered in {r.get('ms')} ms" if r["ok"] else str(r.get("error"))))
    if not r["ok"]:
        return rows
    # one AI-assisted fit of a core-shell-like curve
    from src.fitting.llm_structure import LLMStructure
    q, I, sg = _curve(4.0, 0.08, seed=7)
    t0 = time.time()
    res = LLMStructure().fit(q, I, sg, dist="auto")
    info = res.get("llm_structure") or {}
    good = info.get("status") == "ai"
    rows.append(("AI-assisted structure fit", PASS if good else FAIL,
                 f"status {info.get('status')}, proposed {[c.get('model') for c in info.get('proposed') or []]}, "
                 f"winner {res.get('model')}, R {round((res.get('size') or {}).get('radius') or 0, 2)} nm (true 4.0), "
                 f"{time.time() - t0:.0f} s"))
    row, extra = run_campaign("gp_ei", "catalogue_bic", min(budget, 8), keep, ai_level="ai_copilot")
    ai = (extra or {}).get("ai") or {}
    row = (row[0], row[1], row[2] + f"; AI picks {ai.get('n_ai_picks', ai.get('n_picks', '?'))}, "
           f"fallbacks {ai.get('n_fallbacks', '?')}")
    rows.append(row)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="also run simulated closed-loop campaigns")
    ap.add_argument("--ai", action="store_true", help="also make real AI calls")
    ap.add_argument("--no-features", action="store_true", help="skip the test groups")
    ap.add_argument("--methods", default="random,gp_ei,gp_ucb,turbo,bax,pareto_ehvi")
    ap.add_argument("--fitter", default="catalogue_bic")
    ap.add_argument("--budget", type=int, default=15)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", default="", help="only the features whose name contains this text")
    a = ap.parse_args(argv)
    all_rows = []
    if not a.no_features:
        print("1. Features, by their tests")
        rows = run_features(a.only)
        _table(rows, "Features")
        all_rows += rows
    if a.loop:
        print("\n2. Simulated closed loop (hidden optimum R = 4.0 nm)")
        rows = []
        for m in [x.strip() for x in a.methods.split(",") if x.strip()]:
            try:
                row, _ = run_campaign(m, a.fitter, a.budget, a.keep)
            except Exception as exc:                       # noqa: BLE001
                row = (m, FAIL, f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(f"  {row[1]:4}  {row[0]}", flush=True)
        _table(rows, "Closed loop, one campaign per method")
        all_rows += rows
    if a.ai:
        print("\n3. Real AI")
        try:
            rows = run_ai(a.budget, a.keep)
        except Exception as exc:                           # noqa: BLE001
            rows = [("real AI", FAIL, f"{type(exc).__name__}: {exc}")]
        _table(rows, "Real AI")
        all_rows += rows
    nf = sum(1 for r in all_rows if r[1] == FAIL)
    print(f"\n{len(all_rows)} checks: {sum(1 for r in all_rows if r[1] == PASS)} passed, {nf} failed, "
          f"{sum(1 for r in all_rows if r[1] == WARN)} warnings, {sum(1 for r in all_rows if r[1] == SKIP)} skipped")
    if any(r[1] == WARN for r in all_rows):
        print("WARN = the loop worked (fits right, no errors, no stall) but that method did not reach the "
              "spec within the budget. One campaign is one sample: compare methods over many seeds in "
              "the Analyser's Compare methods goal before drawing a conclusion.")
    return 1 if nf else 0


if __name__ == "__main__":
    sys.exit(main())
