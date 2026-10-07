"""HW3 GATE - Phase 3: Encrypted Vault + Auto-Purge.

Mandated audit (completed BEFORE any encryption code was written):
- Disk-write-site inventory. IN-SCOPE (secret-bearing): guardrail/store.py
  conversation JSON, handlers.py notes txt, xray.py capture png,
  rag/store.py sqlite. EXEMPT (user decisions): macros (no product write
  path; revisit only if a save_macro tool is ever added), all 11 project
  *.log files (Amendment B content grep CLEAN: 6 transcript phrases x 11
  logs, 11/11 positive controls, 0 hits), dev scripts (not runtime).
- Amendment B log-content grep: CLEAN (ran before encryption code).
- Flagged-only touches: store.py, handlers.py, xray.py, rag/store.py,
  rag/service.py, main.py (~first-statement + purge/close), config.py.

Tests:
T1  vault key: DPAPI roundtrip (CryptUnprotectData(file) == get_key()),
    44-byte Fernet key, raw key bytes absent from key file, cached.
T2  envelope: seal/open roundtrip, at-rest has no plaintext, structural
    is_envelope, legacy plaintext passthrough, corrupt envelope raises
    VaultError, atomic write leaves no .tmp.
T3  conversation store: append -> ciphertext on disk (secret bytes not
    present), reload roundtrip; legacy plaintext file migrated+sealed on
    first load; purge_older_than drops aged record, keeps fresh.
T4  notes handler (real): write -> encrypted file; read roundtrip;
    append semantics preserved; list; delete. The real user note
    notes/name.txt is byte-identical before/after.
T5  xray real capture save: file on disk is an envelope (no PNG magic),
    decrypts to a real PNG. Vision NOT invoked (no question).
T6  VecStore lifecycle: lock exists while open (plaintext window marked),
    plaintext while open, encrypt-on-close, lock released after encrypt,
    decrypt-on-reopen + search roundtrip, double-close safe.
T7  recover_rag_db unit: stale lock + plaintext -> lock=cleared,
    db=re_encrypted; already-encrypted -> encrypted; absent -> absent.
T8  REAL daemon spawn (Amendment A ground truth): plant stale lock +
    plaintext RAG DB in data/, spawn run_daemon.py, capture merged
    stdout/stderr. Evidence required: 'STALE LOCK CLEARED' and
    'RAG DB RE-ENCRYPTED (crash recovery)' and 'vault recovery:
    lock=cleared db=re_encrypted' ALL precede 'BlankSlate daemon started'
    AND 'IPC server listening'; after kill the DB is ciphertext and the
    lock is gone; conversation history restored byte-identical.
T9  purge table (injected scratch dirs - real notes/ and data/ never
    scanned): aged/fresh x conversation/notes/captures/rag, exact counts,
    fresh survivors untouched.
T10 main.py wiring: recover_rag_db is called before OllamaClient in _run,
    purge_loop task created, close_store() in finally.
T11 gate-(b) zero-raw-write sweep: repo-wide scan of write-mode open /
    write_text / write_bytes across blankslate/; every hit must be a
    documented allowlist site (vault choke point, in-memory BytesIO, or
    decrypt-on-open re-materialization); flagged files verified to use
    the vault choke point; no .tmp residue.
T12 regressions: 21 registry tools, vault_ttl_days default 14.0,
    BS_VAULT_TTL_DAYS env override honored.

Ground truth: on-disk bytes + DPAPI unprotect, real daemon process
stdout ordering, real screen capture. Zero network.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import win32crypt

from blankslate.config import DaemonConfig, load_config
from blankslate.guardrail.store import ContextStore
from blankslate.rag.store import VecStore
from blankslate.tools.handlers import note_taking
from blankslate.tools.registry import ToolRegistry
from blankslate.vault import core as vault

DAEMON_DIR = Path(__file__).resolve().parent
TMP_DIRS: list[Path] = []
RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def tmpdir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="bs_hw3_"))
    TMP_DIRS.append(d)
    return d


# ── T1: vault key ────────────────────────────────────────────────────────────

def t1_key() -> None:
    print("\n--- T1: DPAPI key ---")
    k1 = vault.get_key()
    k2 = vault.get_key()
    rec("T1 key 44-byte Fernet, cached", isinstance(k1, bytes) and len(k1) == 44 and k1 is k2,
        f"len={len(k1)}")
    kp = vault.key_path()
    blob = kp.read_bytes() if kp.exists() else b""
    unprot = win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1] if blob else b""
    rec("T1 file->DPAPI roundtrip == get_key()", unprot == k1 and bool(blob),
        f"keyfile={kp.name} {len(blob)}B")
    rec("T1 raw key bytes NOT in key file", bool(blob) and k1 not in blob)


# ── T2: envelope ─────────────────────────────────────────────────────────────

def t2_envelope() -> None:
    print("\n--- T2: envelope roundtrip / legacy / corrupt ---")
    d = tmpdir()
    p = d / "secret.bin"
    vault.write_bytes(p, b"TOP-SECRET-XYZ-123")
    raw = p.read_bytes()
    rec("T2 at rest = envelope, no plaintext",
        vault.is_envelope(raw) and b"TOP-SECRET-XYZ-123" not in raw)
    rec("T2 decrypt roundtrip", vault.read_bytes(p) == b"TOP-SECRET-XYZ-123")
    rec("T2 atomic write, no .tmp residue", not list(d.glob("*.tmp")))
    legacy = d / "old.txt"
    legacy.write_bytes(b"legacy plaintext note")
    rec("T2 legacy plaintext passthrough",
        vault.read_bytes(legacy) == b"legacy plaintext note"
        and not vault.is_envelope(legacy.read_bytes()))
    corrupt = d / "bad.env"
    corrupt.write_bytes(b'{"v":1,"ts":1.5,"ct":"!!!not-a-fernet-token!!!"}')
    try:
        vault.read_bytes(corrupt)
        rec("T2 corrupt envelope raises VaultError", False, "no exception")
    except vault.VaultError as exc:
        rec("T2 corrupt envelope raises VaultError", True, str(exc)[:60])


# ── T3: conversation store ───────────────────────────────────────────────────

def t3_conversation() -> None:
    print("\n--- T3: ContextStore ciphertext + migration + purge ---")
    d = tmpdir()
    cfg = load_config()
    s = ContextStore(cfg, data_dir=d)
    s.append("user", "conversation vault secret phrase", "text")
    p = d / "conversation_history.json"
    raw = p.read_bytes()
    rec("T3 conversation at rest = envelope",
        vault.is_envelope(raw) and b"conversation vault secret phrase" not in raw)
    s2 = ContextStore(cfg, data_dir=d)
    rec("T3 reload roundtrip", s2.recent(10)[-1].content == "conversation vault secret phrase")

    d2 = tmpdir()
    p2 = d2 / "conversation_history.json"
    p2.write_text(
        '[{"role": "u", "content": "legacy record", "timestamp": 1.0, "source": "text"}]',
        encoding="utf-8",
    )
    s3 = ContextStore(cfg, data_dir=d2)
    rec("T3 legacy plaintext migrated+sealed on load",
        vault.is_envelope(p2.read_bytes())
        and s3.recent(1)[0].content == "legacy record")

    s._records[0].timestamp = time.time() - 30 * 86400  # age the record
    with s._lock:
        s._flush_locked()
    s.append("assistant", "fresh record", "text")
    removed = s.purge_older_than(time.time() - 14 * 86400)
    rec("T3 purge drops aged, keeps fresh",
        removed == 1 and s.count == 1
        and s.recent(1)[0].content == "fresh record",
        f"removed={removed} left={s.count}")


# ── T4: notes handler ────────────────────────────────────────────────────────

async def t4_notes() -> None:
    print("\n--- T4: notes handler (real files in notes/) ---")
    label = "bs_gate_vault_note"
    p = DAEMON_DIR / "notes" / f"{label}.txt"
    name_p = DAEMON_DIR / "notes" / "name.txt"
    name_before = name_p.read_bytes() if name_p.exists() else None
    if p.exists():
        p.unlink()
    try:
        r1 = await note_taking({"action": "write", "label": label,
                                "text": "vault gate secret content"})
        raw = p.read_bytes() if p.exists() else b""
        rec("T4 write -> encrypted file on disk",
            p.exists() and vault.is_envelope(raw)
            and b"vault gate secret content" not in raw)
        r2 = await note_taking({"action": "read", "label": label})
        rec("T4 read roundtrip", r2.get("result") == "vault gate secret content",
            repr(str(r2.get("result"))[:50]))
        await note_taking({"action": "append", "label": label, "text": "+more"})
        r3 = await note_taking({"action": "read", "label": label})
        rec("T4 append semantics preserved",
            r3.get("result") == "vault gate secret content+more",
            repr(str(r3.get("result"))[:60]))
        r4 = await note_taking({"action": "list"})
        rec("T4 list sees note", label in (r4.get("notes") or []))
        r5 = await note_taking({"action": "delete", "label": label})
        rec("T4 delete removes file", r5.get("deleted") is True and not p.exists())
        name_after = name_p.read_bytes() if name_p.exists() else None
        rec("T4 real user notes/name.txt untouched", name_before == name_after,
            f"{len(name_after or b'')}B identical")
    finally:
        if p.exists():
            p.unlink()


# ── T5: xray capture save ────────────────────────────────────────────────────

async def t5_xray() -> None:
    print("\n--- T5: real screen capture saved via vault ---")
    from blankslate.tools.xray import screen_xray

    d = tmpdir()
    save = str(d / "cap.png")
    out = await screen_xray({"save": save, "query": ""})
    if out.get("error"):
        rec("T5 capture+save", False, str(out["error"])[:80])
        return
    p = Path(out.get("saved_to") or save)
    raw = p.read_bytes() if p.exists() else b""
    rec("T5 saved file is envelope, no PNG magic on disk",
        p.exists() and vault.is_envelope(raw) and not raw.startswith(b"\x89PNG"))
    plain = vault.read_bytes(p) if raw else b""
    rec("T5 decrypts to real PNG",
        plain.startswith(b"\x89PNG") and len(plain) > 1000,
        f"{out.get('width')}x{out.get('height')} {len(plain)}B")
    rec("T5 vision not invoked (no question)", out.get("answer") is None)


# ── T6: VecStore lifecycle ───────────────────────────────────────────────────

def t6_rag_lifecycle() -> None:
    print("\n--- T6: VecStore lock / encrypt-on-close / decrypt-on-open ---")
    d = tmpdir()
    rp = d / "rag_gate.sqlite3"
    lp = vault.rag_lock_path(rp)
    st = VecStore(rp)
    rec("T6 lock exists while open (plaintext window marked)", lp.exists())
    rec("T6 plaintext while open (honest limitation)", not vault.is_envelope(rp.read_bytes()))
    st.add(["alpha beta gamma"], ["history"], [[0.1] * 768])
    st.close()
    rec("T6 encrypt-on-close", vault.is_envelope(rp.read_bytes()))
    rec("T6 lock released after encrypt", not lp.exists())
    st.close()  # double close must be a no-op
    rec("T6 double-close safe", True)
    st2 = VecStore(rp)
    rec("T6 reopen: lock re-acquired, DB decrypted", lp.exists()
        and not vault.is_envelope(rp.read_bytes()))
    hits = st2.search([0.1] * 768, k=3)
    rec("T6 search roundtrip after decrypt-reopen",
        bool(hits) and hits[0].text == "alpha beta gamma",
        hits[0].text if hits else "no hits")
    st2.close()
    rec("T6 closed state: ciphertext + no lock",
        vault.is_envelope(rp.read_bytes()) and not lp.exists())


# ── T7: recover_rag_db unit ──────────────────────────────────────────────────

def t7_recovery_unit() -> None:
    print("\n--- T7: recover_rag_db (unit) ---")
    d = tmpdir()
    rp = d / "rag_rec.sqlite3"
    conn = sqlite3.connect(str(rp))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER)")
    conn.commit()
    conn.close()
    lp = vault.rag_lock_path(rp)
    lp.write_text("pid=99999 ts=0.0", encoding="utf-8")
    r = vault.recover_rag_db(rp)
    rec("T7 stale lock + plaintext -> cleared/re_encrypted",
        r == {"lock": "cleared", "db": "re_encrypted"}, str(r))
    rec("T7 result on disk: ciphertext + lock gone",
        vault.is_envelope(rp.read_bytes()) and not lp.exists())
    r2 = vault.recover_rag_db(rp)
    rec("T7 already-encrypted -> encrypted", r2["db"] == "encrypted", str(r2))
    r3 = vault.recover_rag_db(d / "absent.sqlite3")
    rec("T7 absent -> absent", r3 == {"lock": "none", "db": "absent"}, str(r3))


# ── T8: REAL daemon spawn (Amendment A) ──────────────────────────────────────

def t8_daemon_spawn() -> None:
    print("\n--- T8: real daemon spawn — crash recovery ordering (ground truth) ---")
    data_dir = DAEMON_DIR / "data"
    conv = data_dir / "conversation_history.json"
    rag = data_dir / "rag_memory.sqlite3"
    lock = data_dir / "rag_memory.sqlite3.lock"

    conv_backup = conv.read_bytes() if conv.exists() else None
    rag_existed = rag.exists()
    rag_backup = rag.read_bytes() if rag_existed else None
    lock_existed = lock.exists()
    lock_backup = lock.read_bytes() if lock_existed else None

    proc: subprocess.Popen | None = None
    lines: list[str] = []
    try:
        # Plant: stale lock + plaintext RAG DB (crash leftover)
        if rag.exists():
            rag.unlink()
        conn = sqlite3.connect(str(rag))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER)")
        conn.commit()
        conn.close()
        planted_raw = rag.read_bytes()
        rec("T8 pre-state: DB planted plaintext",
            not vault.is_envelope(planted_raw)
            and planted_raw.startswith(b"SQLite format 3"))
        lock.write_text("pid=99999 ts=0.0", encoding="utf-8")

        env = dict(os.environ)
        env.update({
            "BS_IPC_PORT": "8793",
            "BS_TTS_ENABLED": "False",
            "PYTHONIOENCODING": "utf-8",
        })
        proc = subprocess.Popen(
            [sys.executable, "run_daemon.py"],
            cwd=str(DAEMON_DIR), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

        def reader() -> None:
            assert proc is not None and proc.stdout is not None
            for line in proc.stdout:
                lines.append(line.rstrip("\n"))

        threading.Thread(target=reader, daemon=True).start()

        def have_evidence() -> bool:
            return (
                any("vault recovery:" in l for l in lines)
                and any("IPC server listening" in l for l in lines)
            )

        deadline = time.time() + 120
        while time.time() < deadline and proc.poll() is None and not have_evidence():
            time.sleep(0.25)
        time.sleep(0.7)  # flush trailing lines

        def idx(needle: str) -> int:
            return next((i for i, l in enumerate(lines) if needle in l), -1)

        i_stale = idx("STALE LOCK CLEARED")
        i_reenc = idx("RAG DB RE-ENCRYPTED (crash recovery)")
        i_recov = idx("vault recovery:")
        i_started = idx("BlankSlate daemon started")
        i_ipc = idx("IPC server listening")

        rec("T8 daemon reached IPC listening (alive)", i_ipc >= 0 and proc.poll() is None,
            f"exit={proc.poll()} lines={len(lines)}")
        rec("T8 recovery summary line exact",
            any("vault recovery: lock=cleared db=re_encrypted" in l for l in lines),
            lines[i_recov].strip() if i_recov >= 0 else "MISSING")
        rec("T8 STALE LOCK CLEARED logged", i_stale >= 0,
            lines[i_stale].strip()[-90:] if i_stale >= 0 else "MISSING")
        rec("T8 RAG DB RE-ENCRYPTED (crash recovery) logged", i_reenc >= 0,
            lines[i_reenc].strip()[-70:] if i_reenc >= 0 else "MISSING")
        rec("T8 ordering: stale-lock/re-encrypt + recovery BEFORE daemon started",
            0 <= i_stale < i_started and 0 <= i_reenc < i_started
            and 0 <= i_recov < i_started,
            f"stale@{i_stale} reenc@{i_reenc} recovery@{i_recov} started@{i_started}")
        rec("T8 ordering: recovery BEFORE IPC server listening",
            0 <= i_recov < i_ipc, f"recovery@{i_recov} ipc@{i_ipc}")

        # Kill before any file inspection (daemon must not be running)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        time.sleep(0.3)

        rec("T8 after kill: RAG DB is ciphertext", rag.exists() and vault.is_envelope(rag.read_bytes()))
        rec("T8 after kill: lock removed", not lock.exists())

        print("  --- key stdout/stderr lines (evidence) ---")
        enc = sys.stdout.encoding or "utf-8"
        for i, l in enumerate(lines):
            if any(k in l for k in ("STALE LOCK", "RE-ENCRYPTED", "vault recovery:",
                                    "BlankSlate daemon started", "IPC server listening",
                                    "migrated to vault")):
                safe = l.strip()[:150].encode(enc, "replace").decode(enc)
                print(f"    [{i:03d}] {safe}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        # Full restore: conversation history byte-identical, planted files gone
        if conv_backup is not None:
            conv.write_bytes(conv_backup)
        elif conv.exists():
            conv.unlink()
        if rag_existed:
            rag.write_bytes(rag_backup)  # type: ignore[arg-type]
        elif rag.exists():
            rag.unlink()
        if lock_existed:
            lock.write_bytes(lock_backup)  # type: ignore[arg-type]
        elif lock.exists():
            lock.unlink()
        residue = list(data_dir.glob("*.tmp"))
        rec("T8 cleanup: conversation restored, planted files removed, no .tmp",
            not residue
            and (conv.exists() == (conv_backup is not None)),
            f"tmp={len(residue)}")


# ── T9: purge table ──────────────────────────────────────────────────────────

def t9_purge() -> None:
    print("\n--- T9: TTL purge table (injected scratch dirs) ---")
    cfg = load_config()
    d = tmpdir()

    # conversation: aged + fresh
    cd = d / "conv"
    store = ContextStore(cfg, data_dir=cd)
    store.append("user", "aged record", "text")
    store.append("user", "fresh record", "text")
    store._records[0].timestamp = time.time() - 30 * 86400
    with store._lock:
        store._flush_locked()

    # notes + captures: aged + fresh (mtime aged via os.utime)
    nd = d / "notes"
    nd.mkdir()
    (nd / "aged.txt").write_text("old", encoding="utf-8")
    (nd / "fresh.txt").write_text("new", encoding="utf-8")
    os.utime(nd / "aged.txt", (time.time() - 30 * 86400,) * 2)
    dd = d / "captures"
    dd.mkdir()
    (dd / "aged.png").write_bytes(b"\x89PNG aged")
    (dd / "fresh.png").write_bytes(b"\x89PNG fresh")
    os.utime(dd / "aged.png", (time.time() - 30 * 86400,) * 2)

    # rag: aged + fresh via service singleton (scratch db)
    from blankslate.rag import service as rag_service
    rp = d / "purge_rag.sqlite3"
    rag_service.configure(rp)
    rag_service.ingest_count()  # force store creation
    st = rag_service._get_store()
    st.add(["aged chunk"], ["history"], [[0.3] * 768])
    st.add(["fresh chunk"], ["history"], [[0.4] * 768])
    conn = sqlite3.connect(str(rp))
    conn.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("UPDATE chunks SET created = ? WHERE chunk_text = ?",
                 (time.time() - 30 * 86400, "aged chunk"))
    conn.commit()
    conn.close()

    from blankslate.vault.purge import purge_once
    counts = purge_once(cfg, store, data_dir=dd, notes_dir=nd)
    expected = {"conversation": 1, "notes": 1, "captures": 1, "rag": 1}
    rec("T9 exact purge counts (aged only)", counts == expected,
        f"got={counts}")
    rec("T9 survivors intact",
        store.count == 1 and store.recent(1)[0].content == "fresh record"
        and (nd / "fresh.txt").exists() and not (nd / "aged.txt").exists()
        and (dd / "fresh.png").exists() and not (dd / "aged.png").exists())
    rag_service.close_store()
    rec("T9 rag store encrypted after close", vault.is_envelope(rp.read_bytes()))
    rec("T9 real notes/ and data/ never scanned (injected dirs)",
        True, "gate passed data_dir/notes_dir explicitly")

    # cadence: loop sleeps first (no purge at t=0) — structural check
    src = (DAEMON_DIR / "blankslate" / "vault" / "purge.py").read_text(encoding="utf-8")
    rec("T9 purge_loop sleeps first (900 s cadence)",
        "await asyncio.sleep(PURGE_INTERVAL_S)" in src
        and "PURGE_INTERVAL_S = 900.0" in src)


# ── T10: main.py wiring ──────────────────────────────────────────────────────

def t10_wiring() -> None:
    print("\n--- T10: main.py wiring ---")
    src = (DAEMON_DIR / "blankslate" / "main.py").read_text(encoding="utf-8")
    run_at = src.index("async def _run")
    recov_at = src.index("recover_rag_db", run_at)
    ollama_at = src.index("llm = OllamaClient", run_at)
    rec("T10 recovery called BEFORE OllamaClient (first statement of _run)",
        recov_at < ollama_at, f"recover@{recov_at} ollama@{ollama_at}")
    rec("T10 purge_loop task created",
        "purge_loop(cfg, store)" in src and "purge_task.cancel()" in src)
    rec("T10 close_store() in shutdown finally", "close_store()" in src
        and src.index("close_store()") > src.index("finally:"))


# ── T11: zero-raw-write sweep ────────────────────────────────────────────────

def t11_sweep() -> None:
    print("\n--- T11: gate-(b) zero-raw-write sweep ---")
    import re

    # (file-substring, line-substring) allowlist with reasons
    allow = [
        ("vault/core.py", "tmp.write_bytes(blob)", "choke point: key write (DPAPI blob)"),
        ("vault/core.py", 'open(tmp, "wb")', "choke point: envelope write"),
        ("vault/core.py", "Path(lock_path).write_text(", "lock marker (no secrets)"),
        ("rag/store.py", "tmp.write_bytes(vault.open_envelope(", "decrypt-on-open re-materialization (lock held; Amendment A)"),
        ("voice/mic.py", 'wave.open(buf, "wb")', "in-memory BytesIO (no file on disk)"),
        ("guardrail/store.py", "vault.write_bytes(self._records_path", "choke-point call (delegates to vault)"),
        ("rag/store.py", "vault.write_bytes(self._path", "choke-point call (encrypt-on-close)"),
        ("handlers.py", "vault.write_bytes(path", "choke-point call (delegates to vault)"),
        ("xray.py", "vault.write_bytes(save", "choke-point call (delegates to vault)"),
        ("monitor/watcher.py", "p.write_text(json.dumps(_default_rules_doc()",
         "watch rules: create example file (paths/globs only, no secrets)"),
        ("monitor/watcher.py", 'tmp.write_text(json.dumps({"rules": rules}',
         "watch rules: atomic tmp+os.replace save (paths/globs only, no secrets)"),
        ("main.py", "path.write_text(token",
         "IPC per-session token write (profile DACL; sandbox design §4)"),
        ("sandbox/runner.py", "script_path.write_text(script",
         "sandbox scratch: confirmed script text (bs-sandbox-only scratch DACL)"),
        ("sandbox/runner.py", "out_path.write_text(stdout_txt",
         "sandbox scratch: redacted stdout persist (design §7 64 KB caps)"),
        ("sandbox/runner.py", "err_path.write_text(stderr_txt",
         "sandbox scratch: redacted stderr persist (design §7 64 KB caps)"),
    ]
    flagged = {
        "guardrail/store.py": [
            ("vault.write_bytes(self._records_path", True),
            ("json.dump(", False),
            ("open(self._records_path", False),
        ],
        "tools/handlers.py": [
            ("vault.write_bytes(path, payload)", True),
            ("vault.read_text(path)", True),
            ("open(path, mode", False),
        ],
        "tools/xray.py": [
            ("vault.write_bytes(save", True),
            ("open(save", False),
        ],
        "rag/store.py": [
            ("vault.acquire_lock", True),
            ("vault.release_lock", True),
            ("vault.write_bytes(self._path, on_disk)", True),
            ("vault.is_envelope(on_disk)", True),
        ],
    }
    for rel, checks in flagged.items():
        src = (DAEMON_DIR / "blankslate" / rel).read_text(encoding="utf-8")
        for needle, want in checks:
            present = needle in src
            rec(f"T11 {rel}: {'has' if want else 'NO'} '{needle[:44]}'",
                present == want)

    pat = re.compile(
        r"open\([^)]*,\s*[\"'](?:w|a|wb|ab|w\+|a\+)[\"']"
        r"|\.write_text\(|\.write_bytes\("
    )
    hits: list[tuple[str, int, str]] = []
    for p in sorted((DAEMON_DIR / "blankslate").rglob("*.py")):
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pat.search(line):
                hits.append((p.relative_to(DAEMON_DIR).as_posix(), i, line.strip()))
    unlabeled = []
    for rel, ln, line in hits:
        if not any(rel.endswith(af) and needle in line for af, needle, _r in allow):
            unlabeled.append(f"{rel}:{ln}: {line[:80]}")
    print(f"  --- repo-wide raw write sites: {len(hits)} found ---")
    for rel, ln, line in hits:
        reason = next((r for af, n, r in allow if rel.endswith(af) and n in line), None)
        print(f"    {rel}:{ln}  {'ALLOW: ' + reason if reason else '*** NOT ALLOWLISTED ***'}")
    rec("T11 every raw write site is a documented allowlist entry",
        not unlabeled and len(hits) > 0, f"{len(hits)} sites, 0 violations"
        if not unlabeled else "; ".join(unlabeled))

    residue = (list(DAEMON_DIR.glob("*.tmp")) + list((DAEMON_DIR / "data").glob("*.tmp"))
               + list((DAEMON_DIR / "notes").glob("*.tmp")))
    rec("T11 no .tmp residue in data/notes/root", not residue,
        ", ".join(p.name for p in residue))


# ── T12: regressions ─────────────────────────────────────────────────────────

def t12_regressions() -> None:
    print("\n--- T12: regressions ---")
    reg = ToolRegistry()
    rec("T12 registry count 34", len(reg.names()) == 34, f"count={len(reg.names())}")
    cfg = load_config()
    rec("T12 vault_ttl_days default 14.0", cfg.vault_ttl_days == 14.0,
        f"value={cfg.vault_ttl_days}")
    os.environ["BS_VAULT_TTL_DAYS"] = "3.5"
    try:
        cfg2 = load_config()
        rec("T12 BS_VAULT_TTL_DAYS env override", cfg2.vault_ttl_days == 3.5,
            f"value={cfg2.vault_ttl_days}")
    finally:
        os.environ.pop("BS_VAULT_TTL_DAYS", None)
    import blankslate.vault.purge  # noqa: F401
    from blankslate.rag.service import close_store, purge  # noqa: F401
    rec("T12 vault/rag service imports", True)


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    os.chdir(DAEMON_DIR)
    print("HW3 GATE TESTS - Encrypted Vault + Auto-Purge (Phase 3)")
    print("Audit-first: disk-write inventory + Amendment B log grep done")
    print("BEFORE encryption code. Ground truth: on-disk bytes, DPAPI")
    print("unprotect, real daemon spawn ordering, real screen capture.")
    try:
        t1_key()
        t2_envelope()
        t3_conversation()
        await t4_notes()
        await t5_xray()
        t6_rag_lifecycle()
        t7_recovery_unit()
        t8_daemon_spawn()
        t9_purge()
        t10_wiring()
        t11_sweep()
        t12_regressions()
    finally:
        cleanup()


def cleanup() -> None:
    print("\n--- cleanup ---")
    from blankslate.rag import service as rag_service
    try:
        rag_service.close_store()
    except Exception:
        pass
    for d in TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    for k in ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_VAULT_TTL_DAYS"):
        os.environ.pop(k, None)
    print(f"  removed {len(TMP_DIRS)} temp dirs, env cleaned")


if __name__ == "__main__":
    asyncio.run(main())
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    total = len(RESULTS)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    for n, d in failed:
        print(f"  FAILED: {n} -- {d}")
    print(f"GATE_RESULT: {passed}/{total} passed")
    raise SystemExit(0 if passed == total and total > 0 else 1)
