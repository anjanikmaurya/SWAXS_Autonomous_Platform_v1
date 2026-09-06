"""
src/ai/knowledge.py — Living Knowledge Base
=============================================
Manages a ChromaDB vector store that serves as the AI assistant's long-term
domain knowledge.  Documents are chunked, embedded (sentence-transformers),
and stored persistently so ingestion only ever runs once per document.

Collections
-----------
  literature   — SAXS/WAXS textbooks, review papers, instrument papers
  apps         — per-app knowledge.md files. NOTE: no longer queried at runtime.
                 The in-scope app's doc is injected directly into the assistant's
                 system prompt from disk (assistant.py::_resolve_app_knowledge);
                 it is not ingested here anymore. See RAG_COLLECTIONS.
  user_papers  — user-uploaded sample-specific PDFs (runtime uploads)
  beamline     — facility/instrument YAML configs

Usage
-----
    from src.ai.knowledge import KnowledgeBase

    kb = KnowledgeBase("/abs/path/to/ai_knowledge")
    kb.ingest_pdf("literature/glatter_kratky.pdf", collection="literature")
    kb.ingest_markdown("reduction/knowledge.md",   collection="apps")

    results = kb.retrieve("how to determine Guinier range", top_k=6)
    for chunk in results:
        print(chunk["text"], chunk["source"])
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("swaxs_platform")

# ── Chunking parameters ────────────────────────────────────────────────────────
_CHUNK_SIZE    = 900    # characters per chunk
_CHUNK_OVERLAP = 180   # character overlap between adjacent chunks
_MIN_CHUNK     = 80    # discard chunks shorter than this

# ── ChromaDB collection names ──────────────────────────────────────────────────
COLLECTION_LITERATURE  = "literature"
COLLECTION_APPS        = "apps"
COLLECTION_USER_PAPERS = "user_papers"
COLLECTION_BEAMLINE    = "beamline"

ALL_COLLECTIONS = [
    COLLECTION_LITERATURE,
    COLLECTION_APPS,
    COLLECTION_USER_PAPERS,
    COLLECTION_BEAMLINE,
]

#: Collections that STILL use semantic RAG retrieval at query time. `apps` is
#: deliberately excluded: each app's knowledge.md is a small, single, already-
#: in-scope document that the assistant injects verbatim into its system prompt
#: (see src/ai/assistant.py::_resolve_app_knowledge), so chunk-level semantic
#: search over it solves a problem we don't have — and it was the collection
#: worst hit by the chunker bug. literature/user_papers/beamline stay on RAG:
#: those are volume-of-documents corpora that injection can't replace.
RAG_COLLECTIONS = [
    COLLECTION_LITERATURE,
    COLLECTION_USER_PAPERS,
    COLLECTION_BEAMLINE,
]


class KnowledgeBase:
    """
    Persistent ChromaDB-backed knowledge base for the SWAXS AI assistant.

    Parameters
    ----------
    base_dir : str | Path
        Root of the ``ai_knowledge/`` folder.  The ChromaDB store is kept at
        ``base_dir/vector_db/`` and the ingestion log at
        ``base_dir/ingestion_log.json``.
    embedding_model : str
        Sentence-transformers model used for embedding.  Default is
        ``all-MiniLM-L6-v2`` — fast and runs on CPU.
    """

    def __init__(
        self,
        base_dir: str | Path,
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._base      = Path(base_dir)
        self._db_path   = self._base / "vector_db"
        self._log_path  = self._base / "ingestion_log.json"
        self._em_model  = embedding_model
        self._client    = None   # lazy — created on first use
        self._ef        = None   # embedding function, lazy
        self._log: dict = {}     # {collection: {source_key: {...}}}

        self._db_path.mkdir(parents=True, exist_ok=True)
        self._load_log()

    # ── Public API ─────────────────────────────────────────────────────────────

    def ingest_pdf(
        self,
        pdf_path:   str | Path,
        collection: str = COLLECTION_LITERATURE,
        *,
        force:      bool = False,
    ) -> int:
        """
        Chunk a PDF and add it to *collection*.

        Parameters
        ----------
        pdf_path   : path to the PDF file
        collection : target ChromaDB collection name
        force      : re-ingest even if the file hash is unchanged

        Returns the number of chunks added (0 if already up-to-date).
        """
        path = Path(pdf_path)
        if not path.exists():
            logger.warning("[KB] PDF not found: %s", path)
            return 0

        file_hash = _sha256(path)
        key       = str(path.resolve())

        if not force and self._already_ingested(collection, key, file_hash):
            logger.debug("[KB] Skipping (unchanged): %s", path.name)
            return 0

        text = _extract_pdf_text(path)
        if not text.strip():
            logger.warning("[KB] No text extracted from %s", path.name)
            return 0

        chunks = _chunk_text(text)
        n = self._add_chunks(
            chunks,
            collection = collection,
            source     = path.name,
            doc_type   = "pdf",
        )
        self._record_ingestion(collection, key, file_hash, n)
        logger.info("[KB] Ingested %s → %d chunks into '%s'",
                    path.name, n, collection)
        return n

    def ingest_markdown(
        self,
        md_path:    str | Path,
        collection: str = COLLECTION_APPS,
        *,
        force:      bool = False,
    ) -> int:
        """
        Chunk a Markdown file and add it to *collection*.
        Splits on ``##`` headings for natural semantic boundaries.
        """
        path = Path(md_path)
        if not path.exists():
            logger.debug("[KB] Markdown not found: %s", path)
            return 0

        file_hash = _sha256(path)
        key       = str(path.resolve())

        if not force and self._already_ingested(collection, key, file_hash):
            logger.debug("[KB] Skipping (unchanged): %s", path.name)
            return 0

        text   = path.read_text(encoding="utf-8", errors="replace")
        chunks = _chunk_markdown(text)
        # Qualify the source with its parent dir (e.g. "reduction/knowledge.md")
        # instead of the bare "knowledge.md": otherwise all ten apps share one
        # source string — citations can't tell them apart, and removing/replacing
        # one app's chunks would key on "knowledge.md" and wipe them all.
        src_label = f"{path.parent.name}/{path.name}" if path.parent.name else path.name
        n = self._add_chunks(
            chunks,
            collection = collection,
            source     = src_label,
            doc_type   = "markdown",
        )
        self._record_ingestion(collection, key, file_hash, n)
        logger.info("[KB] Ingested %s → %d chunks into '%s'",
                    path.name, n, collection)
        return n

    def ingest_yaml(
        self,
        yaml_path:  str | Path,
        collection: str = COLLECTION_BEAMLINE,
        *,
        force:      bool = False,
    ) -> int:
        """
        Ingest a YAML config (beamline notes) as a single chunk.
        """
        path = Path(yaml_path)
        if not path.exists():
            logger.debug("[KB] YAML not found: %s", path)
            return 0

        file_hash = _sha256(path)
        key       = str(path.resolve())

        if not force and self._already_ingested(collection, key, file_hash):
            return 0

        text   = path.read_text(encoding="utf-8", errors="replace")
        chunks = [text] if len(text) <= _CHUNK_SIZE * 3 else _chunk_text(text)
        n = self._add_chunks(
            chunks,
            collection = collection,
            source     = path.name,
            doc_type   = "yaml",
        )
        self._record_ingestion(collection, key, file_hash, n)
        logger.info("[KB] Ingested %s → %d chunks into '%s'",
                    path.name, n, collection)
        return n

    def retrieve(
        self,
        query:        str,
        top_k:        int = 8,
        collection:   str | None = None,
        collections:  list[str] | None = None,
    ) -> list[dict]:
        """
        Semantic search across the knowledge base.

        Parameters
        ----------
        query       : natural language query
        top_k       : number of chunks to return per collection
        collection  : if given, search only that single collection
        collections : if given, search exactly this list of collections (takes
                      precedence over `collection`). Callers pass RAG_COLLECTIONS
                      here to skip `apps` — that doc is injected, not retrieved.
                      If neither is given, search all collections.

        The query is embedded ONCE and reused across the chosen collections.

        Returns a list of dicts with keys:
            text, source, doc_type, collection, distance
        sorted by relevance (ascending distance).
        """
        if collections is not None:
            cols = list(collections)
        elif collection:
            cols = [collection]
        else:
            cols = ALL_COLLECTIONS
        hits: list[dict] = []

        # Embed the query ONCE and reuse across collections. Previously each
        # collection's col.query(query_texts=[query]) re-encoded the same string
        # with the SentenceTransformer — 4× the embedding CPU per chat turn (on the
        # 2-thread cap). Falls back to text query if the embedder isn't ready.
        q_emb = None
        try:
            self._client_init()
            if self._ef is not None:
                q_emb = self._ef([query])
        except Exception:
            q_emb = None

        for col_name in cols:
            col = self._get_collection(col_name)
            if col is None:
                continue
            try:
                count = col.count()
                if count == 0:
                    continue
                k = min(top_k, count)
                res = (col.query(query_embeddings=q_emb, n_results=k)
                       if q_emb is not None
                       else col.query(query_texts=[query], n_results=k))
                docs  = res.get("documents", [[]])[0]
                metas = res.get("metadatas",  [[]])[0]
                dists = res.get("distances",  [[]])[0]
                for doc, meta, dist in zip(docs, metas, dists):
                    hits.append({
                        "text":       doc,
                        "source":     meta.get("source", "unknown"),
                        "doc_type":   meta.get("doc_type", ""),
                        "collection": col_name,
                        "distance":   dist,
                    })
            except Exception as exc:
                logger.debug("[KB] Query error on '%s': %s", col_name, exc)

        hits.sort(key=lambda h: h["distance"])
        return hits[:top_k]

    def collection_stats(self) -> dict[str, int]:
        """Return {collection_name: document_count} for all collections."""
        stats = {}
        for name in ALL_COLLECTIONS:
            col = self._get_collection(name)
            stats[name] = col.count() if col else 0
        return stats

    def list_ingested(self, collection: str | None = None) -> list[dict]:
        """Return the ingestion log entries (optionally filtered by collection)."""
        out = []
        for col, sources in self._log.items():
            if collection and col != collection:
                continue
            for src, meta in sources.items():
                out.append({"collection": col, "source": src,
                            "name": Path(src).name, **meta})
        return out

    def ingest_text(
        self,
        text:       str,
        name:       str,
        collection: str = COLLECTION_USER_PAPERS,
    ) -> int:
        """
        Add a free-text note/snippet (no PDF) to *collection* so it can be
        retrieved and cited later. ``name`` is the source label.

        Returns the number of chunks added.
        """
        if not text or not text.strip():
            return 0
        chunks = _chunk_text(text)
        if not chunks:                       # short notes fall below _MIN_CHUNK —
            chunks = [text.strip()]          # keep them as a single chunk anyway
        n = self._add_chunks(chunks, collection=collection,
                             source=name, doc_type="note")
        # log under a stable key so it can be listed/removed
        key = f"note:{name}"
        self._record_ingestion(collection, key, _sha256_text(text), n)
        logger.info("[KB] Added note '%s' → %d chunks into '%s'",
                    name, n, collection)
        return n

    def remove_source(
        self,
        source:     str,
        collection: str | None = None,
    ) -> dict:
        """
        Remove an ingested source (by file name, note name, or log key) from the
        vector store AND the ingestion log. Reversible by re-ingesting.

        Returns {"removed": [names], "chunks": int} or {"error": ...}.
        """
        s = str(source).strip().lower()
        targets = []   # (collection, key, display_name)
        for col, sources in self._log.items():
            if collection and col != collection:
                continue
            for key in list(sources):
                name = Path(key).name if not key.startswith("note:") else key[5:]
                if s in (key.lower(), name.lower()) or s in name.lower():
                    targets.append((col, key, name))
        if not targets:
            return {"error": f"No ingested source matching '{source}'."}

        removed_chunks = 0
        for col, key, name in targets:
            c = self._get_collection(col)
            if c is not None:
                try:
                    c.delete(where={"source": name})
                except Exception as exc:
                    logger.debug("[KB] chroma delete failed for %s: %s", name, exc)
            removed_chunks += int(self._log.get(col, {}).get(key, {}).get("chunks", 0))
            self._log.get(col, {}).pop(key, None)
        self._save_log()
        return {"removed": [t[2] for t in targets], "chunks": removed_chunks}

    # ── Internal ───────────────────────────────────────────────────────────────

    def _client_init(self):
        """Lazy initialise ChromaDB client and embedding function."""
        if self._client is not None:
            return
        try:
            import chromadb
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction,
            )
            # NB: CPU-thread capping is handled by the OMP/MKL env vars set in
            # assistant/app.py BEFORE torch loads (the assistant is the only KB
            # user). Not re-imposed here — a direct `import torch` would also be an
            # undeclared dependency (torch is transitive via sentence-transformers).
            self._client = chromadb.PersistentClient(path=str(self._db_path))
            self._ef     = SentenceTransformerEmbeddingFunction(
                model_name=self._em_model
            )
            logger.info("[KB] ChromaDB initialised at %s", self._db_path)
        except ImportError as exc:
            logger.error(
                "[KB] chromadb or sentence-transformers not installed: %s. "
                "Run: pip install chromadb sentence-transformers", exc
            )
            raise

    def _get_collection(self, name: str):
        """Return a ChromaDB collection, or None if ChromaDB is unavailable."""
        try:
            self._client_init()
            return self._client.get_or_create_collection(
                name=name,
                embedding_function=self._ef,
            )
        except Exception as exc:
            logger.debug("[KB] Cannot get collection '%s': %s", name, exc)
            return None

    def _add_chunks(
        self,
        chunks:     list[str],
        collection: str,
        source:     str,
        doc_type:   str,
    ) -> int:
        col = self._get_collection(collection)
        if col is None:
            return 0

        # Upsert semantics: drop this source's existing chunks before adding the
        # new ones. IDs embed a timestamp, so without this a re-ingest of an
        # EDITED file left the old chunks behind forever (the corpus grew ~2x per
        # edit, and a renamed/removed doc's chunks stayed retrievable). Scoped by
        # the now-qualified `source` (see ingest_markdown).
        try:
            col.delete(where={"source": source})
        except Exception as exc:
            logger.debug("[KB] pre-add delete for '%s' skipped: %s", source, exc)

        # Deduplicate chunk IDs using source + index
        src_key  = re.sub(r"[^a-zA-Z0-9_\-]", "_", source)
        ts_tag   = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        ids      = [f"{src_key}_{ts_tag}_{i}" for i, _ in enumerate(chunks)]
        metas    = [{"source": source, "doc_type": doc_type, "chunk_idx": i}
                    for i in range(len(chunks))]

        try:
            col.add(documents=chunks, metadatas=metas, ids=ids)
            return len(chunks)
        except Exception as exc:
            logger.warning("[KB] add() failed for '%s': %s", collection, exc)
            return 0

    def _already_ingested(
        self, collection: str, key: str, file_hash: str
    ) -> bool:
        entry = self._log.get(collection, {}).get(key)
        return entry is not None and entry.get("hash") == file_hash

    def _record_ingestion(
        self,
        collection: str,
        key:        str,
        file_hash:  str,
        n_chunks:   int,
    ) -> None:
        self._log.setdefault(collection, {})[key] = {
            "hash":         file_hash,
            "chunks":       n_chunks,
            "ingested_at":  datetime.now(timezone.utc).isoformat(),
        }
        self._save_log()

    def _load_log(self) -> None:
        if self._log_path.exists():
            try:
                self._log = json.loads(self._log_path.read_text(encoding="utf-8"))
            except Exception:
                self._log = {}
        else:
            self._log = {}

    def _save_log(self) -> None:
        self._log_path.write_text(json.dumps(self._log, indent=2), encoding="utf-8")


# ── Text extraction helpers ───────────────────────────────────────────────────

def _extract_pdf_text(path: Path) -> str:
    """Extract plain text from a PDF using pypdf (preferred) or pdfminer."""
    # Try pypdf first (lighter dependency)
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        pages  = [p.extract_text() or "" for p in reader.pages]
        return "\n\n".join(pages)
    except ImportError:
        pass

    # Fallback: pdfminer.six
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        return pdfminer_extract(str(path))
    except ImportError:
        pass

    logger.error(
        "[KB] Cannot extract PDF text — install pypdf: pip install pypdf"
    )
    return ""


# ── Chunking helpers ──────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list[str]:
    """
    Split *text* into overlapping character-level chunks.
    Tries to break on paragraph or sentence boundaries.
    """
    # Normalise whitespace a bit
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    chunks: list[str] = []
    start = 0
    length = len(text)

    while start < length:
        end = min(start + _CHUNK_SIZE, length)

        # Try to break at paragraph boundary
        if end < length:
            pb = text.rfind("\n\n", start, end)
            if pb != -1 and pb > start + _MIN_CHUNK:
                end = pb + 2
            else:
                # Try sentence boundary
                sb = max(
                    text.rfind(". ", start, end),
                    text.rfind(".\n", start, end),
                )
                if sb != -1 and sb > start + _MIN_CHUNK:
                    end = sb + 2

        chunk = text[start:end].strip()
        if len(chunk) >= _MIN_CHUNK:
            chunks.append(chunk)

        if end >= length:
            break
        # Guarantee forward progress. The old `max(start+1, end-overlap)` advanced
        # by a SINGLE character whenever a paragraph/sentence break landed within
        # _CHUNK_OVERLAP of start (every section tail), emitting ~100 near-identical
        # 1-char-shifted fragments per section and bloating the corpus ~60%. Step
        # back by the overlap only when that still moves past the current chunk;
        # otherwise jump to `end` (no overlap) so we never stall.
        nxt = end - _CHUNK_OVERLAP
        start = nxt if nxt > start else end

    return chunks


def _chunk_markdown(text: str) -> list[str]:
    """
    Split Markdown on ``##`` headings, keeping each section as one chunk.
    Falls back to character chunking for very long sections.
    """
    # Split on level-2+ headings
    sections = re.split(r"(?m)^#{1,3} ", text)
    chunks: list[str] = []

    for section in sections:
        section = section.strip()
        if not section or len(section) < _MIN_CHUNK:
            continue
        if len(section) <= _CHUNK_SIZE * 2:
            chunks.append(section)
        else:
            # Section is too long — sub-chunk it
            chunks.extend(_chunk_text(section))

    return chunks if chunks else _chunk_text(text)


# ── Utilities ──────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
