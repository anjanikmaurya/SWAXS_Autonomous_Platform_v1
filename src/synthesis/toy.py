"""
src/synthesis/toy.py — a simulated stand-in instrument, for tests and as a template.

NOT hardware and not registered anywhere. It exists so the contract can be
exercised end to end, and so the first real module has a short example to copy:
describe → connect → tests → setup → compile → execute → safe_state.
``fail_on`` lets a test make one test or one step fail on purpose.
"""
from __future__ import annotations

from .instrument import Instrument
from .types import (Channel, Description, Parameter, Plan, Refusal, Result, SetupItem,
                    Step, TestResult, TestSpec)


class ToyMixer(Instrument):
    simulated = True

    def __init__(self, fail_on: tuple = ()):
        self.fail_on = set(fail_on)
        self.connected = False
        self.flow = 0.0
        self.safe_calls = 0
        self.estops = 0
        self.ran: list[str] = []

    def describe(self) -> Description:
        return Description(
            id="toy_mixer", title="Toy mixer (simulated)", role="synthesis", kind="toy",
            capabilities=("mix", "flush"),
            parameters=(Parameter("flow", "µL/min", 0, 100, 50),
                        Parameter("time_s", "s", 1, 3600, 10)),
            channels=(Channel("flow", "µL/min", 2.0, safe_max=120, warn_max=105, plot_group="flow"),),
            tests=(TestSpec("ports", "Ports open"), TestSpec("sensor", "Sensor reads", validity_s=3600),
                   TestSpec("leak", "Leak test", required=False)),
            setup=(SetupItem("prime", "Prime the line"),),
        )

    def connect(self, cfg):
        if "connect" in self.fail_on:
            return Result(False, "port not found")
        self.connected = True
        return Result(True, "connected (simulated)")

    def disconnect(self):
        self.connected = False

    def run_test(self, test_id):
        if test_id in self.fail_on:
            return TestResult(False, value=None, expected="reply", hint=f"check the {test_id} cable")
        return TestResult(True, value="ok")

    def setup(self, item_id, values):
        return Result(True, f"{item_id} done")

    def compile(self, recipe):
        if recipe.get("flow", 0) > 0 and recipe.get("time_s") is None:
            return Refusal("time_s is required with a flow", "time_s")
        return Plan((Step("mix", {"flow": recipe.get("flow", 50)}, recipe.get("time_s", 10)),
                     Step("flush", {}, 5)))

    def execute(self, step):
        if step.capability in self.fail_on:
            raise RuntimeError(f"{step.capability} driver error")
        self.ran.append(step.capability)
        self.flow = step.params.get("flow", 0.0)
        return Result(True)

    def read_channels(self):
        return {"flow": self.flow}

    def safe_state(self):
        self.safe_calls += 1
        self.flow = 0.0

    def estop(self):
        self.estops += 1
        self.flow = 0.0
