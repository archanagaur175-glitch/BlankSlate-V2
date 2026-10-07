"""Encrypted vault core (Phase 3) — local DPAPI key + Fernet envelope.

At-rest format for every vault-managed file: UTF-8 JSON envelope
    {"v": 1, "ts": <epoch seconds>, "ct": "<fernet token>"}

Key: data/vault.key.dpapi — one 32-byte Fernet key, generated on first
use and sealed with Windows DPAPI (user scope). The key never leaves the
machine, is never sent anywhere, and is never logged. Offline by
construction: this module imports nothing network-related.

Honest limitations (stated plainly, no overclaiming):
- The RAG SQLite file cannot stay encrypted while open: it is decrypted
  on open, encrypted on close. The lock file
  data/rag_memory.sqlite3.lock marks that plaintext window;
  recover_rag_db() runs FIRST at daemon startup, clears stale locks and
  re-encrypts crash leftovers before any other startup work.
- Purge deletes files/rows; there is no multi-pass secure-deletion
  claim. The protection model is encryption-at-rest + short TTL.

Window/lock invariants:
    acquire_lock() BEFORE decrypt-on-open, release_lock() AFTER
    encrypt-on-close — a crash inside the window leaves
    (plaintext + lock), which recovery detects and fixes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("blankslate.vault")

ENVELOPE_VERSION = 1


class VaultError(RuntimeError):
    """Envelope parse/encrypt/decrypt failure."""


_key_cache: bytes | None = None
_fernet_cache: Fernet | None = None


def key_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "vault.key.dpapi"


def get_key() -> bytes:
    """Return the process-cached Fernet key, creating it on first use."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache

    import win32crypt

    path = key_path()
    if path.exists():
        blob = path.read_bytes()
        try:
            key = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]
        except Exception as exc:  # DPAPI failure = wrong user/machine
            raise VaultError(f"vault key unprotect failed: {exc}") from exc
        if not isinstance(key, bytes) or len(key) != 44:
            raise VaultError(f"vault key has unexpected length {len(key)!r}")
    else:
        key = Fernet.generate_key()
        blob = win32crypt.CryptProtectData(
            key, "BlankSlate vault key", None, None, None, 0
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        logger.info("vault key created: %s", path)
    _key_cache = key
    return key


def _fernet() -> Fernet:
    global _fernet_cache
    if _fernet_cache is None:
        _fernet_cache = Fernet(get_key())
    return _fernet_cache


def is_envelope(raw: bytes) -> bool:
    """Cheap structural test: is this on-disk blob a vault envelope?"""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return False
    return (
        isinstance(obj, dict)
        and obj.get("v") == ENVELOPE_VERSION
        and isinstance(obj.get("ct"), str)
        and isinstance(obj.get("ts"), (int, float))
    )


def seal(raw: bytes) -> bytes:
    """Encrypt bytes into the on-disk envelope form."""
    token = _fernet().encrypt(raw)
    env = {"v": ENVELOPE_VERSION, "ts": time.time(), "ct": token.decode("ascii")}
    return json.dumps(env, separators=(",", ":")).encode("utf-8")


def open_envelope(raw: bytes) -> bytes:
    """Decrypt an envelope back to original bytes. Raises VaultError."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise VaultError(f"vault envelope parse failed: {exc}") from exc
    if not (
        isinstance(obj, dict)
        and obj.get("v") == ENVELOPE_VERSION
        and isinstance(obj.get("ct"), str)
    ):
        raise VaultError("not a vault envelope")
    try:
        return _fernet().decrypt(obj["ct"].encode("ascii"))
    except InvalidToken as exc:
        raise VaultError("vault envelope decrypt failed (wrong key or corrupt)") from exc


def write_bytes(path: Path | str, raw: bytes) -> None:
    """Encrypt-and-write atomically (tmp + os.replace)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(seal(raw))
    os.replace(tmp, path)


def read_bytes(path: Path | str) -> bytes:
    """Read bytes, decrypting envelopes; legacy plaintext passes through."""
    raw = Path(path).read_bytes()
    return open_envelope(raw) if is_envelope(raw) else raw


def write_text(path: Path | str, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def read_text(path: Path | str) -> str:
    return read_bytes(path).decode("utf-8")


# --- RAG SQLite open/close window -----------------------------------------

def rag_lock_path(db_path: Path | str) -> Path:
    db = Path(db_path)
    return db.with_name(db.name + ".lock")


def acquire_lock(lock_path: Path | str) -> None:
    Path(lock_path).write_text(
        f"pid={os.getpid()} ts={time.time():.3f}", encoding="utf-8"
    )


def release_lock(lock_path: Path | str) -> None:
    try:
        Path(lock_path).unlink()
    except FileNotFoundError:
        pass


def recover_rag_db(db_path: Path | str) -> dict[str, str]:
    """Amendment A startup recovery: run as the FIRST statement of _run().

    1. Clear any stale lock (previous crash left the open-window marker).
    2. If the DB file exists but is not an envelope (crash left plaintext),
       re-encrypt it in place before anything else starts.
    """
    db = Path(db_path)
    lock = rag_lock_path(db)
    result = {"lock": "none", "db": "absent"}
    if lock.exists():
        try:
            lock.unlink()
            result["lock"] = "cleared"
            logger.warning("STALE LOCK CLEARED: %s", lock)
        except OSError:
            result["lock"] = "stale"
    if db.exists():
        raw = db.read_bytes()
        if is_envelope(raw):
            result["db"] = "encrypted"
        else:
            write_bytes(db, raw)
            logger.warning("RAG DB RE-ENCRYPTED (crash recovery): %s", db)
            result["db"] = "re_encrypted"
    return result
