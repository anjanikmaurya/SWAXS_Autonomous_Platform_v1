"""
src/reduction/read_raw_file.py — Raw detector file reader
==========================================================
Reads binary .raw detector images into numpy arrays.
"""

import logging
from pathlib import Path
from typing import List

import numpy as np

__all__ = [
    "read_detector_image",  # Read a binary .raw detector file → numpy array
]

logger = logging.getLogger("swaxs_pipeline")


def read_detector_image(raw_file_path: Path, shape: List[int]) -> np.ndarray:
    """
    Read a raw binary detector file and return it as a 2-D NumPy array.

    Parameters
    ----------
    raw_file_path : Path
        Path to the .raw detector file.
    shape : list of int
        [num_rows, num_columns] — detector pixel dimensions.

    Returns
    -------
    np.ndarray
        2-D int32 array of pixel counts, shaped [rows, cols].

    Raises
    ------
    FileNotFoundError
        If the file does not exist.
    ValueError
        If the file size is inconsistent with the given shape.
    """
    raw_file_path = Path(raw_file_path)

    # Check existence BEFORE reading (original code checked it afterwards — crash risk)
    if not raw_file_path.exists():
        raise FileNotFoundError(f"Raw detector file not found: {raw_file_path}")

    expected_pixels = shape[0] * shape[1]
    data = np.fromfile(str(raw_file_path), dtype=np.int32)

    if data.size != expected_pixels:
        raise ValueError(
            f"File {raw_file_path.name} has {data.size} int32 values "
            f"but detector shape {shape} expects {expected_pixels}. "
            "Check that the detector_shapes in your config are correct."
        )

    data = data.reshape(shape)

    logger.debug(
        f"Read {raw_file_path.name}: shape={data.shape}, "
        f"min={data.min()}, max={data.max()}, mean={data.mean():.1f}"
    )

    return data
