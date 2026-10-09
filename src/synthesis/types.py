"""
src/synthesis/types.py — the vocabulary of the instrument contract.

Plain dataclasses, no hardware and no Flask: what an instrument declares about
itself (Description), what its checks return (TestResult), and what a recipe
turns into (Plan, or a Refusal when it cannot be made). See
docs/design/SYNTHESIS_PLATFORM_PLAN.md, section 2.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

ROLES = ("synthesis", "probe")           # makes/changes a sample · measures it


@dataclass(frozen=True)
class Parameter:
    """A setting the instrument accepts (a recipe field or a setup value)."""
    name: str
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None
    default: Any = None
    description: str = ""

    def check(self, value: Any) -> Optional[str]:
        """None if ``value`` is acceptable, else the reason it is not."""
        if self.min is None and self.max is None:
            return None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return f"{self.name} is not a number: {value!r}"
        if self.min is not None and v < self.min:
            return f"{self.name} {v:g} is below {self.min:g} {self.unit}".rstrip()
        if self.max is not None and v > self.max:
            return f"{self.name} {v:g} is above {self.max:g} {self.unit}".rstrip()
        return None


@dataclass(frozen=True)
class Channel:
    """A live reading the instrument streams (plotted, stored, supervised)."""
    name: str
    unit: str = ""
    rate_hz: float = 1.0
    safe_min: Optional[float] = None     # outside → the supervisor trips safe_state
    safe_max: Optional[float] = None
    warn_min: Optional[float] = None     # outside → a warning only
    warn_max: Optional[float] = None
    plot_group: str = ""

    def level(self, value: float) -> str:
        """'ok' | 'warn' | 'trip' for one reading."""
        if (self.safe_min is not None and value < self.safe_min) or \
           (self.safe_max is not None and value > self.safe_max):
            return "trip"
        if (self.warn_min is not None and value < self.warn_min) or \
           (self.warn_max is not None and value > self.warn_max):
            return "warn"
        return "ok"


@dataclass(frozen=True)
class TestSpec:
    """A named hardware check. ``validity_s``: how long a pass counts (None = session)."""
    __test__ = False                     # not a pytest test class
    id: str
    title: str
    required: bool = True
    validity_s: Optional[float] = None


@dataclass
class TestResult:
    __test__ = False                     # not a pytest test class
    ok: bool
    value: Any = None
    expected: Any = None
    hint: str = ""                       # what to do when it fails
    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class SetupItem:
    """Something to configure before running (calibration, priming, plate map)."""
    id: str
    title: str
    parameters: tuple = ()
    required: bool = True


@dataclass(frozen=True)
class Description:
    """Everything the platform needs to show and gate an instrument, without hardware."""
    id: str
    title: str
    role: str                            # one of ROLES
    kind: str = ""                       # "flow_reactor", "beamline", "uv_vis", …
    capabilities: tuple = ()             # "flush", "react", "measure", "monitor", …
    parameters: tuple = ()               # Parameter, the recipe fields it consumes
    channels: tuple = ()                 # Channel
    tests: tuple = ()                    # TestSpec
    setup: tuple = ()                    # SetupItem

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {self.role!r}")
        for group, items in (("parameter", self.parameters), ("channel", self.channels),
                             ("test", self.tests), ("setup item", self.setup)):
            keys = [getattr(i, "name", None) or getattr(i, "id") for i in items]
            if len(keys) != len(set(keys)):
                raise ValueError(f"{self.id}: duplicate {group} names {keys}")


@dataclass(frozen=True)
class Step:
    capability: str
    params: dict = field(default_factory=dict)
    duration_s: float = 0.0


@dataclass(frozen=True)
class Plan:
    """How an instrument will make (or measure) one recipe."""
    steps: tuple = ()

    @property
    def duration_s(self) -> float:
        return sum(s.duration_s for s in self.steps)


@dataclass(frozen=True)
class Refusal:
    """The recipe cannot be made; nothing moved."""
    reason: str
    parameter: str = ""


@dataclass
class Result:
    ok: bool
    message: str = ""
    data: dict = field(default_factory=dict)
