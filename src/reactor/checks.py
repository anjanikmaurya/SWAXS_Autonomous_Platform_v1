"""
src/reactor/checks.py — the "Run all checks" list on the Hardware test tab.

READ ONLY. Every check looks at the latest readings the control loop already
holds (``ReactorController.status()``) and the config; nothing here sends a
command to a pump or to SPEC. That is deliberate: a pre-run check must never be
the thing that moves hardware or opens the shutter.

Each check returns a ``TestResult`` from the instrument contract
(src/synthesis/types.py): pass/fail, what was seen, what was expected, and what
to do when it fails. In Simulation mode the checks run against the simulated
rig and say so.
"""
from __future__ import annotations

from typing import Callable, Dict

from src.synthesis.types import TestResult, TestSpec

CHECKS = (
    TestSpec("pumps_present", "All configured pumps connected"),
    TestSpec("pumps_healthy", "Every pump answers, no fault"),
    TestSpec("pressure", "Chamber pressure within each pump's ceiling"),
    TestSpec("temperature", "Reactor temperature is a live reading"),
    TestSpec("beamline", "Beamline readings arriving (I₀, bstop)"),
)


def _sim(status: dict) -> bool:
    return status.get("backend") != "real"


def _expected_pumps(cfg: dict) -> tuple[list, list]:
    """The pumps the bank opens: the reagents plus the selected flush pump, minus
    any with ``enabled: false`` (same rule as PumpBank). Returns (expected, unused)."""
    from src.reactor.config import FLUSH_PUMP, REAGENT_PUMPS       # constants only
    pumps_cfg = cfg.get("pumps") or {}
    active = set(REAGENT_PUMPS) | {str((cfg.get("flush") or {}).get("pump", FLUSH_PUMP))}
    want = sorted(n for n, pc in pumps_cfg.items()
                  if n in active and (pc or {}).get("enabled", True) is not False)
    unused = sorted(n for n in pumps_cfg if n not in want)
    return want, unused


def _pumps_present(status: dict, cfg: dict) -> TestResult:
    live_fp = (status.get("run_settings") or {}).get("flush_pump")
    if live_fp:                                   # the flush pump chosen in the app wins
        cfg = {**cfg, "flush": {**(cfg.get("flush") or {}), "pump": live_fp}}
    want, unused = _expected_pumps(cfg)
    have = sorted((status.get("pumps") or {}).keys())
    missing = [n for n in want if n not in have]
    if missing:
        return TestResult(False, value=f"missing: {', '.join(missing)}", expected=f"{len(want)} pumps",
                          hint="Check the pump USB/serial cables and power, and close the Dolomite GUI "
                               "(it holds the ports). Then switch Simulation → Hardware again.")
    note = f"; not used now: {', '.join(unused)}" if unused else ""
    return TestResult(True, value=f"{len(want)}/{len(want)}" + (" (simulated)" if _sim(status) else "") + note,
                      expected=f"{len(want)} pumps")


def _pumps_healthy(status: dict, cfg: dict) -> TestResult:
    bad = []
    for n, p in (status.get("pumps") or {}).items():
        if p.get("fault"):
            bad.append(f"{n} fault")
        elif p.get("stale"):
            bad.append(f"{n} not answering")
    if bad:
        return TestResult(False, value=", ".join(bad), expected="no faults",
                          hint="A faulted pump: clear it on the pump or press Reset. A pump not "
                               "answering: check its cable; a pump held in local/manual mode ignores commands.")
    return TestResult(True, value="all answering" + (" (simulated)" if _sim(status) else ""),
                      expected="no faults")


def _pressure(status: dict, cfg: dict) -> TestResult:
    over, unread = [], []
    for n, p in (status.get("pumps") or {}).items():
        pr, pmax = p.get("pressure"), p.get("max_pressure") or 0
        if not isinstance(pr, (int, float)):
            unread.append(n)
        elif pmax and pr > pmax:
            over.append(f"{n} {pr:.0f} > {pmax:.0f} mbar")
    if over or unread:
        parts = over + [f"{n} no reading" for n in unread]
        return TestResult(False, value=", ".join(parts), expected="below each ceiling",
                          hint="High pressure: look for a blockage downstream (capillary, mixer). "
                               "No reading: tare the pressure sensor (step 1) or check the supply line.")
    peak = max(((n, p.get("pressure", 0.0)) for n, p in (status.get("pumps") or {}).items()),
               key=lambda t: t[1], default=("", 0.0))
    return TestResult(True, value=f"max {peak[1]:.0f} mbar ({peak[0]})" if peak[0] else "ok",
                      expected="below each ceiling")


def _temperature(status: dict, cfg: dict) -> TestResult:
    t = status.get("temperature") or {}
    src, cur = t.get("source"), t.get("current")
    if src == "mock":
        return TestResult(True, value=f"{cur} °C (simulated)", expected="live reading",
                          hint="Simulation only. In Hardware mode this must be a live beamline reading.")
    if src != "beamline":
        return TestResult(False, value="not a measurement (no source wired)", expected="live reading",
                          hint="No temperature source is wired, so the over-temperature interlock cannot "
                               "fire. Do not use temperature arming; check the SPEC/EPICS temperature channel.")
    if t.get("stale"):
        age = t.get("age_s")
        return TestResult(False, value=f"stale{f' ({age:.0f} s old)' if isinstance(age, (int, float)) else ''}",
                          expected="live reading",
                          hint="No fresh temperature read: check that SPEC (bServer) is running and reachable.")
    return TestResult(True, value=f"{cur} °C, live", expected="live reading")


def _beamline(status: dict, cfg: dict) -> TestResult:
    t = status.get("temperature") or {}
    i0, bs = t.get("i0"), t.get("bstop")
    # I₀/bstop are the last cached values: when SPEC stops answering they stay
    # put, so a stale reading must fail rather than show green.
    if t.get("source") == "beamline" and t.get("stale"):
        return TestResult(False, value="readings are stale", expected="fresh readings",
                          hint="No fresh beamline read: check that SPEC (bServer) is running and "
                               "reachable from this computer.")
    missing = [k for k, v in (("I₀", i0), ("bstop", bs)) if not isinstance(v, (int, float))]
    if missing:
        return TestResult(False, value=f"no {' / '.join(missing)}", expected="numbers > 0",
                          hint="Check SPEC is running, the counters are named as in reactor/config.yml, "
                               "and the beamline is not in local control.")
    if i0 <= 0:
        return TestResult(False, value=f"I₀ {i0:g}", expected="I₀ > 0",
                          hint="No incident beam: check the shutter, the ring status and the I₀ counter.")
    return TestResult(True, value=f"I₀ {i0:.4g}, bstop {bs:.4g}" + (" (simulated)" if _sim(status) else ""),
                      expected="numbers > 0")


_RUN: Dict[str, Callable[[dict, dict], TestResult]] = {
    "pumps_present": _pumps_present, "pumps_healthy": _pumps_healthy, "pressure": _pressure,
    "temperature": _temperature, "beamline": _beamline,
}


def run_check(check_id: str, status: dict, cfg: dict) -> TestResult:
    fn = _RUN.get(check_id)
    if fn is None:                                # an unknown id is the caller's error
        raise KeyError(f"no check {check_id!r}; available: {sorted(_RUN)}")
    try:
        return fn(status, cfg)
    except Exception as exc:                      # a broken check reports, never raises
        return TestResult(False, hint=f"the check itself failed: {exc!r}")


def run_all(status: dict, cfg: dict) -> list[dict]:
    out = []
    for spec in CHECKS:
        r = run_check(spec.id, status, cfg)
        out.append({"id": spec.id, "title": spec.title, "ok": r.ok, "value": r.value,
                    "expected": r.expected, "hint": "" if r.ok else r.hint,
                    "note": r.hint if r.ok else ""})
    return out
