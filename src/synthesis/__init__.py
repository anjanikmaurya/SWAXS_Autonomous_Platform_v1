"""
src/synthesis — the instrument contract for the Synthesis app (phase 0).

    types.py       what an instrument declares (Description, Parameter, Channel,
                   TestSpec, SetupItem) and returns (TestResult, Plan, Refusal, Result)
    instrument.py  the Instrument base class and the gated InstrumentSession
    registry.py    register / create / list instruments (empty by default)
    toy.py         a simulated stand-in used by the tests and as a template

Nothing here drives hardware, and the running reactor app does not import it.
Plan and next phases: docs/design/SYNTHESIS_PLATFORM_PLAN.md.
"""
from .instrument import GateError, Instrument, InstrumentSession, State
from .types import (ROLES, Channel, Description, Parameter, Plan, Refusal, Result,
                    SetupItem, Step, TestResult, TestSpec)
from . import registry

__all__ = ["GateError", "Instrument", "InstrumentSession", "State", "ROLES", "Channel",
           "Description", "Parameter", "Plan", "Refusal", "Result", "SetupItem", "Step",
           "TestResult", "TestSpec", "registry"]
