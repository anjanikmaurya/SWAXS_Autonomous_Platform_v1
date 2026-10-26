"""
src/fitting/ai_guard.py: a circuit breaker and call budget for the AI fitter.

The AI fitter (AI-assisted structure) runs unattended for days. Three things
can go wrong, and each must cost little and stop by itself:

* **Broken**: no login, the gateway down, timeouts. Every call fails.
* **Stuck or slow**: a call hangs or the fit takes far too long.
* **Hallucinating**: the answer is unreadable, names shapes that are not in
  the catalogue, or is otherwise unusable (``fallback_invalid_answer``).

The fitter already never trusts the AI: only catalogue models are kept, every
candidate is fitted by physics and BIC picks the winner, and any failure falls
back to Catalogue + BIC. This guard adds the operational safety on top:

* after ``max_failures`` failed AI fits IN A ROW (any of the three cases), the
  AI fitter PAUSES for ``cooldown_s`` (default 3 failures, 30 min): every
  profile gets the Catalogue + BIC fit only, no AI call is made, no token is
  spent. After the pause one trial fit is allowed; one more failure pauses again.
* at most ``max_calls_per_hour`` AI calls (default 60): above that the AI is
  skipped until the hour rolls over, whatever the cause (a backlog, a loop).
* a fit slower than ``slow_s`` (default 150 s) counts as a failure.

Every change of state is returned once, so the app logs it once, not every 3 s.
Thread-safe; state is in memory (a restart starts fresh, which is the safe
direction: at worst one more trial call).
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

FAIL_STATUSES = ("fallback_ai_error", "fallback_invalid_answer", "fallback_no_ai")


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


class AIGuard:
    def __init__(self, max_failures: int | None = None, cooldown_s: float | None = None,
                 max_calls_per_hour: int | None = None, slow_s: float | None = None,
                 clock=time.time):
        self.max_failures = int(max_failures if max_failures is not None
                                else _env("SWAXS_FITTER_AI_MAX_FAILURES", 3))
        self.cooldown_s = float(cooldown_s if cooldown_s is not None
                                else _env("SWAXS_FITTER_AI_COOLDOWN_S", 1800))
        self.max_calls_per_hour = int(max_calls_per_hour if max_calls_per_hour is not None
                                      else _env("SWAXS_FITTER_AI_MAX_PER_HOUR", 60))
        self.slow_s = float(slow_s if slow_s is not None else _env("SWAXS_FITTER_AI_SLOW_S", 150))
        self._clock = clock
        self._lock = threading.Lock()
        self._fails = 0
        self._paused_until = 0.0
        self._why = ""
        self._calls: deque = deque()
        self._last_state = "ok"

    # ── decisions ────────────────────────────────────────────────────────────
    def allow(self) -> tuple[bool, str]:
        """(may the AI be called now?, reason if not)."""
        now = self._clock()
        with self._lock:
            while self._calls and now - self._calls[0] > 3600:
                self._calls.popleft()
            if now < self._paused_until:
                return False, self._why
            if len(self._calls) >= self.max_calls_per_hour:
                return False, (f"the hourly limit of {self.max_calls_per_hour} AI calls is reached "
                               "(SWAXS_FITTER_AI_MAX_PER_HOUR)")
            return True, ""

    def record(self, info: dict | None, seconds: float | None = None) -> None:
        """Feed one AI fit's outcome (res["llm_structure"]) and its duration."""
        info = info or {}
        now = self._clock()
        status = str(info.get("status") or "")
        with self._lock:
            if not info.get("cached"):
                for _ in range(int(info.get("rounds") or 0)):
                    self._calls.append(now)
            failed = status in FAIL_STATUSES or (seconds is not None and seconds > self.slow_s)
            if not failed:
                self._fails = 0
                return
            self._fails += 1
            if self._fails >= self.max_failures:
                kind = ("no AI login" if status == "fallback_no_ai" else
                        "unusable answers (off-catalogue or unreadable)" if status == "fallback_invalid_answer" else
                        f"too slow (over {self.slow_s:.0f} s)" if (seconds or 0) > self.slow_s else
                        "AI errors or timeouts")
                self._why = (f"{self._fails} AI fits in a row failed ({kind}); the AI fitter is paused "
                             f"for {self.cooldown_s / 60:.0f} min and Catalogue + BIC fits alone")
                self._paused_until = now + self.cooldown_s
                self._fails = self.max_failures - 1     # after the pause: one trial, then pause again

    def state_change(self) -> str | None:
        """'paused: why' or 'resumed' once per change; None otherwise."""
        ok, why = self.allow()
        state = "ok" if ok else why
        with self._lock:
            if state == self._last_state:
                return None
            self._last_state = state
        return "resumed" if ok else f"paused: {why}"

    def status(self) -> dict:
        ok, why = self.allow()
        with self._lock:
            return {"allowed": ok, "why": why, "failures_in_a_row": self._fails,
                    "calls_last_hour": len(self._calls), "max_calls_per_hour": self.max_calls_per_hour,
                    "paused_until": self._paused_until if not ok and self._paused_until > self._clock() else None}

    def reset(self) -> None:
        with self._lock:
            self._fails = 0
            self._paused_until = 0.0
            self._why = ""
