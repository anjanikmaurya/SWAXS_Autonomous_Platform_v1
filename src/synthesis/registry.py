"""
src/synthesis/registry.py — which instruments exist.

A module registers a factory under its id; the platform lists and creates them
from here. Nothing is registered by default: the flow reactor joins in phase 1,
robot and well plate routes only when they are developed.
"""
from __future__ import annotations

from typing import Callable, Dict

from .instrument import Instrument

_FACTORIES: Dict[str, Callable[[], Instrument]] = {}


def register(instrument_id: str, factory: Callable[[], Instrument]) -> None:
    if instrument_id in _FACTORIES and _FACTORIES[instrument_id] is not factory:
        raise ValueError(f"instrument {instrument_id!r} is already registered")
    _FACTORIES[instrument_id] = factory


def unregister(instrument_id: str) -> None:
    _FACTORIES.pop(instrument_id, None)


def create(instrument_id: str) -> Instrument:
    try:
        return _FACTORIES[instrument_id]()
    except KeyError:
        raise KeyError(f"no instrument {instrument_id!r}; registered: {sorted(_FACTORIES)}") from None


def available() -> list[str]:
    return sorted(_FACTORIES)
