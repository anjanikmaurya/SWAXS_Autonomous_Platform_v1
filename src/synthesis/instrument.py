"""
src/synthesis/instrument.py — the instrument contract and its gated session.

Every synthesis route (flow reactor, later a liquid handling robot or a syringe
well plate) and every probe (SAXS/WAXS beamline, later UV Vis) is one
``Instrument`` subclass. The platform never talks to hardware directly; it holds
an ``InstrumentSession`` around the instrument, which enforces the order an
operator works in:

    disconnected → connected → tested → set up → ready ⇄ running
                       any failure or e-stop → fault (safe state) → reset

and makes sure a failure always ends in ``safe_state()``.

Status (October 2026): contract only. The existing flow reactor
(src/reactor/, reactor/app.py) does NOT use this yet; it is ported in phase 1
of docs/design/SYNTHESIS_PLATFORM_PLAN.md, behind a test that proves the run
sequence is unchanged.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Union

from .types import Description, Plan, Refusal, Result, Step, TestResult


class State(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    TESTED = "tested"
    SET_UP = "set_up"
    READY = "ready"
    RUNNING = "running"
    FAULT = "fault"


class Instrument(ABC):
    """What a module implements. Only ``describe``, ``connect``, ``safe_state``
    and ``estop`` are mandatory; the rest default to "not supported"."""

    simulated: bool = True               # a simulated twin; False only for real hardware

    @abstractmethod
    def describe(self) -> Description: ...

    @abstractmethod
    def connect(self, cfg: dict) -> Result: ...

    def disconnect(self) -> None:
        pass

    def run_test(self, test_id: str) -> TestResult:
        return TestResult(False, hint=f"test {test_id!r} is not implemented")

    def setup(self, item_id: str, values: dict) -> Result:
        return Result(False, f"setup item {item_id!r} is not implemented")

    def compile(self, recipe: dict) -> Union[Plan, Refusal]:
        return Refusal("this instrument does not accept recipes")

    def execute(self, step: Step) -> Result:
        return Result(False, f"capability {step.capability!r} is not implemented")

    def read_channels(self) -> dict:
        return {}

    @abstractmethod
    def safe_state(self) -> None:
        """Stop motion and flow, close valves, heaters to idle. Must work from a
        cold connect and must be safe to call twice."""

    @abstractmethod
    def estop(self) -> None:
        """Fastest possible stop, no questions asked."""

    def clean(self) -> Result:
        return Result(True, "nothing to clean")


class GateError(RuntimeError):
    """An action was asked for in a state that does not allow it."""


class InstrumentSession:
    """The platform's handle on one instrument: state, gating, test record, log."""

    def __init__(self, instrument: Instrument, clock=time.time):
        self.instrument = instrument
        self.desc = instrument.describe()
        self.state = State.DISCONNECTED
        self.tests: dict[str, TestResult] = {}
        self.setup_done: set[str] = set()
        self.events: list[dict] = []
        self.fault = ""
        self._clock = clock
        self._lock = threading.RLock()

    # ── record ───────────────────────────────────────────────────────────────
    def _log(self, kind: str, msg: str, **data) -> None:
        self.events.append({"t": self._clock(), "kind": kind, "msg": msg, **data})

    def _to(self, state: State, why: str = "") -> None:
        if state != self.state:
            self._log("state", f"{self.state.value} → {state.value}" + (f" ({why})" if why else ""))
            self.state = state

    def _need(self, *allowed: State, action: str) -> None:
        if self.state not in allowed:
            raise GateError(f"{self.desc.title}: cannot {action} while {self.state.value}")

    def _trip(self, why: str) -> None:
        """Any failure: safe state first, then FAULT. Never raises."""
        self.fault = why
        try:
            self.instrument.safe_state()
            self._log("safe_state", "safe state applied")
        except Exception as exc:                       # still go to FAULT, and say so
            self._log("error", f"safe_state FAILED: {exc}")
        self._to(State.FAULT, why)

    # ── what blocks Ready ────────────────────────────────────────────────────
    def missing(self) -> list[str]:
        """Human readable list of what still blocks Ready (empty = nothing)."""
        now, out = self._clock(), []
        for t in self.desc.tests:
            r = self.tests.get(t.id)
            if not t.required:
                continue
            if r is None or not r.ok:
                out.append(f"test: {t.title}")
            elif t.validity_s is not None and now - r.at > t.validity_s:
                out.append(f"test expired: {t.title}")
        for s in self.desc.setup:
            if s.required and s.id not in self.setup_done:
                out.append(f"setup: {s.title}")
        return out

    def _refresh(self) -> None:
        if self.state in (State.DISCONNECTED, State.FAULT, State.RUNNING):
            return
        miss = self.missing()
        tests_ok = not any(m.startswith("test") for m in miss)
        setup_ok = not any(m.startswith("setup") for m in miss)
        target = (State.READY if tests_ok and setup_ok else
                  State.TESTED if tests_ok else
                  State.SET_UP if setup_ok and self.setup_done else
                  State.CONNECTED)
        self._to(target)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def connect(self, cfg: dict | None = None) -> Result:
        with self._lock:
            self._need(State.DISCONNECTED, action="connect")
            try:
                r = self.instrument.connect(cfg or {})
            except Exception as exc:
                r = Result(False, f"connect raised: {exc}")
            self._log("connect", r.message or ("connected" if r.ok else "failed"), ok=r.ok)
            if r.ok:
                self._to(State.CONNECTED)
                self._refresh()
            return r

    def run_test(self, test_id: str) -> TestResult:
        with self._lock:
            self._need(State.CONNECTED, State.TESTED, State.SET_UP, State.READY, action="run a test")
            if test_id not in {t.id for t in self.desc.tests}:
                raise KeyError(f"{self.desc.id} has no test {test_id!r}")
            try:
                r = self.instrument.run_test(test_id)
            except Exception as exc:
                r = TestResult(False, hint=f"the test raised: {exc}")
            r.at = self._clock()
            self.tests[test_id] = r
            self._log("test", f"{test_id}: {'pass' if r.ok else 'FAIL'}", ok=r.ok, hint=r.hint)
            self._refresh()
            return r

    def setup(self, item_id: str, values: dict | None = None) -> Result:
        with self._lock:
            self._need(State.CONNECTED, State.TESTED, State.SET_UP, State.READY, action="change setup")
            r = self.instrument.setup(item_id, values or {})
            if r.ok:
                self.setup_done.add(item_id)
            self._log("setup", f"{item_id}: {'saved' if r.ok else r.message}", ok=r.ok)
            self._refresh()
            return r

    def compile(self, recipe: dict) -> Union[Plan, Refusal]:
        """Check a recipe against declared limits, then let the module plan it.
        Pure: nothing moves, any state but FAULT."""
        for p in self.desc.parameters:
            if p.name in recipe:
                why = p.check(recipe[p.name])
                if why:
                    return Refusal(why, p.name)
        try:
            return self.instrument.compile(recipe)
        except Exception as exc:
            return Refusal(f"compile raised: {exc}")

    def run(self, plan: Plan) -> Result:
        """Execute a compiled plan step by step. Only from READY. Any failure or
        exception → safe_state() and FAULT."""
        with self._lock:
            self._need(State.READY, action="run")
            if self.missing():                          # e.g. a test expired since
                raise GateError(f"{self.desc.title}: not ready: {', '.join(self.missing())}")
            self._to(State.RUNNING)
        for step in plan.steps:
            if self.state != State.RUNNING:            # e-stopped from another thread
                return Result(False, f"stopped: {self.fault or self.state.value}")
            try:
                r = self.instrument.execute(step)
            except Exception as exc:
                r = Result(False, f"{step.capability} raised: {exc}")
            self._log("step", f"{step.capability}: {'ok' if r.ok else r.message}", ok=r.ok)
            if not r.ok:
                with self._lock:
                    self._trip(f"step {step.capability} failed: {r.message}")
                return r
        with self._lock:
            if self.state == State.RUNNING:
                self._to(State.READY, "plan complete")
                self._refresh()
        return Result(True, "plan complete")

    def estop(self) -> None:
        """Always allowed, from any state. Never raises."""
        with self._lock:
            try:
                self.instrument.estop()
                self._log("estop", "emergency stop")
            except Exception as exc:
                self._log("error", f"estop FAILED: {exc}")
            self._trip("emergency stop")

    def reset(self) -> None:
        """Leave FAULT. Tests must pass again before Ready."""
        with self._lock:
            self._need(State.FAULT, action="reset")
            self.tests.clear()
            self.fault = ""
            self._to(State.CONNECTED, "reset")
            self._refresh()

    def disconnect(self) -> None:
        with self._lock:
            if self.state == State.RUNNING:
                self._trip("disconnected while running")
            try:
                self.instrument.disconnect()
            finally:
                self.tests.clear()
                self.setup_done.clear()
                self._to(State.DISCONNECTED)

    def status(self) -> dict:
        return {"id": self.desc.id, "title": self.desc.title, "role": self.desc.role,
                "simulated": self.instrument.simulated, "state": self.state.value,
                "fault": self.fault, "missing": self.missing(),
                "tests": {k: {"ok": v.ok, "hint": v.hint, "at": v.at} for k, v in self.tests.items()}}
