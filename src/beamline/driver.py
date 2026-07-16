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
    "temp_counter": "CTEMP",                        # counter reporting temperature (BL1-5)
    "bstop_counter": "bstop",
    "i0_counter": "i0",
    "read_refresh_cmd": "",                          # SPEC cmd run before a read to refresh counters
                                                    #   (get_all_counters is otherwise the LAST count).
                                                    #   e.g. "ct 0.1"  ⚠ may open the shutter — prefer a
                                                    #   shutter-free temperature-query macro if you have one.
    "set_temp_cmd": "csettemp {T}",                 # ramp command (from MSD.py)
    "open_shutter_cmd": "sopen",                    # open the fast shutter (from MSD.py)
    "close_shutter_cmd": "sclose",                  # close the fast shutter (from MSD.py)
    "macro_file": "",                               # collection macro template (.txt); blank = named-cmd mode
    "collect_mode": "commands",                     # "commands" (stream macro lines via bServer — no shared FS) | "qdo" (write file + qdo) | "named"
    "macro_out_file": "",                           # where the filled macro is written (SPEC-readable) — qdo mode only
    "qdo_cmd": 'qdo "{file}"',                       # how SPEC runs the filled macro file (qdo mode)
    "newfile_cmd": "newfile {path}",                # sets save dir + prefix (named-cmd mode)
    "collect_cmd": "ct {exposure}",                 # 2D acquisition (named-cmd mode)
    "read_during_collect": False,                   # True → keep polling counters DURING a collection
    "cmd_wait_s": 600.0,                             # max wait for SPEC-not-busy between streamed macro lines
    "http_timeout_s": 10.0,
}

# Placeholders the reactor provides for a macro template, as {{token}} markers
# (double braces so real SPEC syntax — %s, if/for { } blocks — passes untouched):
#   {{sample}} {{main_folder}} {{path}} {{recipe_id}} {{role}} {{temperature}}
#   {{exposure}} {{frames}}
_MACRO_KEYS = ("sample", "main_folder", "path", "recipe_id", "role",
               "temperature", "exposure", "frames")


def render_macro(text: str, params: dict) -> str:
    """Fill a SPEC macro template by replacing {{token}} markers only. Uses plain
    string replacement (NOT str.format/%), so SPEC's own %s and { } blocks are
    left exactly as written."""
    out = text
    for k in _MACRO_KEYS:
        if k in params and params[k] is not None:
            out = out.replace("{{" + k + "}}", str(params[k]))
    return out


def _strip_inline_comment(s: str) -> str:
    """Cut a trailing SPEC #-comment, ignoring # inside quotes."""
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
        elif ch == "#":
            return s[:i].strip()
    return s.strip()


def macro_command_lines(text: str) -> list[str]:
    """Split a rendered SPEC macro into individual commands to send one-by-one via
    ``execute_command`` (the "commands" collect mode). Drops blank lines and
    #-comments, strips inline comments, preserves order. Each remaining statement
    is one SPEC command — the same lines ``qdo`` would run, just streamed over the
    bServer so no file needs to live on a SPEC-shared path."""
    out = []
    for raw in text.splitlines():
        s = _strip_inline_comment(raw.strip())
        if s:
            out.append(s)
    return out


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

    def open_shutter(self) -> None:
        # guarded: waits for any in-progress collection to finish first
        with self._lock:
            self._do_open_shutter()

    def close_shutter(self) -> None:
        with self._lock:
            self._do_close_shutter()

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
    def _do_open_shutter(self) -> None: ...
    def _do_close_shutter(self) -> None: ...
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
        self.shutter = "closed"

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

    def _do_open_shutter(self): self.shutter = "open"
    def _do_close_shutter(self): self.shutter = "closed"

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
                rec["rendered"] = render_macro(Path(mf).read_text(), params)   # for inspection/tests
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
        self._cmd_wait_s = float(self.cfg.get("cmd_wait_s", 600.0))
        self._have_control = False

    def _sis(self, command: str, **params):
        r = self._requests.get(self._base + command, params=params, timeout=self._to)
        r.raise_for_status()
        return r.json().get("data")

    def _ensure_control(self):
        # The bServer only runs execute_command while we hold remote control
        # (see main.py handleGET_test: get_remote_control before execute_command).
        # Acquire once, then remember — otherwise commands (ct, csettemp, qdo) are
        # silently ignored and counters never refresh.
        if not self._have_control:
            self._sis("get_remote_control")
            self._have_control = True

    def _cmd(self, spec_cmd: str):
        self._ensure_control()
        return self._sis("execute_command", spec_cmd=spec_cmd)

    def _do_take_control(self):
        self._sis("get_remote_control")
        self._have_control = True

    def _do_close(self):
        try:
            if self._have_control:
                self._sis("release_remote_control")
                self._have_control = False
        except Exception:
            pass

    def _do_set_temperature(self, target_c: float):
        self._cmd(self.cfg["set_temp_cmd"].format(T=float(target_c)))

    def _do_open_shutter(self):
        self._cmd(self.cfg["open_shutter_cmd"]); self._wait(timeout=30.0)

    def _do_close_shutter(self):
        self._cmd(self.cfg["close_shutter_cmd"]); self._wait(timeout=30.0)

    def _do_read_counters(self) -> dict:
        # get_all_counters returns the values from SPEC's LAST count, so they're
        # stale until something counts. Optionally run a refresh command first to
        # update them (skipped during a collection — never count mid-acquisition).
        refresh = self.cfg.get("read_refresh_cmd")
        if refresh and not self._collecting:
            try:
                self._cmd(str(refresh))
                self._wait(timeout=30.0)   # let the count finish before reading (cf. MSD.execute_and_read_count)
            except Exception:
                pass
        names = self._sis("get_all_counter_mnemonics") or []
        vals = self._sis("get_all_counters") or []
        return dict(zip(names, vals))

    def _do_collect(self, **params):
        p = {"exposure": 1.0, "frames": 1, **params}
        self._do_take_control()
        macro_file = self.cfg.get("macro_file")
        mode = str(self.cfg.get("collect_mode", "commands")).lower()
        if macro_file and mode == "qdo":
            # qdo mode: fill the .txt template, write it, qdo it.
            # NEEDS the filled file to sit on a path SPEC can open (shared mount).
            rendered = render_macro(Path(macro_file).read_text(), p)
            out = Path(self.cfg.get("macro_out_file")
                       or (Path(macro_file).parent / "_autopilot_run.mac"))
            out.write_text(rendered)
            self._cmd(self.cfg["qdo_cmd"].format(file=str(out)))
        elif macro_file:
            # commands mode (default): stream the rendered macro to SPEC line-by-line.
            # No file is written anywhere — works even if SPEC is a different host,
            # because SPEC does its own file I/O via the paths inside the macro.
            # NOTE: use a FLAT macro (inlined values, plain action commands) — SPEC
            # variable assignments / eval(sprintf) don't run reliably through the
            # interactive execute_command path. Wait for SPEC between lines so each
            # command finishes before the next (mirrors MSD.wait_until_SPECfinished).
            for line in macro_command_lines(render_macro(Path(macro_file).read_text(), p)):
                self._cmd(line)
                self._wait(timeout=self._cmd_wait_s)
        else:
            # named-command mode: newfile then the collect command
            if p.get("path"):
                self._cmd(self.cfg["newfile_cmd"].format(path=p["path"]))
            self._cmd(self.cfg["collect_cmd"].format(**p))
        self._wait()

    def _do_is_busy(self) -> bool:
        return bool(self._sis("is_busy"))
