"""
src/plot_reduction.py — Data loading and averaging utilities
=============================================================
Shared by the viewer app (port 5002) and any future apps that need
to load or average 1-D SAXS/WAXS .dat files.

Public API (see __all__):
  read_folder      — load all matching .dat files from a folder into memory
  average_and_save — average scans per keyword and write output .dat files
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

from .utils.read_dat_metadata import read_dat_data_metadata

__all__ = [
    "read_folder",       # Load .dat files from a folder → list of file dicts
    "average_and_save",  # Average scans per keyword and save output .dat files
    "average_batch",     # Average an explicit list of frame dicts → one .dat
]

logger = logging.getLogger("swaxs_platform")

# Regex that captures the scan index (any digit count) and optional detector suffix.
# e.g. sample_001.dat       → group(1)=001    (3-digit)
#      sample_0001.dat      → group(1)=0001   (4-digit)
#      sample_0001_SAXS.dat → group(1)=0001   (with detector suffix)
_IDX_RE = re.compile(r"_(\d+)(?:_[A-Za-z]+)?\.dat$")

# Strip label suffixes added by average_and_save so that loading from the
# Averaged/ folder returns the original keyword.
# e.g. BSA_10mg_Average.dat        → keyword "BSA_10mg"
# e.g. BSA_10mg_30files_Average.dat → keyword "BSA_10mg"
_LABEL_RE = re.compile(r"(?:_\d+files)?_(?:Average|Avg)$", re.IGNORECASE)


# ── Private helpers ────────────────────────────────────────────────────────────

def _common_q_grid(files: list[dict], n_pts: int = 1000) -> np.ndarray:
    """
    Build a common log-spaced q grid spanning the overlap of all supplied files.
    Raises ValueError if there is no overlapping q range.
    """
    lo = max(f["q"].min() for f in files)
    hi = min(f["q"].max() for f in files)
    if lo >= hi:
        raise ValueError(
            f"No overlapping q range across files "
            f"(max q_min={lo:.4f}, min q_max={hi:.4f})."
        )
    return np.geomspace(lo, hi, n_pts)


def _write_averaged_dat(
    path: Path,
    q: np.ndarray,
    I: np.ndarray,
    sigma: np.ndarray,
    keyword: str,
    metadata: dict,
    n_files: int = 1,
) -> None:
    """Write an averaged .dat file with a METADATA INFORMATION footer."""
    lines = [
        f"# Averaged SAXS/WAXS data — keyword: {keyword}",
        f"# Files averaged: {n_files}",
        "# Columns: q_nm-1  I  sigma",
    ]
    for qi, Ii, si in zip(q, I, sigma):
        lines.append(f"{qi:.8e}  {Ii:.8e}  {si:.8e}")

    if metadata:
        lines.append("# METADATA INFORMATION")
        for k, v in metadata.items():
            lines.append(f"# {k}: {v}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _average_group(
    kw_files: list[dict],
    i0_filter_pct: float = 0.0,
    n_pts: int = 1000,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict, int] | None:
    """
    Average one group of frame dicts (same keyword) onto a common q grid.

    This is the shared averaging core used by both ``average_and_save`` (which
    groups a whole folder by keyword) and ``average_batch`` (which averages an
    explicit list of frames).

    Frames whose I0 deviates from the group median by more than
    *i0_filter_pct* (when > 0) are discarded first.  Only the scans that
    actually contribute are counted, so skipped/invalid scans never inflate the
    denominator (which would bias I low and σ small).

    Returns ``(q, I, sigma, metadata, n_used)`` or ``None`` if no usable scans
    remain.
    """
    # ── I0-based outlier rejection ────────────────────────────────────────────
    if i0_filter_pct > 0:
        i0_vals: dict[str, float] = {}
        for fd in kw_files:
            raw = fd["metadata"].get("i0")
            if raw is not None:
                try:
                    v = float(raw)
                    if np.isfinite(v):
                        i0_vals[fd["filename"]] = v
                except (TypeError, ValueError):
                    pass

        if i0_vals:
            median_i0 = float(np.median(list(i0_vals.values())))
            if abs(median_i0) > 1e-10:
                kw_files = [
                    fd for fd in kw_files
                    if fd["filename"] not in i0_vals
                    or abs(i0_vals[fd["filename"]] - median_i0)
                       / abs(median_i0) * 100 <= i0_filter_pct
                ]

    if not kw_files:
        return None

    if len(kw_files) == 1:
        # Single file: use its data directly
        fd = kw_files[0]
        return fd["q"], fd["I"], fd["sigma"], fd["metadata"], 1

    # Build common grid and interpolate all files onto it
    try:
        q_grid = _common_q_grid(kw_files, n_pts=n_pts)
    except ValueError as exc:
        logger.warning("[_average_group] %s", exc)
        return None

    I_rows:   list[np.ndarray] = []
    sig_rows: list[np.ndarray] = []
    used_files: list[dict] = []

    for fd in kw_files:
        valid = (fd["q"] > 0) & (fd["I"] > 0)
        if valid.sum() < 3:
            logger.warning(
                "[_average_group] skipping %s (<3 valid points)",
                fd.get("filename", "?"))
            continue
        # Log-space interpolation (more accurate for scattering data)
        I_rows.append(np.exp(np.interp(
            np.log(q_grid),
            np.log(fd["q"][valid]),
            np.log(np.maximum(fd["I"][valid],    1e-30)),
        )))
        sig_rows.append(np.exp(np.interp(
            np.log(q_grid),
            np.log(fd["q"][valid]),
            np.log(np.maximum(fd["sigma"][valid], 1e-30)),
        )))
        used_files.append(fd)

    if not I_rows:
        return None

    # Average ONLY the scans that contributed (skipped scans must not inflate
    # the denominator, which would bias I low and σ small).
    I_stack   = np.vstack(I_rows)
    sig_stack = np.vstack(sig_rows)
    n         = I_stack.shape[0]
    q_out     = q_grid
    I_out     = I_stack.mean(axis=0)
    sig_out   = np.sqrt((sig_stack**2).sum(axis=0)) / n

    # Carry median metadata across the contributing files
    meta_keys = set().union(*(fd["metadata"].keys() for fd in used_files))
    meta_out: dict = {}
    for k in meta_keys:
        vals = [fd["metadata"][k] for fd in used_files if k in fd["metadata"]]
        if not vals:
            continue
        # Median only over numeric values; non-numeric metadata (strings)
        # carries the first seen value instead of crashing the average.
        nums = []
        for v in vals:
            try:
                fv = float(v)
                if np.isfinite(fv):
                    nums.append(fv)
            except (TypeError, ValueError):
                pass
        meta_out[k] = float(np.median(nums)) if nums else vals[0]

    return q_out, I_out, sig_out, meta_out, n


def _truncate_q(q: np.ndarray, I: np.ndarray, sigma: np.ndarray,
                q_min: float | None = None, q_max: float | None = None):
    """
    Restrict (q, I, σ) to the closed interval [q_min, q_max] (either bound may be
    None).  If the interval would keep fewer than 2 points it is ignored and the
    full arrays are returned (with a warning), so truncation can never produce an
    empty/degenerate file.
    """
    if q_min is None and q_max is None:
        return q, I, sigma
    mask = np.ones(q.shape, dtype=bool)
    if q_min is not None:
        mask &= (q >= float(q_min))
    if q_max is not None:
        mask &= (q <= float(q_max))
    if int(mask.sum()) < 2:
        logger.warning("[truncate_q] q-range [%s, %s] keeps <2 points — ignoring",
                       q_min, q_max)
        return q, I, sigma
    return q[mask], I[mask], sigma[mask]


# ── Public API ─────────────────────────────────────────────────────────────────

def read_folder(
    folder: str | Path,
    keywords: list[str] | None = None,
) -> list[dict]:
    """
    Load all matching .dat files from *folder* into a list of file dicts.

    Parameters
    ----------
    folder : str | Path
        Directory containing 1-D .dat scattering files.
    keywords : list[str] | None
        If given, only files whose name contains at least one keyword are
        returned.  Pass None to load all .dat files.

    Returns
    -------
    list[dict]
        Each element is::

            {
                "filename": str,         # e.g. "sample_0003.dat"
                "keyword":  str,         # stem with trailing _NNNN stripped
                "scan_idx": int,         # numeric index from filename
                "q":        np.ndarray,
                "I":        np.ndarray,
                "sigma":    np.ndarray,
                "metadata": dict,        # float-valued metadata from footer
            }
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")

    dat_files = sorted(folder.glob("*.dat"))
    if not dat_files:
        logger.warning("[read_folder] No .dat files found in %s", folder)
        return []

    # Filter by keywords if supplied
    if keywords:
        dat_files = [f for f in dat_files
                     if any(kw in f.name for kw in keywords)]

    results = []
    for path in dat_files:
        try:
            _, q, I, sigma, meta = read_dat_data_metadata(path)

            # Extract scan index from filename
            m = _IDX_RE.search(path.name)
            scan_idx = int(m.group(1)) if m else 0

            # Derive keyword: strip trailing _N+ (and optional _DET suffix).
            # For averaged files (no numeric index), path.stem is used, then
            # _LABEL_RE strips any _Average / _Avg suffix.
            keyword = path.name[: path.name.index(m.group(0))] if m else path.stem
            keyword = _LABEL_RE.sub("", keyword)

            results.append({
                "filename": path.name,
                "keyword":  keyword,
                "scan_idx": scan_idx,
                "q":        q,
                "I":        I,
                "sigma":    sigma,
                "metadata": meta,
            })
            logger.debug("[read_folder] %s: loaded %d points", path.name, len(q))
        except Exception as exc:
            logger.warning("[read_folder] WARNING — could not read %s: %s",
                           path.name, exc)
            continue

    return results


def average_and_save(
    folder: str | Path,
    keywords: list[str],
    *,
    n_pts: int = 1000,
    label_suffix: str = "Average",
    output_dir: str | Path | None = None,
    i0_filter_pct: float = 0.0,
    q_min: float | None = None,
    q_max: float | None = None,
) -> list[tuple[str, Path]]:
    """
    Average 1-D scans per keyword and save the result as a .dat file.

    Parameters
    ----------
    folder : str | Path
        Folder of .dat files (e.g. 1D/SAXS/Reduction/).
    keywords : list[str]
        Sample keywords to average.  Only files whose name contains a keyword
        are included.
    n_pts : int
        Number of q points in the common log-spaced output grid (default 1000).
    label_suffix : str
        Appended to the keyword when naming the output file (default "Average").
    output_dir : str | Path | None
        Where to save output files.  Defaults to a sibling ``Averaged/``
        subfolder next to *folder*.
    i0_filter_pct : float
        If > 0, discard frames whose I0 deviates from the median by more than
        this percentage.  0 = no filtering.

    Returns
    -------
    list[tuple[str, Path]]
        One ``(keyword, output_path)`` pair per keyword successfully saved.
    """
    folder = Path(folder)
    if output_dir is None:
        output_dir = folder.parent / "Averaged"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_files = read_folder(folder, keywords=keywords if keywords else None)
    if not all_files:
        logger.warning("[average_and_save] No files found in %s for keywords %s",
                       folder, keywords)
        return []

    # Group files by user-supplied keyword (longest match wins).
    # This ensures all files matching a partial keyword end up in one group,
    # regardless of how long the actual filename stem is.
    # e.g. keyword "Run1_18CE" matches 30 files that each have a long full stem —
    # they all land in the same "Run1_18CE" group instead of 30 per-file groups.
    groups: dict[str, list[dict]] = {}
    for fd in all_files:
        if keywords:
            # Find the longest user keyword that appears in the filename
            best_kw, best_len = fd["keyword"], 0
            for rk in keywords:
                if rk in fd["filename"] and len(rk) > best_len:
                    best_kw, best_len = rk, len(rk)
            group_key = best_kw
        else:
            group_key = fd["keyword"]
        groups.setdefault(group_key, []).append(fd)

    saved: list[tuple[str, Path]] = []

    for kw, kw_files in groups.items():
        result = _average_group(kw_files, i0_filter_pct=i0_filter_pct, n_pts=n_pts)
        if result is None:
            logger.warning("[average_and_save] '%s' — no valid scans to average", kw)
            continue
        q_out, I_out, sig_out, meta_out, n_files = result
        q_out, I_out, sig_out = _truncate_q(q_out, I_out, sig_out, q_min, q_max)

        # ── Write output .dat ─────────────────────────────────────────────────
        out_path = output_dir / f"{kw}_{n_files}files_{label_suffix}.dat"
        _write_averaged_dat(out_path, q_out, I_out, sig_out, kw,
                            meta_out, n_files=n_files)
        saved.append((kw, out_path))
        logger.info("[average_and_save] Saved %s (%d scans)", out_path.name, n_files)

    return saved


def average_batch(
    frames: list[dict],
    keyword: str,
    out_path: str | Path,
    *,
    i0_filter_pct: float = 0.0,
    n_pts: int = 1000,
    q_min: float | None = None,
    q_max: float | None = None,
) -> Path | None:
    """
    Average an explicit list of frame dicts and write a single averaged .dat.

    Unlike :func:`average_and_save` — which groups an entire folder by keyword —
    this averages exactly the frames passed in.  It is the building block for
    the viewer's auto-averaging monitor, which feeds it rolling batches of *N*
    consecutive frames for one sample.

    Parameters
    ----------
    frames : list[dict]
        Frame dicts as returned by :func:`read_folder` (each with ``q``, ``I``,
        ``sigma``, ``metadata``, ``filename``).
    keyword : str
        Sample keyword, written into the output header.
    out_path : str | Path
        Destination .dat file.  Parent directories are created if needed.
    i0_filter_pct : float
        If > 0, discard frames whose I0 deviates from the batch median by more
        than this percentage before averaging.
    n_pts : int
        Number of q points in the common log-spaced output grid.

    Returns
    -------
    Path | None
        The written path, or ``None`` if the batch had no usable frames.
    """
    if not frames:
        return None
    result = _average_group(frames, i0_filter_pct=i0_filter_pct, n_pts=n_pts)
    if result is None:
        return None
    q_out, I_out, sig_out, meta_out, n_used = result
    q_out, I_out, sig_out = _truncate_q(q_out, I_out, sig_out, q_min, q_max)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_averaged_dat(out_path, q_out, I_out, sig_out, keyword,
                        meta_out, n_files=n_used)
    logger.info("[average_batch] Saved %s (%d frames)", out_path.name, n_used)
    return out_path
