"""sqlite-vec vector store for long-term conversation memory (Phase 4).

Write-side REDACTION CHOKE POINT: every chunk passes through
redact_text() here — no secret-shaped string ever touches the vector
DB, regardless of what the caller hands over.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from blankslate.guardrail.redact import redact_text
from blankslate.vault import core as vault

logger = logging.getLogger("blankslate.rag.store")

DIM = 768  # nomic-embed-text output size
DEFAULT_K = 5
DEFAULT_MAX_DISTANCE = 0.50  # cosine distance (1 - sim); calibrated: must-hit
                             # sims 0.59/0.71, must-miss 0.39/0.47/0.48


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "rag_memory.sqlite3"


@dataclass(frozen=True)
class Chunk:
    rowid: int
    text: str
    source: str
    distance: float


class VecStore:
    """SQLite + sqlite-vec chunk store with a persisted ingestion cursor.

    Phase 3 vault lifecycle: the DB file lives encrypted at rest
    (Fernet envelope), decrypted on open and re-encrypted on close. The
    lock file marks the plaintext window and is acquired BEFORE the
    decrypt step (Amendment A). `created` (float) is the TTL purge key —
    sqlite-vec vec0 forbids ALTER, so it must exist from CREATE time.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = vault.rag_lock_path(self._path)
        self._closed = False
        # Lock FIRST, then decrypt-on-open: every plaintext moment is marked.
        vault.acquire_lock(self._lock_path)
        if self._path.exists():
            on_disk = self._path.read_bytes()
            if vault.is_envelope(on_disk):
                tmp = self._path.with_name(self._path.name + ".tmp")
                tmp.write_bytes(vault.open_envelope(on_disk))
                os.replace(tmp, self._path)
                logger.info("RAG DB decrypted for open: %s", self._path)
        self._conn = sqlite3.connect(str(self._path))
        # Windows Python ships with extension loading off by default.
        self._conn.enable_load_extension(True)
        try:
            sqlite_vec.load(self._conn)
        finally:
            try:
                self._conn.enable_load_extension(False)
            except Exception:
                pass
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0("
            f"embedding float[{DIM}] distance_metric=cosine, "
            f"chunk_text text, source text, created float)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)"
        )
        self._conn.commit()
        logger.info("VecStore ready: %s (dim=%d)", self._path, DIM)

    @property
    def path(self) -> Path:
        return self._path

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def get_cursor(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'ingested'"
        ).fetchone()
        return int(row[0]) if row else 0

    def set_cursor(self, value: int) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES('ingested', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (int(value),),
        )
        self._conn.commit()

    def add(self, texts: list[str], sources: list[str], vectors: list[list[float]]) -> int:
        """Add chunks. Redaction choke point: text is redacted HERE, always."""
        if not (len(texts) == len(sources) == len(vectors)):
            raise ValueError(
                f"length mismatch: {len(texts)} texts / "
                f"{len(sources)} sources / {len(vectors)} vectors"
            )
        added = 0
        for text, source, vec in zip(texts, sources, vectors):
            cleaned = redact_text(text)
            if not cleaned.strip():
                continue
            self._conn.execute(
                "INSERT INTO chunks(embedding, chunk_text, source, created) "
                "VALUES (?, ?, ?, ?)",
                (
                    sqlite_vec.serialize_float32([float(x) for x in vec]),
                    cleaned,
                    str(source),
                    float(time.time()),
                ),
            )
            added += 1
        self._conn.commit()
        return added

    def search(self, vector: list[float], k: int = DEFAULT_K) -> list[Chunk]:
        """Top-k KNN by cosine distance, ranked nearest-first."""
        total = self.count()
        if total == 0:
            return []
        k = max(1, min(int(k), total))
        rows = self._conn.execute(
            f"SELECT rowid, distance, chunk_text, source FROM chunks "
            f"WHERE embedding MATCH ? AND k = {k}",
            (sqlite_vec.serialize_float32([float(x) for x in vector]),),
        ).fetchall()
        return [
            Chunk(rowid=int(r[0]), distance=float(r[1]), text=r[2], source=r[3])
            for r in rows
        ]

    def get_text(self, rowid: int) -> str | None:
        row = self._conn.execute(
            "SELECT chunk_text FROM chunks WHERE rowid = ?", (int(rowid),)
        ).fetchone()
        return str(row[0]) if row else None

    def purge_created_before(self, cutoff: float) -> int:
        """TTL purge: delete chunks older than cutoff (epoch seconds)."""
        if self._closed:
            return 0
        cur = self._conn.execute(
            "DELETE FROM chunks WHERE created < ?", (float(cutoff),)
        )
        self._conn.commit()
        return max(0, int(cur.rowcount))

    def clear(self) -> None:
        """Drop all chunks and reset the ingestion cursor atomically.

        Used when the source history shrank (restore/purge) and the old
        high-water cursor no longer indexes anything — the store is
        rebuilt from the current records on the next ingest pass.
        """
        if self._closed:
            return
        with self._conn:
            self._conn.execute("DELETE FROM chunks")
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES('ingested', 0) "
                "ON CONFLICT(key) DO UPDATE SET value = 0"
            )
        logger.info("RAG store cleared (history rebuild): %s", self._path)

    def close(self) -> None:
        """Encrypt-on-close, then release the lock (lock AFTER encrypt)."""
        if self._closed:
            return
        self._closed = True
        self._conn.close()
        try:
            if self._path.exists():
                on_disk = self._path.read_bytes()
                if not vault.is_envelope(on_disk):
                    vault.write_bytes(self._path, on_disk)
                    logger.info("RAG DB encrypted on close: %s", self._path)
        finally:
            vault.release_lock(self._lock_path)
