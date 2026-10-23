"""src/analysis/fit_context.py: what the pipeline already knows about one curve.

The fitter receives q, I and σ; the subtracted .dat carries no metadata footer.
Everything else the earlier steps learned stays in the project. This collects it
for one subtracted SAXS file, so the structure fitter (and its AI proposer) can
reason with it:

    recipe        reactor recipe for the recipe_id   reactor/feedback/<rid>.done.json
    chemistry     reagents, solvent, expected product <project>/chemistry.yml (operator)
    reduction     normalisation mode, absolute units  <project>/config.yml
    background    scale, QC metrics and verdict       manifest "background"
    quality       score, verdict, flags               manifest "quality"

Quality grades the flat Subtracted/ file; the Analyser may read the copy in
Good/ or NeedsReview/, so the lookup maps those back to the flat path.
Read-only, never raises: a missing piece is simply absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

_GATE_DIRS = ("Good", "NeedsReview")

CHEMISTRY_TEMPLATE = """\
# chemistry.yml: what this experiment makes, in a few lines. Read by the
# LLM-assisted structure fitter as context (src/analysis/fit_context.py).
# Leave a field out if unknown.
product: ""            # e.g. "PbS quantum dots", "SiO2 spheres", "Au nanorods"
expected_shape: ""     # e.g. "spheres", "core-shell spheres", "rods", "unknown"
reagents: []           # e.g. ["PbCl2 in oleylamine", "S in TOP"]
solvent: ""            # e.g. "1-octadecene"
ligand: ""             # e.g. "oleylamine"
notes: ""              # anything an expert would want to know
"""


def _flat_path(p: Path) -> Path:
    return p.parent.parent / p.name if p.parent.name in _GATE_DIRS else p


def _read_yaml(p: Path) -> dict:
    try:
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _recipe_id(path: Path) -> str | None:
    try:
        from src.optimizer.io import recipe_id_from_filename
        return recipe_id_from_filename(path.name) or None
    except Exception:                                    # noqa: BLE001
        return None


def collect(sub_path, project_root=None, manifest: dict | None = None) -> dict:
    """Context for one subtracted file. ``manifest`` may be passed to avoid a reload."""
    p = Path(sub_path).resolve()
    flat = _flat_path(p)
    root = Path(project_root).resolve() if project_root else None
    if root is None:                      # <project>/1D/SAXS/Subtracted[/Good]/x.dat
        for anc in p.parents:
            if anc.name == "1D":
                root = anc.parent
                break
    ctx: dict = {"file": p.name}
    if p != flat:
        ctx["quality_folder"] = p.parent.name

    rid = _recipe_id(p)
    if rid:
        ctx["recipe_id"] = rid
    if root is None:
        return ctx

    if rid:
        try:
            done = json.loads((root / "reactor" / "feedback" / f"{rid}.done.json")
                              .read_text(encoding="utf-8"))
            ctx["recipe"] = done.get("recipe") or {}
            if done.get("measured_flows"):
                ctx["measured_flows"] = done["measured_flows"]
        except Exception:                                # noqa: BLE001
            pass

    chem = _read_yaml(root / "chemistry.yml")
    chem = {k: v for k, v in chem.items() if v not in ("", None, [], {})}
    if chem:
        ctx["chemistry"] = chem

    cfg = _read_yaml(root / "config.yml")
    if cfg:
        norm = cfg.get("normalization", ["bstop"])
        norm = norm if isinstance(norm, list) else [norm]
        K = cfg.get("absolute_calibration_factor")
        ctx["reduction"] = {
            "normalization": norm,
            "absolute_units": "absolute" in norm and bool(K) and float(K or 0) > 0,
            "q_unit": cfg.get("unit", "q_nm^-1"),
            "compound": cfg.get("compound"),
            "energy_keV": cfg.get("energy_keV"),
        }

    if manifest is None:
        try:
            from src.manifest import load_manifest
            manifest = load_manifest(root)
        except Exception:                                # noqa: BLE001
            manifest = {}
    key = str(flat)
    bg = (manifest.get("background") or {}).get(key) or {}
    if bg:
        qc = bg.get("qc") or {}
        ctx["background"] = {k: bg.get(k) for k in
                             ("scale", "scale_method", "qc_verdict", "clamped") if k in bg}
        ctx["background"].update({k: qc.get(k) for k in
                                  ("pct_negative", "highq_ratio", "lowq_slope", "warnings")
                                  if k in qc})
    ql = (manifest.get("quality") or {}).get(key) or {}
    if ql:
        ctx["quality"] = {k: ql.get(k) for k in ("score", "verdict", "flags") if k in ql}
    return ctx


def write_chemistry_template(project_root) -> Path:
    """Create <project>/chemistry.yml from the template if it does not exist."""
    p = Path(project_root) / "chemistry.yml"
    if not p.exists():
        p.write_text(CHEMISTRY_TEMPLATE, encoding="utf-8")
    return p
