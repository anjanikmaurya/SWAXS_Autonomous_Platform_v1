"""
src/preprocess — pre-reduction utilities: convert raw detector files to CBF and
drive pyFAI calibration (AgBehenate / LaB6 …) to generate .poni files.
"""
from __future__ import annotations

from .raw_convert import (
    DEFAULT_SHAPES, find_raw_files, detect_shape, read_raw,
    frame_stats, raw_to_cbf, convert_dir,
)
from .calib import (
    CALIBRANTS, build_calib2_command, launch_calib2, auto_calibrate,
    list_poni_files,
)
from .sftp_sync import (
    SftpSync, test_connection as sftp_test, load_config as sftp_load_config,
    save_config as sftp_save_config, relative_local_path,
)

__all__ = [
    "DEFAULT_SHAPES", "find_raw_files", "detect_shape", "read_raw",
    "frame_stats", "raw_to_cbf", "convert_dir",
    "CALIBRANTS", "build_calib2_command", "launch_calib2", "auto_calibrate",
    "list_poni_files",
    "SftpSync", "sftp_test", "sftp_load_config", "sftp_save_config", "relative_local_path",
]
