"""
src/reactor/config.py — load and normalise reactor/config.yml.
"""

from __future__ import annotations

from pathlib import Path

import yaml

PUMP_NAMES = ["pd_top_precursor", "oleylamine", "top", "ode_dilution", "ode_flush"]
REAGENT_PUMPS = ["pd_top_precursor", "oleylamine", "top", "ode_dilution"]  # not ode_flush
FLUSH_PUMP = "ode_flush"

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "reactor" / "config.yml"


def load_config(path: str | Path | None = None) -> dict:
    """Load the reactor config YAML, falling back to reactor/config.yml."""
    p = Path(path) if path else _DEFAULT_CONFIG
    with open(p, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("pumps", {})
    cfg.setdefault("bounds", {})
    cfg.setdefault("safety", {})
    cfg.setdefault("temperature", {})
    cfg.setdefault("run", {})
    cfg.setdefault("flush", {})
    cfg.setdefault("folders", {})
    cfg["_path"] = str(p)
    return cfg
