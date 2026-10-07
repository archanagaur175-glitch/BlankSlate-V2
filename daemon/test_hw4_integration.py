"""HW4 GATE - Integration: Hardware + Monitor + Vault through the REAL daemon.

One real run_daemon.py instance drives everything: spawn evidence, WebSocket
wire turns (tool routing, vault writes, LLM chat), monitor push events,
then crash-state recovery + full restore. Ground truth: on-disk bytes,
pycaw COM readbacks, wire traffic timestamps, real daemon stdout ordering.
Zero network beyond loopback + local Ollama.

Discovered-by-integration defects fixed before this gate goes green
(F7 precedent — small NL extraction seams in the affected handlers):
  1. set_volume/mute_microphone only accepted structured args, but the
     judge/orchestrator (voice, HUD, IPC wire) ONLY passes {'query': ...}
     -> every NL volume/mute command errored. Added NL query extraction
     (percent/level/louder/quieter/mute/unmute + mic-context guard with
     silent skip so shared 'mute' keywords don't cross-fire tools).
  2. note_taking matched the bare 'note' WRITE pattern before READ:
     "what does note X say" OVERWROTE the note with query fragments.
     Action inference reordered (delete/list/read before write) + NL
     label extraction with stopword guard.

Tests:
T1  spawn + Amendment A ordering evidence (recovery line before daemon
    started), IPC listening, and NO purge at t=0 (planted aged capture
    survives past IPC-up).
T2  WS transport: ping/pong + get_state on the live daemon.
T3  wire volume turn: 'set volume to 42 percent' -> text_reply intent=tool,
    tools=[set_volume], real COM readback ~0.42, AND the daemon's monitor
    push event for that change arrives on the wire < 1.5 s (tool -> COM ->
    callback -> broadcast). Volume restored.
T4  wire note turn through the vault: write via 'remember that ...' ->
    ciphertext file on disk, read back BY LABEL through the wire ->
    reply contains the secret, file byte-identical (no-wipe through wire).
    Structured delete cleanup.
T5  wire mic-mute turn: 'mute the microphone' -> tools=[mute_microphone]
    (set_volume silently skips mic queries), capture endpoint actually
    muted (COM readback), deterministic short-circuit reply. Unmuted.
T6  wire chat turn: 'hello there' -> LLM reply on the wire; conversation
    history still an envelope and contains the phrase when decrypted;
    daemon logs a real 'RAG ingest' (rag db created with lock — the honest
    plaintext window — while the daemon is alive).
T7  wire media turn: 'play some music' -> tools=[media_control],
    deterministic 'Sent media key' reply (console foregrounded first so
    the key lands somewhere harmless; key delivery itself = HW1 23/23).
T8  regressions: 21 tools; structured-args HW1 contract (set_volume
    percent roundtrip, mute toggle roundtrip, both restored); vault
    envelope roundtrip; approved config defaults (monitor 5/90/90/20/3,
    vault TTL 14).
T9  kill + post-state + FULL RESTORE: rag leftover re-encrypted then
    removed (did not exist pre-gate), conversation history byte-identical
    to pre-gate bytes, aged capture removed, note removed, volume/mute/mic
    restored, no .tmp residue, gate ports free.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil
import websockets
from pycaw.pycaw import AudioUtilities

from blankslate.config import DaemonConfig, load_config
from blankslate.tools.handlers import note_taking
from blankslate.tools.registry import ToolRegistry
from blankslate.vault import core as vault

DAEMON_DIR = Path(__file__).resolve().parent
PORT = 8794
NOTE_LABEL = "hw4gate"
AGED_PNG = DAEMON_DIR / "data" / "bs_hw4_aged_capture.png"

RESULTS: list[tuple[str, bool, str]] = []
TMP_DIRS: list[Path] = []

# Shared state across tests
DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WS = None  # websockets client connection
WIRE: list[tuple[float, dict]] = []
BACKUP: dict[str, object] = {}
ORIG: dict[str, float | bool | None] = {"level": None, "mute": None, "mic": None}


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def _enc(s: str) -> str:
    enc = sys.stdout.encoding or "utf-8"
    return s.encode(enc, "replace").decode(enc)


async def recv_until(ws, pred, deadline: float) -> dict | None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except asyncio.TimeoutError:
            return None
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        WIRE.append((time.time(), msg))
        if pred(msg):
            return msg
    return None


async def send_and_reply(ws, text: str, deadline: float = 60.0) -> dict | None:
    await ws.send(json.dumps({"type": "send_text_message", "text": text}))
    return await recv_until(
        ws, lambda m: m.get("type") in ("text_reply", "error"), deadline=deadline
    )


def _foreground_console() -> None:
    """Best effort: keep the media key (T7) away from the user's apps."""
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# ── setup + T1: spawn evidence ──────────────────────────────────────────────

def t1_spawn() -> None:
    print("\n--- T1: full-daemon spawn + recovery ordering + no purge at t0 ---")
    global DAEMON

    # Backups / plants
    conv = DAEMON_DIR / "data" / "conversation_history.json"
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    BACKUP["rag_existed"] = (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists()
    BACKUP["lock_existed"] = (DAEMON_DIR / "data" / "rag_memory.sqlite3.lock").exists()
    np = DAEMON_DIR / "notes" / f"{NOTE_LABEL}.txt"
    BACKUP["note"] = np.read_bytes() if np.exists() else None
    AGED_PNG.write_bytes(b"\x89PNG aged")
    os.utime(AGED_PNG, (time.time() - 30 * 86400,) * 2)

    render = AudioUtilities.GetSpeakers().EndpointVolume
    ORIG["level"] = render.GetMasterVolumeLevelScalar()
    ORIG["mute"] = bool(render.GetMute())
    ORIG["mic"] = bool(
        AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone())
        .EndpointVolume.GetMute()
    )
    print(f"  originals: vol={ORIG['level']:.4f} sysmute={ORIG['mute']} micmute={ORIG['mic']}")

    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "PYTHONIOENCODING": "utf-8",
    })
    DAEMON = subprocess.Popen(
        [sys.executable, "run_daemon.py"],
        cwd=str(DAEMON_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    def reader() -> None:
        assert DAEMON is not None and DAEMON.stdout is not None
        for line in DAEMON.stdout:
            LINES.append(line.rstrip("\n"))

    threading.Thread(target=reader, daemon=True).start()

    deadline = time.time() + 120
    while time.time() < deadline and DAEMON.poll() is None:
        if any("IPC server listening" in l for l in LINES):
            break
        time.sleep(0.25)
    time.sleep(0.7)

    def idx(n: str) -> int:
        return next((i for i, l in enumerate(LINES) if n in l), -1)

    i_rec, i_start, i_ipc = idx("vault recovery:"), idx("BlankSlate daemon started"), idx("IPC server listening")
    rec("T1 daemon reached IPC listening", i_ipc >= 0 and DAEMON.poll() is None,
        f"exit={DAEMON.poll()} lines={len(LINES)}")
    fresh = not BACKUP["rag_existed"]
    rec("T1 recovery line present" + (" (fresh: lock=none db=absent)" if fresh else " (pre-existing rag)"),
        i_rec >= 0 and (
            any("vault recovery: lock=none db=absent" in l for l in LINES)
            if fresh else True
        ),
        LINES[i_rec].strip()[-60:] if i_rec >= 0 else "MISSING")
    rec("T1 recovery BEFORE daemon started (Amendment A ordering)",
        0 <= i_rec < i_start, f"recovery@{i_rec} started@{i_start}")
    time.sleep(2.0)
    rec("T1 no purge at t=0: planted aged capture still on disk",
        AGED_PNG.exists(), f"age=30d file={AGED_PNG.name}")


# ── T2: WS transport ─────────────────────────────────────────────────────────

async def t2_transport() -> None:
    print("\n--- T2: WebSocket transport on live daemon ---")
    global WS
    WS = await websockets.connect(f"ws://127.0.0.1:{PORT}")
    await WS.send(json.dumps({"type": "ping"}))
    pong = await recv_until(WS, lambda m: m.get("type") == "pong", deadline=5.0)
    rec("T2 ping -> pong", pong is not None)
    await WS.send(json.dumps({"type": "get_state"}))
    st = await recv_until(WS, lambda m: m.get("type") == "state", deadline=5.0)
    rec("T2 get_state -> state payload", st is not None and len(st) >= 3,
        f"keys={sorted(st.keys())[:8] if st else None}")


# ── T3: wire volume + monitor push ───────────────────────────────────────────

async def t3_wire_volume() -> None:
    print("\n--- T3: wire volume turn + monitor push latency ---")
    render = AudioUtilities.GetSpeakers().EndpointVolume
    orig = float(ORIG["level"])
    target = 0.42 if abs(orig - 0.42) > 0.02 else 0.63

    t0 = time.time()
    reply = await send_and_reply(WS, f"set volume to {round(target * 100)} percent", deadline=15.0)
    ok_type = reply is not None and reply.get("type") == "text_reply"
    tools = (reply or {}).get("tools") or []
    rec("T3 text_reply intent=tool tools=[set_volume]",
        ok_type and "set_volume" in tools and (reply or {}).get("intent") == "tool",
        f"intent={(reply or {}).get('intent')} tools={tools}")
    rec("T3 reply reports the percent", ok_type and str(round(target * 100)) in str((reply or {}).get("text", "")),
        repr(str((reply or {}).get("text"))[:60]))
    readback = render.GetMasterVolumeLevelScalar()
    rec("T3 real COM readback ~target", abs(readback - target) < 0.01,
        f"readback={readback:.4f} target={target}")

    m_vol = await recv_until(
        WS,
        lambda m: m.get("type") == "monitor_event"
        and m.get("category") == "volume"
        and abs(m.get("level", -1) - target) < 0.02,
        deadline=3.0,
    )
    lat = (WIRE[-1][0] - t0) if m_vol else 999.0
    rec("T3 monitor_event on wire < 1.5 s (tool->COM->callback->broadcast)",
        m_vol is not None and lat < 1.5, f"latency={lat:.3f}s")

    # conversation still a valid envelope after a live tool turn
    conv = DAEMON_DIR / "data" / "conversation_history.json"
    raw = conv.read_bytes()
    ok_env = vault.is_envelope(raw)
    rec("T3 conversation still envelope + decryptable after live turn",
        ok_env and b"set volume" not in raw
        and b"set volume" in vault.open_envelope(raw).lower())

    render.SetMasterVolumeLevelScalar(orig, None)
    await recv_until(
        WS,
        lambda m: m.get("type") == "monitor_event"
        and m.get("category") == "volume"
        and abs(m.get("level", -1) - orig) < 0.02,
        deadline=3.0,
    )
    print(f"  volume restored to {render.GetMasterVolumeLevelScalar():.4f}")


# ── T4: wire note through the vault ──────────────────────────────────────────

async def t4_wire_note() -> None:
    print("\n--- T4: wire note turn (write + label read, vault on disk) ---")
    np = DAEMON_DIR / "notes" / f"{NOTE_LABEL}.txt"
    if np.exists():
        np.unlink()

    reply = await send_and_reply(
        WS, f"remember that {NOTE_LABEL} integration wire secret", deadline=15.0
    )
    tools = (reply or {}).get("tools") or []
    rec("T4 write routed to note_taking",
        reply is not None and "note_taking" in tools, f"tools={tools}")
    raw = np.read_bytes() if np.exists() else b""
    rec("T4 note ciphertext on disk, no plaintext",
        bool(raw) and vault.is_envelope(raw) and b"integration wire secret" not in raw,
        f"{len(raw)}B envelope" if raw else "missing")
    rec("T4 reply names the label", NOTE_LABEL in str((reply or {}).get("text", "")),
        repr(str((reply or {}).get("text"))[:70]))

    plain_before = vault.read_bytes(np) if raw else b""
    reply2 = await send_and_reply(WS, f"what does note {NOTE_LABEL} say", deadline=15.0)
    rec("T4 label read through wire returns the secret",
        reply2 is not None and "integration wire secret" in str((reply2 or {}).get("text", "")),
        repr(str((reply2 or {}).get("text"))[:80]))
    rec("T4 read did NOT modify the file (no-wipe through wire)",
        np.exists() and vault.read_bytes(np) == plain_before)

    await note_taking({"action": "delete", "label": NOTE_LABEL})
    rec("T4 cleanup: structured delete removed the file", not np.exists())


# ── T5: wire mic mute ────────────────────────────────────────────────────────

async def t5_wire_mic() -> None:
    print("\n--- T5: wire mic-mute turn ---")
    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    orig = bool(ORIG["mic"])
    cap.SetMute(0, None)

    reply = await send_and_reply(WS, "mute the microphone", deadline=15.0)
    tools = (reply or {}).get("tools") or []
    rec("T5 routed to mute_microphone (set_volume skips mic queries)",
        reply is not None and "mute_microphone" in tools and "set_volume" not in tools,
        f"tools={tools} intent={(reply or {}).get('intent')}")
    rec("T5 capture endpoint actually muted (COM readback)",
        bool(cap.GetMute()) is True)
    rec("T5 deterministic reply on wire",
        reply is not None and "microphone" in str((reply or {}).get("text", "")).lower(),
        repr(str((reply or {}).get("text"))[:70]))

    from blankslate.tools.hardware import mute_microphone
    await mute_microphone({"muted": orig})
    rec("T5 mic restored to original state", bool(cap.GetMute()) is orig,
        f"mic={bool(cap.GetMute())} orig={orig}")


# ── T6: wire chat + RAG + conversation vault ─────────────────────────────────

async def t6_wire_chat() -> None:
    print("\n--- T6: wire chat turn -> LLM + RAG + conversation vault ---")
    conv = DAEMON_DIR / "data" / "conversation_history.json"
    before = len(json.loads(vault.read_bytes(conv)))

    reply = await send_and_reply(WS, "hello there", deadline=90.0)
    rec("T6 chat text_reply from real LLM (local ollama)",
        reply is not None and reply.get("type") == "text_reply"
        and len(str(reply.get("text", ""))) > 0,
        f"intent={(reply or {}).get('intent')} text={repr(str((reply or {}).get('text'))[:50])}")

    raw = conv.read_bytes()
    rec("T6 conversation still envelope (no plaintext after chat)",
        vault.is_envelope(raw) and b"hello there" not in raw)
    after = json.loads(vault.read_bytes(conv))
    contents = " | ".join(r.get("content", "") for r in after).lower()
    rec("T6 decrypted history contains the wire phrase; records grew",
        "hello there" in contents and len(after) > before,
        f"{before} -> {len(after)} records")

    # A second wire turn guarantees a closed-record delta past the persisted
    # ingest cursor — the first turn legitimately no-ops when the cursor is
    # already caught up (trailing turn is excluded from `closed`).
    reply2 = await send_and_reply(WS, "how are you today?", deadline=90.0)
    rec("T6 second chat turn (forces closed-record delta for ingest)",
        reply2 is not None and reply2.get("type") == "text_reply"
        and len(str(reply2.get("text", ""))) > 0,
        f"text={repr(str((reply2 or {}).get('text'))[:40])}")

    rag = DAEMON_DIR / "data" / "rag_memory.sqlite3"
    lock = DAEMON_DIR / "data" / "rag_memory.sqlite3.lock"
    deadline = time.time() + 15
    while time.time() < deadline and not any("RAG ingest" in l for l in LINES):
        await asyncio.sleep(0.3)
    diag = next((l.strip()[-90:] for l in LINES if "RAG ingest" in l), None)
    if diag is None:
        near = [l.strip()[-90:] for l in LINES
                if ("RAG" in l or "rag" in l or "embed" in l or "hook" in l
                    or "Traceback" in l)]
        diag = "MISSING; daemon: " + (
            " || ".join(near[-3:]) if near else "no rag/hook/embed/traceback lines at all")
    rec("T6 daemon logged real RAG ingest over the wire turn",
        any("RAG ingest" in l for l in LINES), diag)
    rec("T6 honest plaintext window while alive: db plaintext + lock present",
        rag.exists() and lock.exists() and not vault.is_envelope(rag.read_bytes()))


# ── T7: wire media control ───────────────────────────────────────────────────

async def t7_wire_media() -> None:
    print("\n--- T7: wire media turn ---")
    _foreground_console()
    reply = await send_and_reply(WS, "play some music", deadline=15.0)
    tools = (reply or {}).get("tools") or []
    rec("T7 routed to media_control, deterministic reply",
        reply is not None and "media_control" in tools
        and "Sent media key" in str((reply or {}).get("text", "")),
        f"tools={tools} text={repr(str((reply or {}).get('text'))[:60])}")


# ── T8: regressions ──────────────────────────────────────────────────────────

async def t8_regressions() -> None:
    print("\n--- T8: regressions (structured HW1 contract + registry + vault) ---")
    reg = ToolRegistry()
    rec("T8 registry count 34", len(reg.names()) == 34, f"count={len(reg.names())}")

    from blankslate.tools.hardware import mute_microphone, set_volume

    render = AudioUtilities.GetSpeakers().EndpointVolume
    cur = render.GetMasterVolumeLevelScalar()
    r = await set_volume({"percent": 55})
    rb = render.GetMasterVolumeLevelScalar()
    rec("T8 structured set_volume percent roundtrip (HW1 contract)",
        not r.get("error") and abs(rb - 0.55) < 0.01, f"readback={rb:.4f}")
    render.SetMasterVolumeLevelScalar(cur, None)

    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    m0 = bool(cap.GetMute())
    r2 = await mute_microphone({"action": "toggle"})
    rec("T8 structured mute toggle roundtrip (HW1 contract)",
        not r2.get("error") and bool(cap.GetMute()) is (not m0),
        f"was={m0} now={bool(cap.GetMute())} r2={r2}")
    await mute_microphone({"muted": m0})
    rec("T8 mute restored after toggle", bool(cap.GetMute()) is m0)

    d = Path(tempfile.mkdtemp(prefix="bs_hw4_"))
    TMP_DIRS.append(d)
    p = d / "x.bin"
    vault.write_bytes(p, b"regression-secret")
    rec("T8 vault envelope roundtrip",
        vault.is_envelope(p.read_bytes()) and vault.read_bytes(p) == b"regression-secret")

    c = DaemonConfig()
    ok = (
        c.monitor_poll_interval == 5.0 and c.monitor_cpu_threshold == 90.0
        and c.monitor_gpu_threshold == 90.0 and c.monitor_battery_threshold == 20.0
        and c.monitor_sustained_samples == 3 and c.vault_ttl_days == 14.0
    )
    rec("T8 approved defaults (monitor 5/90/90/20/3, vault TTL 14)", ok,
        f"{c.monitor_poll_interval}/{c.monitor_cpu_threshold}/{c.monitor_gpu_threshold}/"
        f"{c.monitor_battery_threshold}/{c.monitor_sustained_samples}/{c.vault_ttl_days}")


# ── T9: kill + post-state + FULL RESTORE ─────────────────────────────────────

def t9_restore() -> None:
    print("\n--- T9: kill + post-state + full restore ---")
    global DAEMON

    if DAEMON is not None and DAEMON.poll() is None:
        DAEMON.terminate()
        try:
            DAEMON.wait(timeout=15)
        except subprocess.TimeoutExpired:
            DAEMON.kill()
            DAEMON.wait(timeout=10)
    print(f"  daemon exit code: {DAEMON.poll() if DAEMON else 'n/a'}")
    time.sleep(0.5)

    data = DAEMON_DIR / "data"
    rag = data / "rag_memory.sqlite3"
    lock = data / "rag_memory.sqlite3.lock"
    if BACKUP["rag_existed"]:
        rec("T9 rag pre-existed: left for its own lifecycle",
            rag.exists(), f"rag={rag.exists()} lock={lock.exists()}")
    else:
        leftover = rag.exists() and lock.exists() and not vault.is_envelope(rag.read_bytes())
        rec("T9 daemon left rag plaintext+lock (hard kill, honest window)",
            leftover, f"rag={rag.exists()} lock={lock.exists()}")

        # Crash recovery on the leftover, then full removal (didn't exist pre-gate)
        if rag.exists():
            r = vault.recover_rag_db(rag)
            rec("T9 recover_rag_db re-encrypts the gate's own crash leftover",
                r.get("db") == "re_encrypted" and vault.is_envelope(rag.read_bytes()),
                str(r))
            rag.unlink()
            if lock.exists():
                lock.unlink()
        rec("T9 rag db + lock removed (pre-gate state: absent)",
            not rag.exists() and not lock.exists())

    conv = data / "conversation_history.json"
    if BACKUP["conv"] is not None:
        conv.write_bytes(BACKUP["conv"])  # type: ignore[arg-type]
        # conversation is a vault envelope {v, ts, ct} (sealed turns); the
        # decrypted payload is the turn list — read it through the vault.
        doc = json.loads(vault.read_bytes(conv))
        shape_ok = (isinstance(doc, list) and bool(doc)
                    and isinstance(doc[0], dict) and doc[0].get("role") is not None)
        rec("T9 conversation restored byte-identical to pre-gate",
            conv.read_bytes() == BACKUP["conv"] and shape_ok,
            f"{conv.stat().st_size}B")
    else:
        conv.unlink(missing_ok=True)
        rec("T9 conversation removed (absent pre-gate)", not conv.exists())

    np = DAEMON_DIR / "notes" / f"{NOTE_LABEL}.txt"
    if BACKUP["note"] is not None:
        np.write_bytes(BACKUP["note"])  # type: ignore[arg-type]
    else:
        np.unlink(missing_ok=True)
    rec("T9 gate note restored to pre-gate state",
        (BACKUP["note"] is None) == (not np.exists()))

    AGED_PNG.unlink(missing_ok=True)
    rec("T9 aged capture removed", not AGED_PNG.exists())

    render = AudioUtilities.GetSpeakers().EndpointVolume
    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    render.SetMasterVolumeLevelScalar(float(ORIG["level"]), None)
    render.SetMute(1 if ORIG["mute"] else 0, None)
    cap.SetMute(1 if ORIG["mic"] else 0, None)
    lvl = render.GetMasterVolumeLevelScalar()
    rec("T9 audio restored (level/sysmute/micmute)",
        abs(lvl - float(ORIG["level"])) < 0.001
        and bool(render.GetMute()) is bool(ORIG["mute"])
        and bool(cap.GetMute()) is bool(ORIG["mic"]),
        f"vol={lvl:.4f} sys={bool(render.GetMute())} mic={bool(cap.GetMute())}")

    residue = (list(data.glob("*.tmp")) + list((DAEMON_DIR / "notes").glob("*.tmp"))
               + list(DAEMON_DIR.glob("*.tmp")))
    rec("T9 no .tmp residue", not residue, ", ".join(p.name for p in residue))

    used = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port in (8765, 8791, 8794)]
    rec("T9 gate ports free after kill", not used, f"listening={used}")


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    os.chdir(DAEMON_DIR)
    print("HW4 GATE - Integration: Hardware + Monitor + Vault (real daemon)")
    print("Ground truth: wire traffic, COM readbacks, daemon stdout, on-disk bytes.")
    try:
        t1_spawn()
        await t2_transport()
        await t3_wire_volume()
        await t4_wire_note()
        await t5_wire_mic()
        await t6_wire_chat()
        await t7_wire_media()
        await t8_regressions()
    finally:
        if WS is not None:
            try:
                await WS.close()
            except Exception:
                pass
        t9_restore()
        for k in ("BS_IPC_PORT", "BS_TTS_ENABLED"):
            os.environ.pop(k, None)
        for d in TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        print("  env cleaned, temp dirs removed")


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
