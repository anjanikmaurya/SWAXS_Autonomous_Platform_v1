"""src/fitting/ — pluggable per-profile fitting methods.

Each method maps a subtracted I(q) to the standard result dict
(distribution / size / pdi / invariant / confidence / guinier / fit /
diagnostics) that `analyzer/app.py::_analyze_file`, the manifest writer and the
fit plot already consume. New fitters MUST return that same shape.

A method is a small object with:
    id     : str   — stable key ("ls_sphere")
    label  : str   — UI label ("Least-squares sphere")
    ready  : bool  — True if usable now; False shows as "soon" and is not selectable
    fit(q, I, sigma=None, **opts) -> dict

Register built-ins by importing their modules at the bottom. Keeping ONE
registry here (not a private dict per app) is what lets the analyzer list and
switch methods without importing each fitter directly.
"""
from __future__ import annotations

DEFAULT_ID = "ls_sphere"

_REGISTRY: dict = {}        # id -> fitter object
_SOON: list = []            # [{"id","label"}] placeholders for not-yet-built methods


def register(fitter) -> object:
    """Register a ready fitter object (must have id/label/fit)."""
    _REGISTRY[fitter.id] = fitter
    return fitter


def register_soon(method_id: str, label: str) -> None:
    """Advertise a planned method in the UI without a working implementation."""
    _SOON.append({"id": method_id, "label": label})


def get(fitter_id: str | None):
    """The requested fitter, or the default when unknown/empty."""
    return _REGISTRY.get(fitter_id or "") or _REGISTRY[DEFAULT_ID]


#: one-line, plain-language description shown under the Setup dropdown.
_DESC = {
    "ls_sphere":    "Polydisperse-sphere form-factor fit by least squares "
                    "(Schulz / log-normal); physics-based and interpretable.",
    "ml_regressor": "A trained model predicts size / PDI directly from the curve — "
                    "near-instant, uncertainty-aware (coming soon).",
    "llm_assisted": "An AI agent runs the analysis tools, picks the model and "
                    "explains the result in plain language (coming soon).",
}


def available() -> list[dict]:
    """Menu for the Setup tab: ready methods first, then 'soon' placeholders."""
    out = [{"id": f.id, "label": f.label, "ready": True, "desc": _DESC.get(f.id, "")}
           for f in _REGISTRY.values()]
    out += [{"id": s["id"], "label": s["label"], "ready": False, "desc": _DESC.get(s["id"], "")}
            for s in _SOON]
    return out


# ── built-ins ──────────────────────────────────────────────────────────────────
from . import ls_sphere          # noqa: E402,F401  (registers "ls_sphere")
from . import ml_regressor       # noqa: E402,F401  (registers "ml_regressor")
from . import llm_assisted       # noqa: E402,F401  (registers "llm_assisted")
