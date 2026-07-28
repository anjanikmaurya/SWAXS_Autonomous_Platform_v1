"""
src/simulator — physics-based mock 2D SAXS data for full-pipeline testing.

Generates real .raw detector frames (+ CSV/PDI metadata) from a hidden
recipe→size model when a collection is triggered in mock mode, so the entire
chain — reduction → averaging → subtraction → quality → analyzer → optimizer —
can be exercised with no beam and with a KNOWN ground truth to check against.
"""
from __future__ import annotations

from .ground_truth import truth_from_recipe, describe, DEFAULTS as TRUTH_DEFAULTS
from .pattern import (sphere_form_factor, schulz_weights, iq_curve,
                      background_curve, q_map, synthetic_q_map, load_mask,
                      beamstop_mask, simulate_frame)
from .writer import (frame_name, write_raw, counters, write_csv_metadata,
                     write_pdi_metadata, AcquisitionWriter, MARKER,
                     is_simulated_dir, mark_simulated_dir, assert_safe_to_simulate)
from .collector import SimulatedCollector, DEFAULTS as SIM_DEFAULTS

__all__ = [
    "truth_from_recipe", "describe", "TRUTH_DEFAULTS",
    "sphere_form_factor", "schulz_weights", "iq_curve", "background_curve",
    "q_map", "synthetic_q_map", "load_mask", "beamstop_mask", "simulate_frame",
    "frame_name", "write_raw", "counters", "write_csv_metadata",
    "write_pdi_metadata", "AcquisitionWriter", "MARKER", "is_simulated_dir",
    "mark_simulated_dir", "assert_safe_to_simulate",
    "SimulatedCollector", "SIM_DEFAULTS",
]
