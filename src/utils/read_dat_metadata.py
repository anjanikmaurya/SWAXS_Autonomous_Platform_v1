
from pathlib import Path
import numpy as np

__all__ = ["read_dat_data_metadata", "q_unit_from_header", "q_to_nm"]

# Global constant for metadata section identifier
METADATA_SECTION_PREFIX = "# METADATA INFORMATION"
def read_dat_data_metadata(file_path: Path) -> tuple:
    """
    Reads a .dat file and extracts header comments, data, and metadata
    Returns: (header_lines, q_values, intensity_values, sigma_values, metadata_dict)
    """
    header_lines = []
    data_lines = []
    metadata_dict = {}
    in_metadata_section = False

    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.startswith('#'):
                line_stripped = line.strip()

                # Check if we're entering the metadata section
                if line_stripped.startswith(METADATA_SECTION_PREFIX):
                    in_metadata_section = True
                    continue

                # If we're in metadata section, parse the metadata
                if in_metadata_section:
                    # Parse YML format: # key: value
                    if ':' in line_stripped:
                        key_value = line_stripped[1:].strip()  # Remove the '#'
                        if ':' in key_value:
                            key, value = key_value.split(':', 1)
                            key = key.strip()
                            value = value.strip()
                            try:
                                # Convert to float as specified
                                metadata_dict[key] = float(value)
                            except ValueError:
                                # If conversion fails, skip this metadata entry
                                continue
                else:
                    # Regular header line (not metadata)
                    header_lines.append(line_stripped)
            else:
                # Skip empty lines
                if line.strip():
                    data_lines.append(line.strip())

    # Parse data (skip the column header line if it exists in data_lines)
    data_arrays = []
    for line in data_lines:
        # Skip column header lines that might not start with #
        if 'q_nm' in line or 'sigma' in line:
            continue
        try:
            values = [float(x) for x in line.split()]
            if len(values) == 3:  # q, I, sigma
                data_arrays.append(values)
        except ValueError:
            continue  # Skip malformed lines

    if not data_arrays:
        raise ValueError(f"No valid data found in {file_path}")

    data_arrays = np.array(data_arrays)
    q_values = data_arrays[:, 0]
    intensity_values = data_arrays[:, 1]
    sigma_values = data_arrays[:, 2]

    return header_lines, q_values, intensity_values, sigma_values, metadata_dict


# ── q unit (Q7) ───────────────────────────────────────────────────────────────
# Background writes ``q_A-1`` when the operator picks Angstrom output; everything
# else on the platform is nm^-1. A reader that ignores the label is 10x off.
_A_MARKERS = ("q_a-1", "q_a^-1", "q(a-1)", "q (a-1)", "a^-1", "a-1)", "å⁻¹", "å^-1",
              "å-1", "1/å", "angstrom")
_NM_MARKERS = ("q_nm", "nm^-1", "nm-1", "nm⁻¹", "1/nm")


def _unit_of_line(line: str):
    t = line.lower()
    a = any(m in t for m in _A_MARKERS)
    n = any(m in t for m in _NM_MARKERS)
    if a and not n:
        return "A^-1"
    if n and not a:
        return "nm^-1"
    return None


def q_unit_from_header(header_lines) -> str | None:
    """The q unit a .dat header states: "A^-1", "nm^-1", or None if it says nothing.

    The column-label lines (``# Columns: q_A-1 ...`` or ``# q_nm-1  I  sigma``)
    decide first; only if none of them names a unit is the rest of the header used.
    """
    lines = [str(l) for l in (header_lines or [])]
    col = [l for l in lines
           if "columns" in l.lower() or l.lstrip("#").strip().lower().startswith("q")]
    for l in reversed(col):           # the last column label is the one above the data
        u = _unit_of_line(l)
        if u:
            return u
    units = {u for u in (_unit_of_line(l) for l in lines) if u}
    return units.pop() if len(units) == 1 else None


def q_to_nm(q, header_lines, default: str = "nm^-1"):
    """Return (q in nm^-1, unit read). Angstrom^-1 is multiplied by 10; I is untouched.

    ``default`` is used only when the header states no unit (e.g. a UI override
    for an unlabelled external file)."""
    unit = q_unit_from_header(header_lines) or default or "nm^-1"
    q = np.asarray(q, float)
    if str(unit).lower().replace(" ", "").startswith(("a", "å")):
        return q * 10.0, "A^-1"
    return q, "nm^-1"
