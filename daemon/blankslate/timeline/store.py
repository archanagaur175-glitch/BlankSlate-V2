"""Sealed-row store for timeline frames (design doc section 3).

Schema: (id, created_at, payload) — payload is a vault.seal() Fernet
envelope holding text + timestamp + process + window title. `created_at`
is the only plaintext column (needed for TTL purge; leaks "when", never
"what/where/who").

All disk I/O is sqlite (short-lived connections); this module contains no
raw file writes (hw3 zero-raw-write sweep stays green).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable


def db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "timeline.sqlite3"


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS timeline ("
        "id INTEGER PRIMARY KEY, "
        "created_at REAL NOT NULL, "
        "payload BLOB NOT NULL)"
    )
    conn.commit()
    return conn


def insert(payload: bytes, created_at: float | None = None,
           path: Path | str | None = None) -> int:
    ts = time.time() if created_at is None else float(created_at)
    conn = _connect(path)
    try:
        cur = conn.execute(
            "INSERT INTO timeline (created_at, payload) VALUES (?, ?)", (ts, payload)
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def all_rows(path: Path | str | None = None) -> list[tuple[int, float, bytes]]:
    conn = _connect(path)
    try:
        return [(int(i), float(c), bytes(p))
                for i, c, p in conn.execute(
                    "SELECT id, created_at, payload FROM timeline ORDER BY id")]
    finally:
        conn.close()


def get(rowid: int, path: Path | str | None = None) -> tuple[float, bytes] | None:
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT created_at, payload FROM timeline WHERE id = ?", (rowid,)
        ).fetchone()
        return (float(row[0]), bytes(row[1])) if row else None
    finally:
        conn.close()


def count(path: Path | str | None = None) -> int:
    conn = _connect(path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM timeline").fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def purge_older_than(cutoff: float,
                     path: Path | str | None = None) -> list[int]:
    """Delete rows created before `cutoff`; returns the deleted ids."""
    conn = _connect(path)
    try:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT id FROM timeline WHERE created_at < ?", (cutoff,))]
        if ids:
            conn.execute(
                f"DELETE FROM timeline WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            conn.commit()
        return ids
    finally:
        conn.close()
