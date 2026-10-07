"""bs-sandbox credential load — setup-script contract (design doc §3.4).

Cred: daemon/data/sandbox_user.cred — DPAPI CurrentUser blob, entropy
BlankSlate.v2.sandbox.v1, plaintext = account password.
Meta: daemon/data/sandbox_user.json — {user, python, created}.
Missing/unreadable setup raises RuntimeError with the honest §6 message;
nothing ever spawns on this path (server replies error, no result).
"""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes
from pathlib import Path

ENTROPY = b"BlankSlate.v2.sandbox.v1"
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CRED_FILE = _DATA_DIR / "sandbox_user.cred"
META_FILE = _DATA_DIR / "sandbox_user.json"

SETUP_REQUIRED = (
    "sandbox setup required (scripts/sandbox_user_setup.ps1 -Mode enable)"
)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", wintypes.LPVOID)]


def load() -> tuple[str, str, str]:
    """Return (user, password, python_path). Raises RuntimeError if unready."""
    if not CRED_FILE.exists() or not META_FILE.exists():
        raise RuntimeError(SETUP_REQUIRED)
    try:
        meta = json.loads(META_FILE.read_text(encoding="utf-8-sig"))
        blob = CRED_FILE.read_bytes()
        password = _unprotect(blob)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"sandbox credential unreadable: {exc}") from exc
    if not password:
        raise RuntimeError("sandbox credential unreadable (DPAPI unprotect failed)")
    user = str(meta.get("user", "bs-sandbox"))
    python = str(meta.get("python") or "")
    if not python:
        import sys

        python = sys.executable
    return user, password, python


def available() -> bool:
    return CRED_FILE.exists() and META_FILE.exists()


def _unprotect(blob: bytes) -> str:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    cry = ctypes.WinDLL("crypt32", use_last_error=True)
    in_blob = _DataBlob(len(blob), ctypes.cast(ctypes.c_char_p(blob), ctypes.c_void_p))
    ent = _DataBlob(len(ENTROPY), ctypes.cast(ctypes.c_char_p(ENTROPY), ctypes.c_void_p))
    out_blob = _DataBlob()
    if not cry.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(ent), None, None, 0,
        ctypes.byref(out_blob),
    ):
        return ""
    raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    k32.LocalFree(out_blob.pbData)
    return raw.decode("utf-8")
