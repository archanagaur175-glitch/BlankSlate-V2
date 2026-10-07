"""Long-term context persistence with real-time redaction.

Stores conversation history to disk as JSON, but ONLY after passing
through redact_text(). Any secret-shaped string is replaced with
[REDACTED:<type>] before it ever touches the filesystem.

Also provides a rolling-window digest for the short-term buffer.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from blankslate.config import DaemonConfig
from blankslate.guardrail.redact import redact_text
from blankslate.vault import core as vault

logger = logging.getLogger("blankslate.guardrail.store")


@dataclass
class PersistedRecord:
    role: str
    content: str  # already redacted
    timestamp: float
    source: str = "text"  # "text" | "voice" | "tool" | "summary"


class ContextStore:
    """Persists conversation history to disk, redacted.

    Thread-safe writes via a simple lock. Records are appended and
    flushed to disk on each write (small, local, personal build).
    """

    def __init__(self, cfg: DaemonConfig, data_dir: Path | None = None) -> None:
        self._base_dir = data_dir or Path(__file__).resolve().parent.parent.parent / "data"
        self._store_dir = self._base_dir
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._records_path = self._store_dir / "conversation_history.json"
        self._records: list[PersistedRecord] = []
        self._lock = __import__("threading").Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._records_path.exists():
                on_disk = self._records_path.read_bytes()
                was_envelope = vault.is_envelope(on_disk)
                raw = vault.open_envelope(on_disk) if was_envelope else on_disk
                data = json.loads(raw.decode("utf-8"))
                self._records = [
                    PersistedRecord(
                        role=r.get("role", ""),
                        content=r.get("content", ""),
                        timestamp=r.get("timestamp", 0.0),
                        source=r.get("source", "text"),
                    )
                    for r in data
                ]
                logger.info("Loaded %d persisted records from %s", len(self._records), self._records_path)
                if not was_envelope:
                    # One-time migration: seal legacy plaintext on first load.
                    # Failure here must NOT clear the just-loaded records —
                    # the file stays plaintext and the next append seals it.
                    try:
                        self._flush_locked()
                        logger.info("conversation history migrated to vault envelope: %s", self._records_path)
                    except Exception as exc:
                        logger.error("history migration seal failed (will retry on next append): %s", exc)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError, vault.VaultError) as exc:
            logger.error("Failed to load history from %s: %s", self._records_path, exc)
            self._records = []

    def append(self, role: str, content: str, source: str = "text") -> None:
        """Append a redacted record to disk."""
        redacted = redact_text(content)
        # Never persist an unredacted string that needs redaction
        with self._lock:
            self._records.append(
                PersistedRecord(role=role, content=redacted, timestamp=time.time(), source=source)
            )
            self._flush_locked()
        logger.info("Persisted turn: role=%s source=%s (redacted=%s)", role, source, redacted != content)

    def recent(self, limit: int = 50) -> list[PersistedRecord]:
        with self._lock:
            return list(self._records[-limit:])

    def _flush_locked(self) -> None:
        data = [
            {
                "role": r.role,
                "content": r.content,
                "timestamp": r.timestamp,
                "source": r.source,
            }
            for r in self._records
        ]
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        vault.write_bytes(self._records_path, payload)

    def purge_older_than(self, cutoff: float) -> int:
        """Drop records older than cutoff (epoch seconds). Returns removed count."""
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r.timestamp >= cutoff]
            removed = before - len(self._records)
            if removed:
                self._flush_locked()
        return removed

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._records)
