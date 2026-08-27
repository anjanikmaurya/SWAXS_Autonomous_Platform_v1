#!/usr/bin/env python
"""
tools/campaign_plots.py — run a closed-loop campaign in silico and plot it.

This drives the REAL CampaignController against the REAL simulator ground truth
(src/simulator/ground_truth.py), so the figures show how this platform's
optimizer behaves — not a textbook illustration of Bayesian optimization.

The only thing faked is the beam: instead of synthesising and measuring, each
proposed recipe is passed to ``truth_from_recipe()``, which returns the radius
and PDI that recipe "really" produces, with reproducible run-to-run scatter.
Fit confidence is modelled from PDI, because a broad size distribution genuinely
does make the form-factor fit less certain.

Usage
-----
    uv run tools/campaign_plots.py --out docs/figures --budget 40 --seed 3
    uv run tools/campaign_plots.py --snapshots 8,16,40      # slice at each stage
    uv run tools/campaign_plots.py --replicates 12          # convergence stats

Outputs PNGs plus a campaign.json so the numbers quoted in the report can be
re-derived without re-running.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.optimizer.campaign import CampaignController          # noqa: E402
from src.optimizer.space import ParameterSpace                 # noqa: E402
from src.optimizer import diagnostics as dg, plots             # noqa: E402
from src.simulator.ground_truth import DEFAULTS, truth_from_recipe  # noqa: E402


def measure(params: dict, run: int, cfg: dict | None = None) -> tuple[float, float, float]:
    """Stand in for synthesise → reduce → subtract → fit.

    Returns (radius_nm, pdi, confidence). Confidence falls as PDI rises: a
    polydisperse sample smears the form-factor oscillations, which is exactly
    when src/analysis reports a lower confidence. That coupling matters — it is
    what makes the campaign's confidence-as-noise weighting do any work.
    """
    t = truth_from_recipe(params, cfg, seed_key=f"insilico-{run}")
    pdi = t["pdi"]
    conf = float(np.clip(0.97 - 1.5 * pdi, 0.15, 0.97))
    return t["R_nm"], pdi, conf


def run_campaign(*, budget=40, n_init=10, target=None, tolerance=0.30,
                 pdi_cap=0.15, seed=0, cfg=None, snapshots=()):
    """Closed loop to completion. Returns (campaign, {run: slice_png})."""
    truth = dict(DEFAULTS); truth.update(cfg or {})
    target = float(truth["R_opt"]) if target is None else float(target)

    space = ParameterSpace.from_config({})            # platform default bounds
    c = CampaignController(space, target_size=target, tolerance=tolerance,
                           pdi_cap=pdi_cap, budget=budget, n_init=n_init,
                           seed=seed).start()

    shots: dict[int, bytes] = {}
    run = 0
    while c.status_str == "running":
        p = c.ask()
        if p is None:
            break
        run += 1
        size, pdi, conf = measure(p, run, cfg)
        c.tell(p, size, pdi, conf)
        if run in snapshots:
            shots[run] = plots.slice_figure(c, truth=truth)
    return c, shots, truth


def replicate_convergence(n=12, **kw) -> dict:
    """Repeat the campaign with different seeds.

    A single BO trace says almost nothing — the literature on benchmarking BO in
    materials science is emphatic that per-seed variance is large and that
    conclusions must be drawn from repeats. This gives the median and the
    inter-quartile band of best-so-far loss.
    """
    curves, ends = [], []
    for s in range(n):
        c, _, truth = run_campaign(seed=s, **kw)
        d = dg.convergence(c, uncertainty=False)
        curves.append(d["best_loss"])
        ends.append({"seed": s, "status": c.status_str, "n": len(c.history),
                     "best_loss": (c.best or {}).get("loss"),
                     "best_size": (c.best or {}).get("size"),
                     "best_pdi": (c.best or {}).get("pdi"),
                     "best_params": (c.best or {}).get("params")})
    L = max(len(x) for x in curves)
    M = np.full((len(curves), L), np.nan)
    for i, x in enumerate(curves):
        M[i, :len(x)] = x
        M[i, len(x):] = x[-1] if x else np.nan     # hold after an early stop
    return {"runs": list(range(1, L + 1)),
            "median": np.nanmedian(M, axis=0).tolist(),
            "q25": np.nanpercentile(M, 25, axis=0).tolist(),
            "q75": np.nanpercentile(M, 75, axis=0).tolist(),
            "per_seed": ends,
            "n_converged": sum(1 for e in ends if e["status"] == "converged"),
            "n_seeds": len(ends)}


def replicate_figure(rep: dict, out: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    r = rep["runs"]
    fig, ax = plt.subplots(figsize=(7.6, 4.2), dpi=130, facecolor="white")
    ax.fill_between(r, rep["q25"], rep["q75"], color="#2a78d6", alpha=0.20,
                    label="inter-quartile range")
    ax.semilogy(r, rep["median"], lw=2.2, color="#B1040E", label="median best-so-far")
    ax.set_xlabel("run"); ax.set_ylabel("best loss so far (log)")
    ax.grid(alpha=0.25); ax.legend(fontsize=9)
    ax.set_title(f"Convergence over {rep['n_seeds']} independent campaigns "
                 f"({rep['n_converged']} reached the acceptance band)", fontsize=11)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight"); plt.close(fig)


def ablation(n_seeds=24, budget=40, n_init=12, **kw) -> dict:
    """Does the surrogate actually earn its keep?

    The honest control for a Bayesian-optimization claim is the same space-filling
    design run to the same budget with no surrogate at all. Setting ``n_init =
    budget`` makes the controller pure Sobol while changing nothing else — same
    bounds, same constraint, same loss, same simulator, same seeds. Two numbers
    come out: runs-to-acceptance (how much beam time the loop saves) and the best
    loss at a fixed budget (how much better the answer is when time is capped).
    """
    def one(ninit, seed, bud, honour_stop):
        space = ParameterSpace.from_config({})
        c = CampaignController(space, target_size=kw.get("target", 4.0),
                               tolerance=kw.get("tolerance", 0.10),
                               pdi_cap=kw.get("pdi_cap", 0.05),
                               budget=bud, n_init=ninit, seed=seed).start()
        k, curve = 0, []
        while True:
            if not honour_stop and c.status_str == "converged":
                c.status_str = "running"           # keep spending the full budget
            if c.status_str != "running":
                break
            p = c.ask()
            if p is None:
                break
            k += 1
            c.tell(p, *measure(p, k))
            curve.append(min(h["loss"] for h in c.history))
        return c, curve

    out = {}
    for label, ninit in (("bo", n_init), ("sobol", budget)):
        to_accept, accepted, curves = [], 0, []
        for s in range(n_seeds):
            c, _ = one(ninit, s, budget, True)
            to_accept.append(len(c.history))
            accepted += c.status_str == "converged"
            _, cur = one(ninit, s, budget, False)
            cur = (cur + [cur[-1]] * budget)[:budget] if cur else [np.nan] * budget
            curves.append(cur)
        M = np.array(curves, float)
        out[label] = {"runs_to_accept": to_accept, "n_accepted": accepted,
                      "n_seeds": n_seeds, "budget": budget,
                      "median": np.nanmedian(M, 0).tolist(),
                      "q25": np.nanpercentile(M, 25, 0).tolist(),
                      "q75": np.nanpercentile(M, 75, 0).tolist(),
                      "final_median": float(np.nanmedian(M[:, -1]))}
    b, s = out["bo"], out["sobol"]
    out["speedup_runs"] = float(np.median(s["runs_to_accept"]) /
                                max(np.median(b["runs_to_accept"]), 1e-9))
    out["quality_ratio"] = float(s["final_median"] / max(b["final_median"], 1e-9))
    return out


def ablation_figure(ab: dict, out: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    r = np.arange(1, ab["bo"]["budget"] + 1)
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.2),
                             gridspec_kw={"width_ratios": [1.5, 1]},
                             dpi=130, facecolor="white")
    ax = axes[0]
    for key, col, lab in (("sobol", "#888780", "Sobol only (no surrogate)"),
                          ("bo", "#B1040E", "GP + expected improvement")):
        d = ab[key]
        ax.fill_between(r, d["q25"], d["q75"], color=col, alpha=0.18)
        ax.semilogy(r, d["median"], lw=2.2, color=col, label=lab)
    ax.set_xlabel("run"); ax.set_ylabel("best loss so far (log)")
    ax.grid(alpha=0.25); ax.legend(fontsize=9)
    ax.set_title(f"(a) median of {ab['bo']['n_seeds']} campaigns, IQR shaded",
                 fontsize=10.5)

    ax = axes[1]
    data = [ab["bo"]["runs_to_accept"], ab["sobol"]["runs_to_accept"]]
    bp = ax.boxplot(data, tick_labels=["GP + EI", "Sobol"], patch_artist=True,
                    widths=0.55)
    for patch, col in zip(bp["boxes"], ["#B1040E", "#888780"]):
        patch.set_facecolor(col); patch.set_alpha(0.35)
    for med in bp["medians"]:
        med.set_color("black")
    ax.set_ylabel("runs until a recipe is accepted")
    ax.grid(axis="y", alpha=0.25)
    ax.set_title(f"(b) beam time to an answer "
                 f"({ab['bo']['n_accepted']}/{ab['bo']['n_seeds']} vs "
                 f"{ab['sobol']['n_accepted']}/{ab['sobol']['n_seeds']} accepted)",
                 fontsize=10.5)
    fig.suptitle(f"Is the surrogate earning its keep?  "
                 f"{ab['speedup_runs']:.1f}× fewer runs to acceptance, "
                 f"{ab['quality_ratio']:.1f}× lower loss at a fixed budget",
                 fontsize=12, y=1.0)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out, bbox_inches="tight"); plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/figures")
    ap.add_argument("--budget", type=int, default=40)
    ap.add_argument("--n-init", type=int, default=10)
    ap.add_argument("--tolerance", type=float, default=0.30)
    ap.add_argument("--pdi-cap", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--snapshots", default="",
                    help="comma-separated run numbers to snapshot the slice at")
    ap.add_argument("--replicates", type=int, default=12)
    ap.add_argument("--ablation", type=int, default=24,
                    help="seeds for the GP-vs-Sobol control (0 to skip)")
    a = ap.parse_args()

    out = (ROOT / a.out) if not Path(a.out).is_absolute() else Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    shots = tuple(int(s) for s in a.snapshots.split(",") if s.strip())

    print(f"running a {a.budget}-run campaign (seed {a.seed}) …")
    c, snaps, truth = run_campaign(budget=a.budget, n_init=a.n_init,
                                   tolerance=a.tolerance, pdi_cap=a.pdi_cap,
                                   seed=a.seed, snapshots=shots)
    print(f"  status={c.status_str} after {len(c.history)} runs; "
          f"best loss={c.best['loss']:.4f} at R={c.best['size']:.3f} nm, "
          f"PDI={c.best['pdi']:.3f}")

    files = {}
    for view in ("slice", "convergence", "trajectory"):
        p = out / f"campaign_{view}.png"
        p.write_bytes(plots.figure(view, c, truth=truth))
        files[view] = p.name
        print(f"  wrote {p.relative_to(ROOT)}")
    for run, png in snaps.items():
        p = out / f"campaign_slice_run{run:02d}.png"
        p.write_bytes(png); files[f"slice_run{run}"] = p.name
        print(f"  wrote {p.relative_to(ROOT)}")

    if a.replicates > 1:
        print(f"running {a.replicates} replicate campaigns …")
        rep = replicate_convergence(a.replicates, budget=a.budget, n_init=a.n_init,
                                    tolerance=a.tolerance, pdi_cap=a.pdi_cap)
        p = out / "campaign_replicates.png"
        replicate_figure(rep, p); files["replicates"] = p.name
        print(f"  {rep['n_converged']}/{rep['n_seeds']} campaigns converged")
        print(f"  wrote {p.relative_to(ROOT)}")
    else:
        rep = None

    ab = None
    if a.ablation > 1:
        print(f"running the GP-vs-Sobol control over {a.ablation} seeds …")
        ab = ablation(a.ablation, budget=40, n_init=a.n_init,
                      tolerance=a.tolerance, pdi_cap=a.pdi_cap)
        p = out / "campaign_ablation.png"
        ablation_figure(ab, p); files["ablation"] = p.name
        print(f"  GP+EI reaches acceptance {ab['speedup_runs']:.2f}x faster; "
              f"{ab['quality_ratio']:.2f}x lower loss at a fixed budget")
        print(f"  wrote {p.relative_to(ROOT)}")

    meta = {"truth": truth, "status": c.status_str, "n_runs": len(c.history),
            "best": c.best, "summary": dg.summary(c),
            "settings": {"budget": a.budget, "n_init": a.n_init, "seed": a.seed,
                         "tolerance": a.tolerance, "pdi_cap": a.pdi_cap,
                         "target_size": c.target_size},
            "history": c.history, "replicates": rep,
            "ablation": ab, "files": files}
    (out / "campaign.json").write_text(json.dumps(meta, indent=1, default=float))
    print(f"  wrote {(out / 'campaign.json').relative_to(ROOT)}")


if __name__ == "__main__":
    main()
