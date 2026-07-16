"""
src/beamline/driver.py — beamline (SPEC) hardware layer.

Wraps the SSRL BL1-5 SPEC infoserver ("bServer") HTTP API used by the group's
MSD.py: every action is a GET to ``{base_url}<sis_command>`` with a ``spec_cmd``
query param, exactly as the operator scripts do. The reactor calls this the same
way it calls the pump driver.

Two backends, like the pumps:
  • "mock" — simulates a temperature ramp + counters; ``collect`` is a no-op that
    logs. Lets the whole closed loop run and be tested with no beamline.
  • "real" — talks to the bServer over HTTP.

Site-specific SPEC strings (the exact temperature counter name and the collect /
newfile macros) come from config so they can be set per rig without code changes.
Defaults are marked ⚠ CONFIRM — verify them on the beamline before a real run.
"""

from __future__ import annotations

import time
from pathlib import Path


# ── config ─────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "backend": "mock",                              # "mock" | "real"
    "base_url": "http://127.0.0.1:18085/SIS/",
    "temp_counter": "temp",                         # ⚠ CONFIRM: counter reporting temperature
    "bstop_counter": "bstop",
    "i0_counter": "i0",
    "set_temp_cmd": "csettemp {T}",                 # ramp command (from MSD.py)
    "newfile_cmd": "newfile {path}",                # sets save dir + prefix (named-cmd mode)
    "collect_cmd": "ct {exposure}",                 # 2D acquisition (named-cmd mode)
    # ── macro-file mode (preferred): fill a .txt SPEC macro and qdo it ──────────
    "macro_file": "",                               # path to the collection macro template (.txt)
    "macro_out_file": "",                           # where the filled macro is written (SPEC-readable)
    "qdo_cmd": 'qdo "{file}"',                       # how SPEC runs a macro file
    "http_timeout_s": 10.0,
}

# Placeholders the reactor provides for macro substitution / named commands:
#   {path} {recipe_id} {temperature} {exposure} {frames}


def make_beamline(cfg: dict | None = None):
    c = dict(_DEFAULTS)
    c.update((cfg or {}).get("spec", {}) if cfg else {})
    return (SpecBeamline(c) if str(c.get("backend", "mock")).lower() == "real"
            else MockBeamline(c))


# ── base ─────────────────────────────────────────────────────────────────────
class BeamlineDriver:
    def __init__(self, cfg: dict):
        self.cfg = dict(_DEFAULTS); self.cfg.update(cfg or {})

    # control
    def take_control(self) -> None: ...
    def set_temperature(self, target_c: float) -> None: ...
    def read_temperature(self):
        return self.read_counters().get(self.cfg["temp_counter"])
    def read_counters(self) -> dict: ...
    def read_bstop(self):
        return self.read_counters().get(self.cfg["bstop_counter"])
    def read_state(self) -> dict:
        """One read → canonical {temperature, bstop, i0} (one HTTP round-trip)."""
        c = self.read_counters()
        return {"temperature": c.get(self.cfg["temp_counter"]),
                "bstop": c.get(self.cfg["bstop_counter"]),
                "i0": c.get(self.cfg["i0_counter"])}
    def set_output(self, path: str) -> None: ...
    def collect(self, **params) -> None:
        """Run a 2D acquisition. ``params`` may include path, recipe_id,
        temperature, exposure, frames — substituted into the macro / command."""
        ...
    def is_busy(self) -> bool: return False
    def wait(self, timeout: float = 600.0) -> None:
        t0 = time.time()
        while self.is_busy() and time.time() - t0 < timeout:
            time.sleep(0.2)
    def close(self) -> None: ...


# ── mock ─────────────────────────────────────────────────────────────────────
class MockBeamline(BeamlineDriver):
    """In-memory beamline: temperature ramps toward the setpoint; counters faked."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._target = 25.0
        self._temp = 25.0
        self._last = time.time()
        self._ramp = float(self.cfg.get("mock_ramp_c_per_s", 1.0))
        self.collections: list[dict] = []

    def _advance(self):
        now = time.time(); dt = now - self._last; self._last = now
        step = self._ramp * dt
        if abs(self._target - self._temp) <= step:
            self._temp = self._target
        else:
            self._temp += step if self._target > self._temp else -step

    def take_control(self): pass

    def set_temperature(self, target_c: float):
        self._advance(); self._target = float(target_c)

    def read_counters(self) -> dict:
        self._advance()
        frac = max(0.0, min(1.0, self._temp / max(self._target, 1e-6)))
        return {self.cfg["temp_counter"]: round(self._temp, 2),
                self.cfg["bstop_counter"]: round(1.0e5 * (1 - 0.3 * frac), 1),
                self.cfg["i0_counter"]: 1.0e6}

    def set_output(self, path: str):
        self._out = path

    def collect(self, **params):
        rec = dict(params); rec["t"] = time.time()
        mf = self.cfg.get("macro_file")
        if mf:
            try:
                rec["rendered"] = Path(mf).read_text().format(**params)   # for inspection/tests
            except Exception:
                pass
        self.collections.append(rec)

    def is_busy(self) -> bool:
        return False


# ── real (SPEC bServer over HTTP) ──────────────────────────────────────────────
class SpecBeamline(BeamlineDriver):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        import requests                              # noqa: PLC0415
        self._requests = requests
        self._base = self.cfg["base_url"]
        self._to = float(self.cfg["http_timeout_s"])

    def _sis(self, command: str, **params):
        r = self._requests.get(self._base + command, params=params, timeout=self._to)
        r.raise_for_status()
        return r.json().get("data")

    def _cmd(self, spec_cmd: str):
        return self._sis("execute_command", spec_cmd=spec_cmd)

    def take_control(self):
        self._sis("get_remote_control")

    def set_temperature(self, target_c: float):
        self._cmd(self.cfg["set_temp_cmd"].format(T=float(target_c)))

    def read_counters(self) -> dict:
        names = self._sis("get_all_counter_mnemonics") or []
        vals = self._sis("get_all_counters") or []
        return dict(zip(names, vals))

    def set_output(self, path: str):
        self._cmd(self.cfg["newfile_cmd"].format(path=path))

    def collect(self, **params):
        p = {"exposure": 1.0, "frames": 1, **params}
        macro_file = self.cfg.get("macro_file")
        if macro_file:
            # macro mode: fill the .txt template, write it, and qdo it in SPEC
            text = Path(macro_file).read_text()
            rendered = text.format(**p)
            out = Path(self.cfg.get("macro_out_file")
                       or (Path(macro_file).parent / "_autopilot_run.mac"))
            out.write_text(rendered)
            self._cmd(self.cfg["qdo_cmd"].format(file=str(out)))
        else:
            # named-command mode: newfile then the collect macro
            if p.get("path"):
                self.set_output(p["path"])
            self._cmd(self.cfg["collect_cmd"].format(**p))
        self.wait()

    def is_busy(self) -> bool:
        return bool(self._sis("is_busy"))
