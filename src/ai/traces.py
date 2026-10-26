"""src/ai/traces.py: one record per assistant answer, kept on disk.

Why: to diagnose a wrong answer you need to know exactly what the model was
shown and what it did, not a summary. Meta-Harness (Lee et al., arXiv
2603.28052) found full traces to be the key ingredient for improving a harness.

Each answer appends one JSON line to logs/assistant_traces/YYYY-MM-DD.jsonl:

    ts, id, duration_s, app_id, user, model, effort, outcome
    question, answer
    prompt_parts   [{part, chars}]          which knowledge was injected, in order
    retrieved      [{collection, source, distance}]   passages from the index
    tools          [{name, input, ok, output_head}]
    usage          token counts per model round (cache read / write)
    error          only if the turn failed

Local only: logs/ is in .gitignore and nothing here is ever sent anywhere.
Credentials are never recorded: the trace holds the question, the answer and
tool inputs, not headers, environment or settings. Turn off with
SWAXS_ASSISTANT_TRACES=0; SWAXS_TRACES_DIR moves the folder (tests).
Tracing must never break a chat: every function swallows its own errors.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "logs" / "assistant_traces"
_local = threading.local()
_write_lock = threading.Lock()
_MAX_TEXT = 20_000          # cap per text field, so one huge answer cannot bloat the log
_MAX_TOOL_OUT = 500


def enabled() -> bool:
    return os.environ.get("SWAXS_ASSISTANT_TRACES", "1").strip().lower() not in ("0", "false", "off", "no")


def traces_dir() -> Path:
    return Path(os.environ.get("SWAXS_TRACES_DIR") or _DEFAULT_DIR)


def _cut(s, n=_MAX_TEXT):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"


def start(**meta) -> None:
    """Begin a trace for the current thread (one chat turn)."""
    try:
        _local.rec = {"id": uuid.uuid4().hex[:12], "_t0": time.monotonic(),
                      "prompt_parts": [], "retrieved": [], **meta}
    except Exception:                                    # noqa: BLE001
        _local.rec = None


def _rec():
    return getattr(_local, "rec", None)


def part(name: str, text: str | None) -> None:
    """Record that a knowledge block was put into the prompt."""
    r = _rec()
    if r is not None and text:
        r["prompt_parts"].append({"part": name, "chars": len(text)})


def retrieved(hits) -> None:
    r = _rec()
    if r is None:
        return
    try:
        for h in hits or []:
            r["retrieved"].append({"collection": h.get("collection"),
                                   "source": h.get("source"),
                                   "distance": h.get("distance")})
    except Exception:                                    # noqa: BLE001
        pass


def note(key: str, value) -> None:
    r = _rec()
    if r is not None:
        r[key] = value


KEEP_DAYS = 90
_pruned_on: dict = {}


def keep_days() -> int:
    try:
        return max(1, int(os.environ.get("SWAXS_TRACES_KEEP_DAYS", KEEP_DAYS)))
    except ValueError:
        return KEEP_DAYS


def prune(d: Path | None = None, days: int | None = None, today: datetime | None = None) -> int:
    """Remove dated trace files (YYYY-MM-DD.jsonl) older than ``days`` (audit
    AN9; SWAXS_TRACES_KEEP_DAYS, default 90). Other files are left alone.
    Returns how many were removed. Never raises."""
    try:
        d = Path(d or traces_dir())
        days = keep_days() if days is None else int(days)
        cutoff = (today or datetime.now()).date().toordinal() - days
        n = 0
        for p in d.glob("????-??-??.jsonl"):
            try:
                if datetime.strptime(p.stem, "%Y-%m-%d").date().toordinal() < cutoff:
                    p.unlink()
                    n += 1
            except (ValueError, OSError):
                pass
        return n
    except Exception:                                    # noqa: BLE001
        return 0


def _maybe_prune(d: Path) -> None:
    day = datetime.now().strftime("%Y-%m-%d")
    if _pruned_on.get(str(d)) != day:                     # at most once a day per folder
        _pruned_on[str(d)] = day
        prune(d)


def record(kind: str, **fields) -> Path | None:
    """Write one standalone trace line (not a chat turn), e.g. kind="fitter"
    for each structure-fitter decision. Same folder, same rules, never raises."""
    if not enabled():
        return None
    try:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "id": uuid.uuid4().hex[:12], "kind": kind,
               **{k: (_cut(v) if isinstance(v, str) else v) for k, v in fields.items()}}
        d = traces_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        with _write_lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
        _maybe_prune(d)
        return path
    except Exception as exc:                             # noqa: BLE001
        logger.debug("[traces] could not write record: %s", exc)
        return None


def finish(result: dict | None = None, error: str | None = None) -> Path | None:
    """Write the trace for this turn. Returns the file written (or None)."""
    r = _rec()
    _local.rec = None
    if r is None or not enabled():
        return None
    try:
        result = result or {}
        tools = []
        for t in result.get("tool_calls") or []:
            out = t.get("output")
            out_s = out if isinstance(out, str) else json.dumps(out, default=str)
            low = (out_s or "")[:200].lower()
            tools.append({"name": t.get("name"),
                          "input": t.get("input"),
                          "ok": not (low.startswith("error") or "traceback" in low),
                          "output_head": _cut(out_s, _MAX_TOOL_OUT)})
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "id": r.pop("id"),
            "duration_s": round(time.monotonic() - r.pop("_t0"), 2),
            **{k: v for k, v in r.items() if k not in ("question",)},
            "question": _cut(r.get("question")),
            "answer": _cut(result.get("text")),
            "tools": tools,
            "usage": result.get("usage"),
            "n_plots": len(result.get("plots") or []),
        }
        rec.setdefault("outcome", "error" if error else "answered")
        if error:
            rec["error"] = _cut(error, 2000)
        d = traces_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with _write_lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _maybe_prune(d)
        return path
    except Exception as exc:                             # noqa: BLE001
        logger.debug("[traces] could not write trace: %s", exc)
        return None
