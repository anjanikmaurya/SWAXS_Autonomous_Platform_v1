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

HARD SPEC GUARD
---------------
A single lock serializes every SPEC interaction, and ``collect`` holds it for the
entire acquisition. So while an X-ray collection is running:
  • no other SPEC command (temperature, a second collect) can be sent — they
    block until it finishes, so SPEC is never interrupted mid-macro;
  • live reads (``read_state``) skip instead of blocking, so the reactor control
    loop / emergency-stop stay fully responsive.
This is what lets Stop / E-stop act on the PUMPS ONLY and leave the collection to
finish on its own.

Site-specific SPEC strings (temperature counter, collect / newfile macros) come
from config. Defaults are marked ⚠ CONFIRM — verify them on the beamline.
"""

from __future__ import annotations

import threading
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
    "macro_file": "",                               # collection macro template (.txt); blank = named-cmd mode
    "macro_out_file": "",                           # where the filled macro is written (SPEC-readable)
    "qdo_cmd": 'qdo "{file}"',                       # how SPEC runs the filled macro file
    "newfile_cmd": "newfile {path}",                # sets save dir + prefix (named-cmd mode)
    "collect_cmd": "ct {exposure}",                 # 2D acquisition (named-cmd mode)
    "read_during_collect": False,                   # True → keep polling counters DURING a collection
    "http_timeout_s": 10.0,
}

# Placeholders the reactor provides: {path} {recipe_id} {temperature} {exposure} {frames}


def make_beamline(cfg: dict | None = None):
    c = dict(_DEFAULTS)
    c.update((cfg or {}).get("spec", {}) if cfg else {})
    return (SpecBeamline(c) if str(c.get("backend", "mock")).lower() == "real"
            else MockBeamline(c))


# ── base (owns the hard guard; subclasses implement the _do_* primitives) ──────
class BeamlineDriver:
    def __init__(self, cfg: dict):
        self.cfg = dict(_DEFAULTS); self.cfg.update(cfg or {})
        self._lock = threading.RLock()
        self._collecting = False

    # -- guarded public API ---------------------------------------------------
    def take_control(self) -> None:
        with self._lock:
            self._do_take_control()

    def set_temperature(self, target_c: float) -> None:
        # blocks until any in-progress collection finishes (never interrupts SPEC)
        with self._lock:
            self._do_set_temperature(float(target_c))

    def collect(self, **params) -> None:
        """Run a 2D acquisition, holding the SPEC lock for the whole thing."""
        with self._lock:
            self._collecting = True
            try:
                self._do_collect(**params)
            finally:
                self._collecting = False

    def read_state(self) -> dict:
        """Canonical {temperature, bstop, i0}. Non-blocking: normally returns {} if
        a SPEC op (e.g. a collection) holds the lock, so the control loop never
        stalls. If ``read_during_collect`` is set and a collection is running, it
        reads concurrently instead (only enable if the bServer allows counter
        reads during a scan) so the live chart stays live through the acquisition."""
        if self._collecting and self.cfg.get("read_during_collect"):
            try:
                return self._do_read_state()
            except Exception:
                return {}
        if not self._lock.acquire(blocking=False):
            return {}
        try:
            return self._do_read_state()
        finally:
            self._lock.release()

    def read_counters(self) -> dict:
        if self._collecting and self.cfg.get("read_during_collect"):
            try:
                return self._do_read_counters()
            except Exception:
                return {}
        if not self._lock.acquire(blocking=False):
            return {}
        try:
            return self._do_read_counters()
        finally:
            self._lock.release()

    def read_temperature(self):
        return self.read_state().get("temperature")

    def read_bstop(self):
        return self.read_state().get("bstop")

    def is_collecting(self) -> bool:
        return self._collecting

    def close(self) -> None:
        self._do_close()

    # -- primitives (override) ------------------------------------------------
    def _do_take_control(self) -> None: ...
    def _do_set_temperature(self, target_c: float) -> None: ...
    def _do_read_counters(self) -> dict: return {}
    def _do_read_state(self) -> dict:
        c = self._do_read_counters()
        return {"temperature": c.get(self.cfg["temp_counter"]),
                "bstop": c.get(self.cfg["bstop_counter"]),
                "i0": c.get(self.cfg["i0_counter"])}
    def _do_collect(self, **params) -> None: ...
    def _do_is_busy(self) -> bool: return False
    def _do_close(self) -> None: ...

    def _wait(self, timeout: float = 600.0) -> None:
        t0 = time.time()
        while self._do_is_busy() and time.time() - t0 < timeout:
            time.sleep(0.2)


# ── mock ─────────────────────────────────────────────────────────────────────
class MockBeamline(BeamlineDriver):
    """In-memory beamline: temperature ramps toward the setpoint; counters faked."""

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._target = 25.0
        self._temp = 25.0
        self._last = time.time()
        self._ramp = float(self.cfg.get("mock_ramp_c_per_s", 5.0))
        self._collect_s = float(self.cfg.get("mock_collect_s", 0.0))   # simulate acquisition time
        self.collections: list[dict] = []

    def _advance(self):
        now = time.time(); dt = now - self._last; self._last = now
        step = self._ramp * dt
        if abs(self._target - self._temp) <= step:
            self._temp = self._target
        else:
            self._temp += step if self._target > self._temp else -step

    def _do_take_control(self): pass

    def _do_set_temperature(self, target_c: float):
        self._advance(); self._target = float(target_c)

    def _do_read_counters(self) -> dict:
        self._advance()
        frac = max(0.0, min(1.0, self._temp / max(self._target, 1e-6)))
        return {self.cfg["temp_counter"]: round(self._temp, 2),
                self.cfg["bstop_counter"]: round(1.0e5 * (1 - 0.3 * frac), 1),
                self.cfg["i0_counter"]: 1.0e6}

    def _do_collect(self, **params):
        rec = dict(params); rec["t"] = time.time()
        mf = self.cfg.get("macro_file")
        if mf:
            try:
                rec["rendered"] = Path(mf).read_text().format(**params)   # for inspection/tests
            except Exception:
                pass
        if self._collect_s:
            time.sleep(self._collect_s)
        self.collections.append(rec)


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

    def _do_take_control(self):
        self._sis("get_remote_control")

    def _do_set_temperature(self, target_c: float):
        self._cmd(self.cfg["set_temp_cmd"].format(T=float(target_c)))

    def _do_read_counters(self) -> dict:
        names = self._sis("get_all_counter_mnemonics") or []
        vals = self._sis("get_all_counters") or []
        return dict(zip(names, vals))

    def _do_collect(self, **params):
        p = {"exposure": 1.0, "frames": 1, **params}
        self._do_take_control()
        macro_file = self.cfg.get("macro_file")
        if macro_file:
            # macro mode: fill the .txt template, write it, and qdo it in SPEC
            rendered = Path(macro_file).read_text().format(**p)
            out = Path(self.cfg.get("macro_out_file")
                       or (Path(macro_file).parent / "_autopilot_run.mac"))
            out.write_text(rendered)
            self._cmd(self.cfg["qdo_cmd"].format(file=str(out)))
        else:
            # named-command mode: newfile then the collect command
            if p.get("path"):
                self._cmd(self.cfg["newfile_cmd"].format(path=p["path"]))
            self._cmd(self.cfg["collect_cmd"].format(**p))
        self._wait()

    def _do_is_busy(self) -> bool:
        return bool(self._sis("is_busy"))
