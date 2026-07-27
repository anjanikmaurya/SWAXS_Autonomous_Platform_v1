"""
src/preprocess/raw_convert.py — read SSRL BL1-5 binary .raw detector files and
write them out as **CBF** (via fabio), with keyword filtering and automatic
SAXS/WAXS detection by array size. Pre-reduction step (calibrant conversion,
quick QA) — reduction still reads .raw directly.

Raw format: little-endian int32, row-major, no header (same as read_raw_file /
the group's process_raw_with_keywords). Detector is inferred from the pixel count.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

# (rows, cols) per detector — override from config (detector_shapes).
DEFAULT_SHAPES = {"SAXS": (1043, 981), "WAXS": (195, 487)}


def find_raw_files(raw_dir, keywords=None):
    """List *.raw files in ``raw_dir`` whose name contains ANY of ``keywords``
    (case-insensitive). ``keywords`` None/empty → all .raw files. Sorted."""
    raw_dir = Path(raw_dir)
    if isinstance(keywords, str):
        keywords = [keywords]
    kws = [k.lower() for k in (keywords or []) if str(k).strip()]
    out = []
    for f in sorted(os.listdir(raw_dir)):
        if not f.lower().endswith(".raw"):
            continue
        if kws and not any(k in f.lower() for k in kws):
            continue
        out.append(f)
    return out


def detect_shape(size, shapes=None, name=""):
    """Return (detector, (rows, cols)) for a raw of ``size`` pixels.

    Matches the pixel count against the known shapes; if the count is ambiguous
    or unknown, falls back to a name hint (waxs/100k/si → WAXS) then SAXS."""
    shapes = shapes or DEFAULT_SHAPES
    for det, sh in shapes.items():
        if size == sh[0] * sh[1]:
            return det, tuple(sh)
    nl = name.lower()
    if any(k in nl for k in ("waxs", "100k", "si")) and "WAXS" in shapes:
        return "WAXS", tuple(shapes["WAXS"])
    return None, None


def read_raw(file_path, shapes=None):
    """Read a .raw → (detector, 2-D int32 array). Raises ValueError on an
    unrecognised size."""
    file_path = Path(file_path)
    raw = np.fromfile(str(file_path), dtype=np.int32)
    det, sh = detect_shape(raw.size, shapes, file_path.name)
    if sh is None:
        want = " or ".join(str(s[0] * s[1]) for s in (shapes or DEFAULT_SHAPES).values())
        raise ValueError(f"{file_path.name}: unexpected size {raw.size} (expected {want})")
    return det, raw.reshape(sh)


def frame_stats(data):
    """Quick per-frame QA stats."""
    d = np.asarray(data)
    finite = d[np.isfinite(d)]
    return {
        "min": int(finite.min()) if finite.size else 0,
        "max": int(finite.max()) if finite.size else 0,
        "total_counts": int(finite.sum()) if finite.size else 0,
        "hot_pixels": int((finite > 1e6).sum()),
        "shape": list(d.shape),
    }


def raw_to_cbf(file_path, out_dir, shapes=None):
    """Convert one .raw → .cbf (true CBF via fabio). Returns a result dict."""
    import fabio                                        # noqa: PLC0415
    file_path = Path(file_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    det, data = read_raw(file_path, shapes)
    out_path = out_dir / (file_path.stem + ".cbf")
    fabio.cbfimage.CbfImage(data=data.astype(np.int32)).write(str(out_path))
    return {"file": file_path.name, "detector": det, "out": str(out_path),
            "stats": frame_stats(data), "ok": True}


def convert_dir(raw_dir, keywords=None, shapes=None, out_subdir="cbf_output"):
    """Convert every matching .raw in ``raw_dir`` → CBF under
    ``raw_dir/out_subdir``. Fail-soft: a bad file is recorded, not fatal.
    Returns (results, out_dir)."""
    raw_dir = Path(raw_dir)
    out_dir = raw_dir / out_subdir
    results = []
    for name in find_raw_files(raw_dir, keywords):
        try:
            results.append(raw_to_cbf(raw_dir / name, out_dir, shapes))
        except Exception as exc:
            results.append({"file": name, "ok": False, "error": str(exc)})
    return results, str(out_dir)
