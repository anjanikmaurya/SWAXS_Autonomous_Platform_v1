"""
src/analysis/io.py — Analysis output plumbing
=============================================
Shared saving layer for the analysis app. Writes every analysis result to a new
``Analysed/`` folder (sibling of ``Averaged/`` and ``Subtracted/``), organised by
detector then analysis type:

    1D/SAXS/Analysed/Guinier/<sample>_guinier.json   (params + results + provenance)
                                       .dat            (fit curve, if any)
                                       .png            (figure, if provided)

It also:
  • appends the fit parameters back into the source ``.dat`` footer (the file
    that was fitted),
  • registers the analysis in ``manifest.json`` (cross-app + AI visibility),
  • writes a combined batch **summary table** (CSV, plus XLSX when openpyxl is
    available).

All logic lives here (per CLAUDE.md); the Flask app stays a thin shell.
"""
from __future__ import annotations

import base64
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Folder name per analysis type.
_TYPE_DIR = {
    "guinier":       "Guinier",
    "kratky":        "Kratky",
    "porod":         "Porod",
    "pair_distance": "PairDistance",
    "pr":            "PairDistance",
    "invariant":     "Invariant",
    "model":         "Model",
    "sasview":       "Model",
    "peaks":         "Peaks",
    "peak":          "Peaks",
    "atsas":         "ATSAS",
}

_ANNOTATE_MARKER = "# ANALYSIS INFORMATION"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def analysed_dir_for_source(source_path: str | Path, analysis_type: str) -> Path:
    """
    Return (and create) the Analysed/<Type>/ directory for a source curve.
    If the source sits in ``…/<DET>/Subtracted/`` the Analysed folder is a
    sibling of Subtracted; otherwise it is created next to the source file.
    """
    src = Path(source_path)
    type_dir = _TYPE_DIR.get(analysis_type.lower(), analysis_type.capitalize())
    parent = src.parent
    base = parent.parent if parent.name.lower() in ("subtracted", "averaged",
                                                    "reduction", "reduced") else parent
    out = base / "Analysed" / type_dir
    out.mkdir(parents=True, exist_ok=True)
    return out


def _scalar_results(results: dict) -> dict:
    """Keep only finite scalar (number) result fields — drop arrays/curves."""
    out = {}
    for k, v in (results or {}).items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str) and k not in ("plot",):
            out[k] = v
    return out


def annotate_source_dat(source_path: str | Path, analysis_type: str,
                        results: dict) -> bool:
    """
    Append the (scalar) fit parameters into the source ``.dat`` footer under an
    ``# ANALYSIS INFORMATION`` block, e.g. ``# guinier.Rg: 3.21``. A previous
    block for the SAME analysis_type is replaced. Returns True on success.
    """
    p = Path(source_path)
    if not p.is_file():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False

    prefix = f"# {analysis_type.lower()}."
    # drop any existing lines for this analysis type (idempotent re-runs)
    kept = [ln for ln in lines if not ln.lower().startswith(prefix)]
    # ensure the marker exists
    if _ANNOTATE_MARKER not in kept:
        kept.append("")
        kept.append(_ANNOTATE_MARKER)
    block = [f"# {analysis_type.lower()}.{k}: {v}"
             for k, v in _scalar_results(results).items()]
    # insert the block right after the marker
    idx = kept.index(_ANNOTATE_MARKER) + 1
    kept[idx:idx] = block
    try:
        p.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _write_fit_dat(path: Path, q, I, header: str) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(f"# {header}\n# q_nm-1  I\n")
        for qi, Ii in zip(q, I):
            fh.write(f"{qi:.6e}  {Ii:.6e}\n")


def save_analysis(
    project_root:  str | Path | None,
    source_path:   str | Path,
    detector:      str,
    analysis_type: str,
    params:        dict,
    results:       dict,
    *,
    fit_curve:     tuple | None = None,      # (q_array, I_array)
    plot_png_b64:  str | None = None,
    user:          str = "",
    annotate:      bool = True,
    register:      bool = True,
) -> dict:
    """
    Persist one analysis: JSON (+ optional fit .dat and PNG) into Analysed/,
    annotate the source .dat, and register it in the manifest.

    Returns {"json": path, "dat": path|None, "png": path|None,
             "analysis_id": str|None, "annotated": bool}.
    """
    src = Path(source_path)
    out_dir = analysed_dir_for_source(src, analysis_type)
    stem = f"{src.stem}_{analysis_type.lower()}"

    record = {
        "analysis_type": analysis_type,
        "detector":      (detector or "").lower(),
        "source":        str(src),
        "params":        params,
        "results":       _scalar_results(results),
        "created_at":    _now(),
        "user":          user,
    }
    json_path = out_dir / f"{stem}.json"
    json_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    dat_path = None
    if fit_curve is not None and fit_curve[0] is not None:
        dat_path = out_dir / f"{stem}_fit.dat"
        _write_fit_dat(dat_path, fit_curve[0], fit_curve[1],
                       f"{analysis_type} fit curve for {src.name}")

    png_path = None
    if plot_png_b64:
        png_path = out_dir / f"{stem}.png"
        try:
            png_path.write_bytes(base64.b64decode(plot_png_b64))
        except Exception:
            png_path = None

    annotated = annotate_source_dat(src, analysis_type, results) if annotate else False

    analysis_id = None
    if register and project_root:
        try:
            from src.manifest import update_manifest, add_analysis_entry, make_provenance
            prov = make_provenance("analysis", user=user, input_files=[str(src)],
                                   config={"analysis_type": analysis_type})
            analysis_id = update_manifest(
                project_root,
                lambda m: add_analysis_entry(
                    m, analysis_type=analysis_type, file_path=str(src),
                    params=params, results=_scalar_results(results),
                    fit_range=results.get("q_range"),
                    quality_score=results.get("quality_score"),
                    provenance=prov,
                ),
            )
        except Exception:
            analysis_id = None

    return {"json": str(json_path), "dat": str(dat_path) if dat_path else None,
            "png": str(png_path) if png_path else None,
            "analysis_id": analysis_id, "annotated": annotated}


def write_batch_summary(
    out_dir:       str | Path,
    analysis_type: str,
    rows:          list[dict],
) -> dict:
    """
    Write a combined batch results table to ``out_dir`` as CSV (and XLSX when
    openpyxl is available). Returns {"csv": path, "xlsx": path|None}.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # union of keys preserves first-seen order
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)

    csv_path = out / f"{analysis_type}_batch_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})

    xlsx_path = None
    try:
        from openpyxl import Workbook
        wb = Workbook(); ws = wb.active; ws.title = analysis_type[:31] or "batch"
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c, "") for c in cols])
        xlsx_path = out / f"{analysis_type}_batch_{stamp}.xlsx"
        wb.save(xlsx_path)
    except Exception:
        xlsx_path = None

    return {"csv": str(csv_path), "xlsx": str(xlsx_path) if xlsx_path else None}
