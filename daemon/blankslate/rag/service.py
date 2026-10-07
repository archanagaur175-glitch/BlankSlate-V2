"""RAG glue: delta ingestion from ContextStore + retrieval (Phase 4, F4).

Judge calls retrieve_memory() on the LLM-bound path:
  1. ingest newly CLOSED store records (trailing open turn skipped),
  2. embed the query,
  3. top-k vector search, threshold-filtered snippets back to judge.

Any failure propagates to the judge hook, which logs and continues —
memory must never break normal chat.
"""

from __future__ import annotations

import logging
from pathlib import Path

from blankslate.rag.store import DEFAULT_MAX_DISTANCE, DEFAULT_K, VecStore

logger = logging.getLogger("blankslate.rag")

_store: VecStore | None = None
_store_path: Path | None = None


def configure(db_path: Path | str | None) -> None:
    """Point the RAG service at a specific DB (tests / fresh start)."""
    global _store, _store_path
    resolved = Path(db_path) if db_path else None
    if _store is not None and resolved == _store_path:
        return
    if _store is not None:
        try:
            _store.close()
        except Exception:
            pass
    _store = None
    _store_path = resolved


def _get_store() -> VecStore:
    global _store
    if _store is None:
        _store = VecStore(_store_path)
    return _store


def close_store() -> None:
    """Shutdown hook: encrypt-on-close the singleton store if it is open."""
    global _store
    if _store is not None:
        try:
            _store.close()
        except Exception:
            logger.exception("rag store close failed")
        _store = None


def purge(cutoff: float) -> int:
    """TTL purge for the singleton store; no-op (0) when not open."""
    if _store is None:
        return 0
    return _store.purge_created_before(cutoff)


def ingest_count() -> int:
    """Number of ContextStore records consumed into the vector store."""
    return _get_store().get_cursor()


async def retrieve_memory(
    ctx_store,
    llm,
    query: str,
    *,
    k: int = DEFAULT_K,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> list[str]:
    """Ingest closed turns (delta), then retrieve memory chunks for query."""
    store = _get_store()

    # ── delta ingestion: closed records only (trailing turn still open) ──
    records = ctx_store.recent(limit=10 ** 9)
    closed = records[: max(0, len(records) - 1)]
    cursor = store.get_cursor()
    if cursor > len(closed):
        # History shrank below the ingestion high-water mark (gate restore
        # or TTL purge dropped records). The cursor no longer indexes the
        # current records — rebuild memory from what remains instead of
        # stalling (or slicing wrong content) forever.
        logger.info(
            "RAG history shrank (%d ingested > %d closed): rebuilding store",
            cursor,
            len(closed),
        )
        store.clear()
        cursor = 0
    if len(closed) > cursor:
        pending = closed[cursor:]
        texts = [f"{r.role}: {r.content}" for r in pending if (r.content or "").strip()]
        if texts:
            vectors = await llm.embed(texts)  # raises before cursor advance
            added = store.add(texts, ["history"] * len(texts), vectors)
            logger.info(
                "RAG ingest: +%d chunk(s) (cursor %d -> %d)",
                added,
                cursor,
                len(closed),
            )
        store.set_cursor(len(closed))

    # ── retrieval ──
    if store.count() == 0 or not query.strip():
        return []
    qvec = (await llm.embed([query]))[0]
    hits = store.search(qvec, k=k)
    snippets = [h.text for h in hits if h.distance <= max_distance]
    logger.info(
        "RAG retrieve: %d/%d hit(s) within distance %.3f",
        len(snippets),
        len(hits),
        max_distance,
    )
    return snippets
