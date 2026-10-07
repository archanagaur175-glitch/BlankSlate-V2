"""Encrypted vault (Phase 3): DPAPI-sealed key, Fernet envelope, TTL purge."""

from blankslate.vault.core import (
    ENVELOPE_VERSION,
    VaultError,
    acquire_lock,
    get_key,
    is_envelope,
    key_path,
    open_envelope,
    rag_lock_path,
    read_bytes,
    read_text,
    recover_rag_db,
    release_lock,
    seal,
    write_bytes,
    write_text,
)

__all__ = [
    "ENVELOPE_VERSION",
    "VaultError",
    "acquire_lock",
    "get_key",
    "is_envelope",
    "key_path",
    "open_envelope",
    "rag_lock_path",
    "read_bytes",
    "read_text",
    "recover_rag_db",
    "release_lock",
    "seal",
    "write_bytes",
    "write_text",
]
