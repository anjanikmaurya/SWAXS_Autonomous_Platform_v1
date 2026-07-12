"""
src/ai/memory.py — 3-Layer AI Memory
======================================
Provides persistent, layered memory for the SWAXS AI assistant.

Layer 1 — User layer  (~/.swaxs/memory/users/<user_id>/)
    Corrections, preferences, and session summaries personal to each user.
    Persists across projects and machines (if home dir is synced).

Layer 2 — Project layer  (<project_root>/.swaxs/memory/)
    Per-experiment processing history, quality logs, and context.
    Travels with the data.

Layer 3 — Facility layer  (ai_knowledge/beamline/<beamline_id>.yml)
    Instrument-specific know-how, detector quirks, calibration notes.
    Shared across all users at the same facility.

Usage
-----
    from src.ai.memory import LayeredMemory

    mem = LayeredMemory(
        ai_knowledge_dir = "/abs/path/ai_knowledge",
        user_id          = "albert",
    )

    # Load full context for the Claude API system prompt
    ctx = mem.load_context(project_root="/abs/path/experiment",
                           beamline_id="ssrl_1-5")

    # Save a user correction
    mem.save_correction(turn=3,
                        original="Rg = 4.2 nm",
                        corrected="Rg = 3.1 nm — wrong range was used")

    # Update user context (sample details for this session)
    mem.update_user_context(sample_type="protein",
                            expected_Rg_nm=3.5,
                            background="20 mM HEPES pH 7.4")
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("swaxs_platform")

_USER_ROOT_DIR = Path.home() / ".swaxs" / "memory" / "users"


class LayeredMemory:
    """
    Three-layer memory for the SWAXS AI assistant.

    Parameters
    ----------
    ai_knowledge_dir : str | Path
        Root of the ``ai_knowledge/`` folder (facility layer lives here).
    user_id : str
        Identifier for the current user (e.g. OS username or login name).
        If blank, falls back to "default".
    """

    def __init__(
        self,
        ai_knowledge_dir: str | Path,
        user_id: str = "default",
    ) -> None:
        self._kb_dir  = Path(ai_knowledge_dir)
        self._user_id = user_id or "default"

        # ── Layer 1: user dir ────────────────────────────────────────────────
        self._user_dir = _USER_ROOT_DIR / self._user_id
        self._user_dir.mkdir(parents=True, exist_ok=True)

        self._corrections_path    = self._user_dir / "corrections.jsonl"
        self._preferences_path    = self._user_dir / "preferences.yml"
        self._session_summary_dir = self._user_dir / "session_summaries"
        self._session_summary_dir.mkdir(exist_ok=True)

        # ── Layer 3: facility dir ────────────────────────────────────────────
        self._beamline_dir = self._kb_dir / "beamline"
        self._beamline_dir.mkdir(parents=True, exist_ok=True)

        # ── Group layer: shared SOPs / conventions (cross-project, cross-user) ─
        self._group_dir = self._kb_dir / "group"
        self._group_dir.mkdir(parents=True, exist_ok=True)
        self._group_sops_path = self._group_dir / "sops.json"

    # ── Context assembly ───────────────────────────────────────────────────────

    def load_context(
        self,
        project_root:  str | Path | None = None,
        beamline_id:   str | None = None,
        max_corrections: int = 10,
        max_summaries:   int = 3,
    ) -> dict:
        """
        Assemble the layered memory context dict for the Claude system prompt.

        Returns
        -------
        dict with keys:
            user_preferences    : dict
            recent_corrections  : list[dict]
            user_context        : dict   (sample_type, expected_Rg, etc.)
            session_summaries   : list[str]
            experiment_history  : list[dict]   (from project layer)
            quality_log         : list[dict]   (from project layer)
            beamline_notes      : str          (from facility layer)
        """
        ctx: dict[str, Any] = {
            "user_preferences":   self._load_preferences(),
            "recent_corrections": self._load_corrections(max_corrections),
            "user_context":       self._load_user_context(),
            "session_summaries":  self._load_recent_summaries(max_summaries),
            "group_sops":         self.load_group_sops(),
            "experiment_history": [],
            "quality_log":        [],
            "beamline_notes":     "",
        }

        # Layer 2: project
        if project_root:
            ctx["experiment_history"] = self._load_project_history(project_root)
            ctx["quality_log"]        = self._load_quality_log(project_root)

        # Layer 3: facility
        if beamline_id:
            ctx["beamline_notes"] = self._load_beamline_notes(beamline_id)

        return ctx

    def format_for_prompt(self, ctx: dict) -> str:
        """
        Convert the context dict returned by :meth:`load_context` into a
        compact plain-text block suitable for inclusion in the Claude
        system prompt.
        """
        parts: list[str] = []

        if ctx.get("group_sops"):
            lines = ["## Group SOPs & Conventions (apply unless the user overrides)"]
            for s in ctx["group_sops"]:
                lines.append(f"  • {s.get('title','')}: {s.get('text','')}")
            parts.append("\n".join(lines))

        if ctx.get("beamline_notes"):
            parts.append("## Beamline / Instrument Notes\n" +
                         ctx["beamline_notes"])

        if ctx.get("user_context"):
            lines = ["## Current Sample Context"]
            for k, v in ctx["user_context"].items():
                lines.append(f"  {k}: {v}")
            parts.append("\n".join(lines))

        if ctx.get("user_preferences"):
            prefs = ctx["user_preferences"]
            plines = ["## User Preferences"]
            for k, v in prefs.items():
                plines.append(f"  {k}: {v}")
            parts.append("\n".join(plines))

        if ctx.get("recent_corrections"):
            lines = ["## Past User Corrections (most recent first)"]
            for c in ctx["recent_corrections"]:
                lines.append(
                    f"  Original: {c.get('original', '')}\n"
                    f"  Corrected: {c.get('corrected', '')}"
                )
            parts.append("\n".join(lines))

        if ctx.get("session_summaries"):
            parts.append(
                "## Recent Session Summaries\n" +
                "\n---\n".join(ctx["session_summaries"])
            )

        if ctx.get("experiment_history"):
            lines = [f"## Experiment History (last {len(ctx['experiment_history'])} events)"]
            for ev in ctx["experiment_history"][:5]:
                lines.append(f"  {ev.get('ts','')} — {ev.get('action','')} — {ev.get('detail','')}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)

    # ── Group SOPs / conventions (shared layer) ─────────────────────────────────

    def load_group_sops(self) -> list[dict]:
        """Return the shared group SOPs/conventions (list of {id,title,text,added})."""
        if not self._group_sops_path.exists():
            return []
        try:
            data = json.loads(self._group_sops_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("[Memory] Could not read group SOPs: %s", exc)
            return []

    def add_group_sop(self, title: str, text: str) -> dict:
        """Add a shared group SOP/convention. Returns the new entry."""
        sops = self.load_group_sops()
        entry = {
            "id":    uuid.uuid4().hex[:8],
            "title": (title or "untitled").strip(),
            "text":  (text or "").strip(),
            "added": _now(),
        }
        sops.append(entry)
        self._save_group_sops(sops)
        return entry

    def remove_group_sop(self, ident: str) -> bool:
        """Remove a group SOP by id or (case-insensitive) title. Returns True if removed."""
        sops = self.load_group_sops()
        key = str(ident).strip().lower()
        kept = [s for s in sops
                if s.get("id", "").lower() != key
                and s.get("title", "").strip().lower() != key]
        if len(kept) == len(sops):
            return False
        self._save_group_sops(kept)
        return True

    def _save_group_sops(self, sops: list[dict]) -> None:
        self._group_sops_path.write_text(json.dumps(sops, indent=2), encoding="utf-8")

    # ── User corrections ───────────────────────────────────────────────────────

    def save_correction(
        self,
        turn:      int,
        original:  str,
        corrected: str,
    ) -> None:
        """Record that the user corrected an AI response."""
        record = {
            "turn":      turn,
            "original":  original,
            "corrected": corrected,
            "ts":        _now(),
            "user_id":   self._user_id,
        }
        with self._corrections_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        logger.debug("[Memory] Saved correction (turn=%d)", turn)

    # ── User preferences ───────────────────────────────────────────────────────

    def update_preferences(self, **kwargs: Any) -> None:
        """Merge kwargs into the user preferences YAML file."""
        prefs = self._load_preferences()
        prefs.update(kwargs)
        self._preferences_path.write_text(
            yaml.dump(prefs, default_flow_style=False), encoding="utf-8"
        )

    # ── User context (sample details for current session) ─────────────────────

    def update_user_context(self, **kwargs: Any) -> None:
        """
        Update transient sample context for the current session.
        Stored in preferences.yml under the ``_session_context`` key.
        """
        prefs = self._load_preferences()
        prefs.setdefault("_session_context", {}).update(kwargs)
        self._preferences_path.write_text(
            yaml.dump(prefs, default_flow_style=False), encoding="utf-8"
        )

    def clear_user_context(self) -> None:
        """Clear the transient session context (call at session end)."""
        prefs = self._load_preferences()
        prefs.pop("_session_context", None)
        self._preferences_path.write_text(
            yaml.dump(prefs, default_flow_style=False), encoding="utf-8"
        )

    # ── Session summaries ──────────────────────────────────────────────────────

    def save_session_summary(self, session_id: str, summary: str) -> None:
        """Persist a plain-text digest of a completed conversation session."""
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = f"{ts}_{session_id[:8]}.txt"
        (self._session_summary_dir / name).write_text(summary, encoding="utf-8")
        logger.debug("[Memory] Session summary saved: %s", name)

    # ── Project layer (Layer 2) ────────────────────────────────────────────────

    def log_project_event(
        self,
        project_root: str | Path,
        action:       str,
        detail:       str = "",
    ) -> None:
        """Append an entry to the project-level experiment_history.jsonl."""
        record = {"ts": _now(), "action": action, "detail": detail}
        hist_path = _project_memory_dir(project_root) / "experiment_history.jsonl"
        with hist_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def log_quality_flag(
        self,
        project_root: str | Path,
        file_path:    str,
        flag:         str,
        source:       str = "ai",
    ) -> None:
        """Append a quality flag to the project quality_log.jsonl."""
        record = {
            "ts":        _now(),
            "file_path": str(file_path),
            "flag":      flag,
            "source":    source,
        }
        log_path = _project_memory_dir(project_root) / "quality_log.jsonl"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    # ── Per-project chat history (Layer 2) ──────────────────────────────────────

    def append_chat(
        self,
        project_root: str | Path,
        role:         str,
        text:         str,
    ) -> None:
        """Append one chat turn (role + plain text) to the project's history."""
        if not text or not str(text).strip():
            return
        record = {"ts": _now(), "role": role, "text": str(text)[:8000]}
        path = _project_memory_dir(project_root) / "chat_history.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.debug("[Memory] Could not append chat: %s", exc)

    def load_chat(
        self,
        project_root: str | Path,
        max_turns:    int = 40,
    ) -> list[dict]:
        """Return the last *max_turns* chat turns for a project as
        [{"role","text","ts"}], oldest first."""
        path = _project_memory_dir(project_root) / "chat_history.jsonl"
        if not path.exists():
            return []
        out: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception as exc:
            logger.debug("[Memory] Could not load chat: %s", exc)
            return []
        return out[-max_turns:]

    def clear_chat(self, project_root: str | Path) -> None:
        """Delete the project's persisted chat history."""
        path = _project_memory_dir(project_root) / "chat_history.jsonl"
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.debug("[Memory] Could not clear chat: %s", exc)

    # ── Facility layer (Layer 3) ───────────────────────────────────────────────

    def list_beamlines(self) -> list[str]:
        """Return available beamline IDs (YAML stems in ai_knowledge/beamline/)."""
        return [p.stem for p in self._beamline_dir.glob("*.yml")]

    # ── Internal loaders ───────────────────────────────────────────────────────

    def _load_preferences(self) -> dict:
        if self._preferences_path.exists():
            try:
                return yaml.safe_load(
                    self._preferences_path.read_text(encoding="utf-8")
                ) or {}
            except Exception:
                pass
        return {}

    def _load_corrections(self, n: int) -> list[dict]:
        if not self._corrections_path.exists():
            return []
        lines = self._corrections_path.read_text(encoding="utf-8").splitlines()
        records = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
            if len(records) >= n:
                break
        return records

    def _load_user_context(self) -> dict:
        prefs = self._load_preferences()
        return prefs.get("_session_context", {})

    def _load_recent_summaries(self, n: int) -> list[str]:
        files = sorted(self._session_summary_dir.glob("*.txt"), reverse=True)[:n]
        return [f.read_text(encoding="utf-8") for f in files]

    def _load_project_history(
        self, project_root: str | Path, n: int = 20
    ) -> list[dict]:
        hist_path = _project_memory_dir(project_root) / "experiment_history.jsonl"
        if not hist_path.exists():
            return []
        lines = hist_path.read_text(encoding="utf-8").splitlines()
        records = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
            if len(records) >= n:
                break
        return list(reversed(records))

    def _load_quality_log(
        self, project_root: str | Path, n: int = 30
    ) -> list[dict]:
        log_path = _project_memory_dir(project_root) / "quality_log.jsonl"
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding="utf-8").splitlines()
        records = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                pass
            if len(records) >= n:
                break
        return list(reversed(records))

    def _load_beamline_notes(self, beamline_id: str) -> str:
        path = self._beamline_dir / f"{beamline_id}.yml"
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_memory_dir(project_root: str | Path) -> Path:
    d = Path(project_root) / ".swaxs" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d
