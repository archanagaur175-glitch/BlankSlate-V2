"""In-memory FTS5 search index (design doc section 3 + honesty section 7).

The index is a `:memory:` sqlite database — nothing FTS-related is ever
created on disk. The plaintext of every sealed row lives in this index for
the daemon process lifetime (documented limitation, security doc 7.2),
rebuilt lazily from the sealed rows on first use.

Thread model: index operations only ever run on the asyncio loop thread
(session loop hops back after its executor pass; purge runs on the loop).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Iterable

from blankslate.timeline import store as timeline_store
from blankslate.vault import core as vault

logger = logging.getLogger("blankslate.timeline.index")


class TimelineIndex:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("CREATE VIRTUAL TABLE fts USING fts5(text)")
        self._built = False

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def built(self) -> bool:
        return self._built

    def ensure_built(self) -> None:
        """(Re)build from the sealed rows; idempotent until invalidated."""
        if self._built:
            return
        rows = timeline_store.all_rows()
        self.clear()
        for rid, _created, payload in rows:
            text = _text_of(payload)
            if text:
                self._conn.execute(
                    "INSERT INTO fts(rowid, text) VALUES (?, ?)", (rid, text)
                )
        self._conn.commit()
        self._built = True
        logger.info("timeline index built: %d doc(s) from %d row(s)",
                    self._count(), len(rows))

    def invalidate(self) -> None:
        self._built = False

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── operations ───────────────────────────────────────────────────────

    def clear(self) -> None:
        self._conn.execute("DELETE FROM fts")
        self._conn.commit()

    def add(self, rowid: int, text: str) -> None:
        if not self._built:
            return
        if text:
            self._conn.execute(
                "INSERT INTO fts(rowid, text) VALUES (?, ?)", (rowid, text)
            )
            self._conn.commit()

    def remove(self, rowids: Iterable[int]) -> None:
        ids = [int(i) for i in rowids]
        if not ids:
            return
        self._conn.execute(
            f"DELETE FROM fts WHERE rowid IN ({','.join('?' * len(ids))})", ids
        )
        self._conn.commit()

    def search(self, query: str, limit: int = 10) -> list[tuple[int, str]]:
        """FTS5 match -> [(rowid, snippet)]; safe literal token quoting."""
        tokens = [t for t in _tokens(query)]
        if not tokens:
            return []
        match = " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        cur = self._conn.execute(
            "SELECT rowid, snippet(fts, 0, '[', ']', '...', 12) "
            "FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        )
        return [(int(r), str(s)) for r, s in cur.fetchall()]

    def _count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM fts").fetchone()
        return int(row[0]) if row else 0


def _tokens(query: str) -> list[str]:
    import re

    return re.findall(r"\w+", query or "")


def _text_of(payload: bytes) -> str:
    """Unseal a row payload and return its searchable text ('' on failure)."""
    try:
        data = json.loads(vault.open_envelope(payload))
        return str(data.get("text", ""))
    except Exception:
        logger.exception("timeline index: could not unseal a row")
        return ""


# ── module singleton ────────────────────────────────────────────────────────

_INDEX: TimelineIndex | None = None


def get_index() -> TimelineIndex | None:
    return _INDEX


def ensure_index() -> TimelineIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = TimelineIndex()
    return _INDEX


def close_index() -> None:
    global _INDEX
    if _INDEX is not None:
        _INDEX.close()
        _INDEX = None
