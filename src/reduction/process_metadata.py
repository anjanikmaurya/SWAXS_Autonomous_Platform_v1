"""
src/reduction/process_metadata.py — Metadata extraction utilities
=================================================================
Extracts i0, bstop, and other metadata from CSV and PDI beamline files.
"""

import re
from pathlib import Path

import pandas as pd

__all__ = [
    "process_csv_metadata",    # Extract i0/bstop from experiment CSV file
    "process_pdi_full",        # Extract metadata from PDI beamline file
    "find_row_number_to_read", # Map raw file index → CSV row
    "get_saxs_pdi_from_waxs",  # Derive SAXS PDI path from WAXS PDI path
    "get_meta_from_pdi",       # Low-level PDI parser
]

# ─────────────────────────────────────────────────────────────────────────────
# CSV metadata
# ─────────────────────────────────────────────────────────────────────────────

def find_row_number_to_read(raw_file_path: Path) -> int:
    """
    Parse the 4-digit index from a raw filename (e.g. run_0009.raw → 9).
    This index is used as the row number when reading the paired CSV file.
    """
    match = re.search(r"_(\d{4})\.", str(raw_file_path))
    if match is None:
        raise RuntimeError(
            f"Could not parse 4-digit index from filename: {raw_file_path.name}\n"
            "Expected a filename like  experiment_0042.raw"
        )
    return int(match.group(1))


def process_csv_metadata(raw_file_path: Path) -> dict:
    """
    Find the CSV file that matches this .raw file, read the correct row,
    and return a dict of metadata values (I0, bstop, etc.).

    The CSV is expected to live one directory above the SAXS/ or WAXS/ subfolder.
    The CSV filename stem must appear somewhere in the .raw filename.
    """
    raw_file_path = Path(raw_file_path)
    csv_dir = raw_file_path.parent.parent   # up one level from SAXS/ or WAXS/

    # Match strategy:
    # 1. Strip the trailing _NNNN index from the raw filename stem so we compare
    #    apples to apples (the CSV has no index suffix).
    #    e.g. "sone_Run1_...scan1_0001" → "sone_Run1_...scan1"
    # 2. Find every CSV whose stem appears as a substring of that stripped name.
    #    This handles both cases:
    #    a. CSV stem == stripped raw stem (exact, e.g. both start with "sone_")
    #    b. CSV stem is embedded (e.g. raw has "sone_" prefix but CSV does not)
    # 3. Among multiple matches, pick the LONGEST stem so we prefer the most
    #    specific CSV over a shorter accidental substring.
    raw_stem_no_idx = re.sub(r'_\d{4}$', '', raw_file_path.stem)

    csv_file   = None
    best_len   = -1
    for candidate in csv_dir.glob("*.csv"):
        stem = candidate.stem
        if stem in raw_stem_no_idx and len(stem) > best_len:
            csv_file = candidate
            best_len = len(stem)

    if csv_file is None:
        raise RuntimeError(
            f"No matching CSV found for {raw_file_path.name} "
            f"in directory {csv_dir}.\n"
            f"Searched for a CSV whose name (without extension) appears inside "
            f"'{raw_stem_no_idx}'. "
            f"CSVs present: {[f.name for f in csv_dir.glob('*.csv')]}"
        )

    df = pd.read_csv(csv_file)
    row_index = find_row_number_to_read(raw_file_path)

    if row_index >= len(df):
        raise RuntimeError(
            f"CSV {csv_file.name} has {len(df)} rows but file "
            f"{raw_file_path.name} requires row index {row_index}."
        )

    row_dict = df.iloc[row_index].to_dict()
    # Strip leading/trailing whitespace from column names
    return {k.strip(): v for k, v in row_dict.items()}


# ─────────────────────────────────────────────────────────────────────────────
# PDI metadata
# ─────────────────────────────────────────────────────────────────────────────

def get_saxs_pdi_from_waxs(waxs_pdi_path: str) -> str:
    """
    Given a WAXS PDI path, locate the corresponding SAXS PDI file.
    Used when a WAXS PDI is empty and the counters must be read from SAXS.
    """
    pattern = r"(Run\d{1,3}.*scan\d_\d{4})"
    match = re.search(pattern, waxs_pdi_path)
    if match is None:
        raise RuntimeError(
            f"Could not extract shared Run/scan identifier from: {waxs_pdi_path}"
        )
    shared = match.group(1)

    saxs_dir = Path(waxs_pdi_path).parent.parent / "SAXS"
    for pdi_file in saxs_dir.glob("*.pdi"):
        if shared in pdi_file.name:
            return str(pdi_file)

    raise FileNotFoundError(
        f"No corresponding SAXS PDI found for WAXS PDI {waxs_pdi_path}\n"
        f"Searched in: {saxs_dir}"
    )


def process_pdi_full(raw_file_path: Path, detector_type: str) -> dict:
    """
    Read metadata from the .pdi sidecar file next to a .raw file.
    If the WAXS PDI is empty (no 'All Counters' section), falls back to
    the matching SAXS PDI.

    Returns a dict of counter values (I0, bstop, etc.).
    """
    pdi_path = str(raw_file_path) + ".pdi"

    if not Path(pdi_path).exists():
        raise FileNotFoundError(f"PDI file not found: {pdi_path}")

    with open(pdi_path, "r", encoding="utf-8") as f:
        content = f.read()

    det = detector_type.upper()
    if det not in ("SAXS", "WAXS"):
        raise RuntimeError(f"detector_type must be 'SAXS' or 'WAXS', got '{detector_type}'")

    if "All Counters" not in content and det == "WAXS":
        # WAXS PDI is empty — fall back to the matching SAXS PDI
        pdi_path = get_saxs_pdi_from_waxs(pdi_path)

    counters, _motors, _extras = get_meta_from_pdi(pdi_path)

    if not counters:
        raise RuntimeError(
            f"Could not parse counter values from PDI file: {pdi_path}\n"
            "The PDI may be malformed or use an unsupported format."
        )

    return counters


def get_meta_from_pdi(pdi_file: str) -> tuple:
    """
    Parse counter and motor values from a PDI file.

    Returns (Counters, Motors, Extras) as dicts.
    Raises RuntimeError (instead of silently returning empty dicts) if
    parsing fails, so callers get a clear error message.
    """
    with open(pdi_file, "r", encoding="utf-8") as f:
        data = f.read()

    data = data.replace("\n", ";")

    try:
        # Standard format: 'All Counters' section
        counters_raw = re.search(r"All Counters;(.*);;# All Motors", data).group(1)
        cts = re.split(r";|=", counters_raw)
        Counters = {c.split()[0]: float(cs) for c, cs in zip(cts[::2], cts[1::2])}

        motors_raw = re.search(r"All Motors;(.*);#", data).group(1)
        cts = re.split(r";|=", motors_raw)
        Motors = {c.split()[0]: float(cs) for c, cs in zip(cts[::2], cts[1::2])}

    except AttributeError:
        # Fallback format: diffractometer motor positions only (no counters section)
        ss1 = r"# Diffractometer Motor Positions for image;# "
        ss2 = r";# Calculated Detector Calibration Parameters for image:"
        try:
            motors_raw = re.search(f"{ss1}(.*){ss2}", data).group(1)
        except AttributeError:
            raise RuntimeError(
                f"Could not parse PDI file: {pdi_file}\n"
                "Neither the standard nor the fallback format was recognised."
            )
        cts = re.split(r";|=", motors_raw)
        Motors = {c.split()[0]: float(cs) for c, cs in zip(cts[::2], cts[1::2])}
        if "2Theta" in Motors:
            Motors["TwoTheta"] = Motors["2Theta"]
        # No counter section in this format — return empty Counters
        Counters = {}

    Extras = {}
    last_semicolon = data.rindex(";")
    tail = data[last_semicolon + 1:]
    if tail.strip():
        Extras["epoch"] = tail

    return Counters, Motors, Extras
