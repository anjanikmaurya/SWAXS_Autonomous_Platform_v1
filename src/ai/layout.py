"""src/ai/layout.py: where every piece of the assistant's knowledge lives.

ONE place defines the paths, so the harness is inspectable: what the model is
told, in which order, and from which file. See ai_knowledge/README.md for what
each layer means and ai_knowledge/sources.yml for the full list.

    ai_knowledge/
      rules/        authoritative, injected every turn, highest priority first
        corrections.json   operator corrections (outrank everything)
        sops.json          group conventions / SOPs
        system_prompt.md   the assistant's base instructions
      reference/    searched by meaning (the vector index)
        literature/  user_papers/  beamline/
      cases/        expert-judged worked examples (knowledge pack; may be empty)
      evals/        exam questions with marking points (may be empty)
      index/        vector_db/ + ingestion_log.json (rebuilt, not in git)

Older clones keep these files at the ai_knowledge/ root (corrections.json,
group/sops.json, literature/, beamline/, user_papers/, vector_db/,
ingestion_log.json). `migrate()` moves them once, idempotently, and rewrites
the ingestion log's absolute-path keys so the index does not re-ingest (and
duplicate) every paper after the move.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REFERENCE_COLLECTIONS = ("literature", "user_papers", "beamline")


@dataclass(frozen=True)
class KnowledgeLayout:
    root: Path

    def __init__(self, root) -> None:
        object.__setattr__(self, "root", Path(root))

    # ── rules (injected every turn) ──────────────────────────────────────────
    @property
    def rules_dir(self) -> Path:
        return self.root / "rules"

    @property
    def corrections(self) -> Path:
        return self.rules_dir / "corrections.json"

    @property
    def sops(self) -> Path:
        return self.rules_dir / "sops.json"

    @property
    def system_prompt(self) -> Path:
        return self.rules_dir / "system_prompt.md"

    # ── reference (searched) ─────────────────────────────────────────────────
    @property
    def reference_dir(self) -> Path:
        return self.root / "reference"

    def reference(self, collection: str) -> Path:
        return self.reference_dir / collection

    # ── examples, exam, index ────────────────────────────────────────────────
    @property
    def cases_dir(self) -> Path:
        return self.root / "cases"

    @property
    def evals_dir(self) -> Path:
        return self.root / "evals"

    @property
    def index_dir(self) -> Path:
        return self.root / "index"

    @property
    def vector_db(self) -> Path:
        return self.index_dir / "vector_db"

    @property
    def ingestion_log(self) -> Path:
        return self.index_dir / "ingestion_log.json"


#: The repository's own knowledge folder.
REPO_KB = Path(__file__).resolve().parents[2] / "ai_knowledge"


def _legacy_moves(L: KnowledgeLayout) -> list[tuple[Path, Path]]:
    r = L.root
    moves = [(r / "corrections.json", L.corrections),
             (r / "group" / "sops.json", L.sops),
             (r / "vector_db", L.vector_db),
             (r / "ingestion_log.json", L.ingestion_log)]
    moves += [(r / c, L.reference(c)) for c in REFERENCE_COLLECTIONS]
    return moves


def _rewrite_log_keys(log_path: Path, kb_name: str = "ai_knowledge") -> bool:
    """The ingestion log is keyed by absolute file path. After the reference
    folders move, rewrite ``.../ai_knowledge/<coll>/x`` to
    ``.../ai_knowledge/reference/<coll>/x`` so unchanged files are not
    re-ingested (and duplicated). Matches by folder name, not by absolute
    prefix, so a log written on another machine or OS is fixed too.
    Idempotent; returns True if anything changed."""
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    changed = False
    for coll, entries in list(log.items()):
        if not isinstance(entries, dict):
            continue
        new_entries = {}
        for key, val in entries.items():
            nk = key
            for sep in ("/", "\\"):
                for c in REFERENCE_COLLECTIONS:
                    old = f"{sep}{kb_name}{sep}{c}{sep}"
                    if old in nk:
                        nk = nk.replace(old, f"{sep}{kb_name}{sep}reference{sep}{c}{sep}")
            changed |= nk != key
            new_entries[nk] = val
        log[coll] = new_entries
    if changed:
        tmp = log_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(log, indent=2), encoding="utf-8")
        os.replace(tmp, log_path)
    return changed


def migrate(root=REPO_KB) -> list[str]:
    """Move legacy files into the layout. Idempotent: a file already at its new
    place is never overwritten (the legacy copy is then left alone and reported).
    Returns a list of human-readable actions."""
    L = KnowledgeLayout(root)
    if not L.root.is_dir():
        return []
    actions: list[str] = []
    moved: list[tuple[Path, Path]] = []
    for old, new in _legacy_moves(L):
        if not old.exists():
            continue
        if new.exists():
            if old.is_dir() and not any(old.iterdir()):
                old.rmdir()
            else:
                actions.append(f"kept legacy {old.relative_to(L.root)} "
                               f"(already have {new.relative_to(L.root)})")
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        moved.append((old, new))
        actions.append(f"moved {old.relative_to(L.root)} -> {new.relative_to(L.root)}")
    grp = L.root / "group"
    if grp.is_dir() and not any(grp.iterdir()):
        grp.rmdir()
    if L.ingestion_log.exists() and _rewrite_log_keys(L.ingestion_log, L.root.name):
        actions.append("updated index/ingestion_log.json paths to reference/")
    for d in (L.rules_dir, L.cases_dir, L.evals_dir, L.index_dir,
              *(L.reference(c) for c in REFERENCE_COLLECTIONS)):
        d.mkdir(parents=True, exist_ok=True)
    for a in actions:
        logger.info("[knowledge layout] %s", a)
    return actions


def load_system_prompt(root=REPO_KB) -> str:
    """The assistant's base instructions, from rules/system_prompt.md."""
    return KnowledgeLayout(root).system_prompt.read_text(encoding="utf-8")
