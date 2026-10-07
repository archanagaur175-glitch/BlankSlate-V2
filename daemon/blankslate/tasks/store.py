"""Encrypted task store (Phase 1) — SQLite rows sealed through the vault layer.

On-disk model (D3-consistent, same strong pattern as the Phase 4 timeline):
a plain SQLite file with only `id` + `payload` columns, where `payload` is a
vault envelope (Fernet) wrapping the full task JSON. No plaintext title,
due date, or note content ever touches disk — and unlike the RAG db there
is no decrypt-open window: rows are sealed on write, opened per-row on load.

Reminder lifecycle: a task created with a due time gets remind=True; the
monitor package's ReminderPoller fires each due task exactly once
(`reminded_at` marked inside the sealed payload) over the existing IPC
broadcast channel. Tasks are EXEMPT from vault auto-purge (D7, macro-like:
user data with an explicit lifecycle, not transient content).

All mutations happen on the daemon's asyncio loop thread; the in-RAM
cache is the source of truth, persisted through vault.seal() on change.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from blankslate.vault import core as vault

logger = logging.getLogger("blankslate.tasks.store")


@dataclass
class Task:
    id: str
    title: str
    created_at: float
    due_at: float | None = None
    completed_at: float | None = None
    source: str = "text"
    remind: bool = False
    reminded_at: float | None = None

    @property
    def is_completed(self) -> bool:
        return self.completed_at is not None

    def to_public(self, now: float | None = None) -> dict[str, Any]:
        """Flat dict for handler replies (local-time display strings)."""
        t = self
        out: dict[str, Any] = {
            "id": t.id,
            "title": t.title,
            "completed": t.is_completed,
            "source": t.source,
            "remind": t.remind,
        }
        if t.due_at is not None:
            out["due_local"] = _fmt_due(t.due_at)
            out["due_at"] = t.due_at
        if t.completed_at is not None:
            out["completed_local"] = _fmt_due(t.completed_at)
        if now is not None:
            out["overdue"] = bool(
                t.due_at is not None and t.due_at < now and not t.is_completed
            )
        return out


def _fmt_due(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def default_db_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "tasks.sqlite3"


class TaskStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path is not None else default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id TEXT PRIMARY KEY, "
            "payload TEXT NOT NULL)"
        )
        self._db.commit()
        self._tasks: dict[str, Task] = {}
        self._load()

    # ── persistence (payload always sealed through the vault layer) ─────────

    def _load(self) -> None:
        for tid, payload in self._db.execute("SELECT id, payload FROM tasks"):
            try:
                raw = vault.open_envelope(payload.encode("ascii"))
                data = json.loads(raw)
                known = {f for f in Task.__dataclass_fields__}
                self._tasks[tid] = Task(**{k: v for k, v in data.items() if k in known})
            except Exception:
                logger.exception("task row unreadable, skipped: %s", tid)

    def _persist(self, task: Task) -> None:
        payload = vault.seal(json.dumps(asdict(task), separators=(",", ":")).encode("utf-8"))
        self._db.execute(
            "INSERT OR REPLACE INTO tasks (id, payload) VALUES (?, ?)",
            (task.id, payload.decode("ascii")),
        )
        self._db.commit()

    # ── CRUD ────────────────────────────────────────────────────────────────

    def add(
        self,
        title: str,
        due_at: float | None = None,
        source: str = "text",
        remind: bool | None = None,
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:12],
            title=title,
            created_at=time.time(),
            due_at=due_at,
            source=source,
            remind=bool(due_at) if remind is None else bool(remind),
        )
        self._tasks[task.id] = task
        self._persist(task)
        logger.info("task added: %s due=%s", task.id, task.due_at)
        return task

    def all(self) -> list[Task]:
        return sorted(
            self._tasks.values(),
            key=lambda t: (t.due_at is None, t.due_at or t.created_at),
        )

    def find(self, ref: str) -> Task | None:
        """Resolve a task by id (exact), exact title (ci), or unique substring.

        Substring matches run BOTH directions: handlers strip command
        phrasing imperfectly ("complete the task call Ned" → ref
        "the task call Ned"), so a title contained in the ref must also hit.
        """
        ref = str(ref).strip()
        if not ref:
            return None
        if ref in self._tasks:
            return self._tasks[ref]
        low = ref.lower()
        for t in self._tasks.values():
            if t.title.lower() == low:
                return t
        hits = [
            t for t in self._tasks.values()
            if low in t.title.lower() or t.title.lower() in low
        ]
        if len(hits) == 1:
            return hits[0]
        return None

    def complete(self, ref: str) -> Task | None:
        t = self.find(ref)
        if t is None or t.is_completed:
            return t
        t.completed_at = time.time()
        self._persist(t)
        logger.info("task completed: %s", t.id)
        return t

    def delete(self, ref: str) -> bool:
        t = self.find(ref)
        if t is None:
            return False
        del self._tasks[t.id]
        self._db.execute("DELETE FROM tasks WHERE id = ?", (t.id,))
        self._db.commit()
        logger.info("task deleted: %s", t.id)
        return True

    # ── reminder support ────────────────────────────────────────────────────

    def due_tasks(self, now: float | None = None) -> list[Task]:
        now = time.time() if now is None else now
        return [
            t
            for t in self._tasks.values()
            if t.remind
            and not t.is_completed
            and t.reminded_at is None
            and t.due_at is not None
            and t.due_at <= now
        ]

    def mark_reminded(self, task_id: str) -> None:
        t = self._tasks.get(task_id)
        if t is None or t.reminded_at is not None:
            return
        t.reminded_at = time.time()
        self._persist(t)

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            logger.exception("task store close failed")


# ── module singleton (daemon wiring: get_store at startup, close in finally) ─

_STORE: TaskStore | None = None


def get_store() -> TaskStore:
    global _STORE
    if _STORE is None:
        _STORE = TaskStore()
        logger.info("task store: open %s", _STORE._path)
    return _STORE


def close_store() -> None:
    global _STORE
    if _STORE is not None:
        try:
            _STORE.close()
        finally:
            _STORE = None
