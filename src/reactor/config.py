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


def hub_to_spec_dir(hub_path: str, mapping: dict | None) -> str | None:
    """Translate a hub (Windows) project folder to the Linux path SPEC writes to.

    ``mapping`` = {"from": "<windows prefix>", "to": "<linux prefix>"} — e.g.
    {"from": "X:\\bl1-5", "to": "/msd_data/checkout/bl1-5"}. Case-insensitive on
    the prefix, backslashes normalised to forward slashes. Returns the translated
    Linux path, or None if it can't be mapped (missing map or prefix mismatch) so
    the caller can leave data_dir untouched rather than send SPEC a bad path."""
    p = (hub_path or "").strip()
    frm = str((mapping or {}).get("from", "")).strip()
    to = str((mapping or {}).get("to", "")).strip()
    if not (p and frm and to):
        return None
    pn = p.replace("\\", "/")
    fn = frm.replace("\\", "/").rstrip("/")
    if pn.lower().startswith(fn.lower()):
        rest = pn[len(fn):].lstrip("/")
        return to.rstrip("/") + ("/" + rest if rest else "")
    return None
