
from pathlib import Path
import numpy as np

__all__ = ["read_dat_data_metadata"]

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
