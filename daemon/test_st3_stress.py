"""ST3 GATE - voice-stress adaptation (Phase 3 of the 7-phase expansion).

Design: Phase 3 spec — advisory brevity suffix on the VOICE path only.
Ground truth: wire traffic (stress metadata message), daemon stdout (the
"stress bias applied" judge log line), decrypted conversation history,
word counts of real LLM replies, and a full tl5 re-run as subprocess.

Speech delivery (tl5-proven): daemon spawned with BS_GATE_HOOKS=1; the
utterance is SAPI-synthesized (rate parameter = calm/rushed modulation),
resampled to 16 kHz PCM, and injected through the env-gated
inject_speech_chunk hook at the exact post-VAD speech-chunk boundary,
while real PTT (ctrl+alt+space keybd_event) is held. STT (whisper),
judge, RAG and LLM run unmodified. Calibration probe (this session):
SAPI rate 0 -> urgency ~0.195 (band calm); rate 8 -> ~0.78 (band rushed),
margins vs CALM_MAX=0.35 / RUSH_MIN=0.50.

INTERPRETATION FLAGGED FOR REVIEW (deviation from the literal spec text):
stress metadata travels on a NEW wire message `transcription_stress`
broadcast by main.py. The pipeline's own `transcription` message is
untouched because voice/pipeline.py is NOT in the P3 flag list. Rushed vs
calm messages are correlated by order (utterance text is identical across
deliveries).

Spec assertion map:
(1) wire carries stress.urgency, rushed > calm real numbers .... T3/T4
(2) unit prompt-builder calls (4 cases) ........................ T1
(3) decrypted conversation history has no stress fields ........ T8
(4) runtime toggle off -> second rushed utterance no bias ...... T6
(5) reply word counts printed, rushed <= calm ................. T7
(6) full tl5 re-run (67/67) as subprocess ..................... T10
Beyond plan: T5 zero-delta check for the TEXT path.

Env note for the operator: run in a quiet window — ambient speech while
PTT is held could dispatch an extra utterance (carried tl5 env risk).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import websockets
import win32gui

from blankslate.config import load_config
from blankslate.tools.registry import ToolRegistry

DAEMON_DIR = Path(__file__).resolve().parent
PORT = 8803
PHRASE = "Tell me what causes the northern lights in the sky."
TEXT_TURN = "What is the boiling point of water?"
CALM_RATE = 0
RUSHED_RATE = 8
SCRATCH_PREFIX = "blankslate_st3"
SCRATCH_FILE = Path(tempfile.gettempdir()) / f"{SCRATCH_PREFIX}.txt"
SAPI_TMP = Path(tempfile.gettempdir()) / "st3_gate_sapi.wav"
DAEMON_LOG = Path(tempfile.gettempdir()) / "st3_daemon_stdout.log"
CONV = DAEMON_DIR / "data" / "conversation_history.json"
TASKS = DAEMON_DIR / "data" / "tasks.sqlite3"
TIMELINE_DB = DAEMON_DIR / "data" / "timeline.sqlite3"
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"

VK_CTRL, VK_ALT, VK_SPACE = 0x11, 0x12, 0x20
KEYUP = 0x0002
_u32 = None

RESULTS: list[tuple[str, bool, str]] = []
DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WIRE: list[tuple[float, dict[str, Any]]] = []
DRAIN_TASK: asyncio.Task | None = None
BACKUP: dict[str, object] = {}
REG = ToolRegistry()
USER_NOTEPAD_OPEN = False
CALM: dict[str, Any] = {}
RUSHED: dict[str, Any] = {}
REPLY_CALM = ""
REPLY_RUSHED = ""
WS_ERRS: list[str] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def line_count(substr: str) -> int:
    return sum(1 for l in LINES if substr in l)


async def wait_line(substr: str, n: int = 1, deadline: float = 10.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        if line_count(substr) >= n:
            return True
        await asyncio.sleep(0.2)
    return False


async def wait_wire(pred: Any, timeout: float, start: int = 0) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _ts, m in WIRE[start:]:
            if pred(m):
                return m
        await asyncio.sleep(0.15)
    return None


async def send_expect(ws: Any, payload: dict[str, Any], pred: Any,
                      timeout: float = 15) -> dict[str, Any] | None:
    idx = len(WIRE)
    await ws.send(json.dumps(payload))
    deadline = time.time() + timeout
    while time.time() < deadline:
        for _ts, m in WIRE[idx:]:
            if pred(m):
                return m
        await asyncio.sleep(0.15)
    return None


# ── spawn / kill (tl5 pattern) ──────────────────────────────────────────────

def spawn() -> bool:
    global DAEMON
    mark = len(LINES)
    DAEMON_LOG.unlink(missing_ok=True)
    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "BS_GATE_HOOKS": "1",
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
        with DAEMON_LOG.open("a", encoding="utf-8") as logf:
            for line in DAEMON.stdout:
                LINES.append(line.rstrip("\n"))
                logf.write(line)
                logf.flush()

    threading.Thread(target=reader, daemon=True).start()

    deadline = time.time() + 150
    ok = False
    while time.time() < deadline:
        if DAEMON.poll() is not None:
            break
        if any("IPC server listening" in l for l in LINES[mark:]):
            ok = True
            break
        time.sleep(0.25)
    time.sleep(0.7)
    ok = ok and DAEMON.poll() is None
    if not ok:
        print(f"  spawn FAILED (exit={DAEMON.poll()}); stdout tail:")
        for l in LINES[mark:][-25:]:
            print(f"    | {l}")
    return ok


def kill_daemon() -> None:
    global DAEMON
    if DAEMON is None:
        return
    DAEMON.terminate()
    try:
        DAEMON.wait(timeout=15)
    except subprocess.TimeoutExpired:
        DAEMON.kill()
        DAEMON.wait(timeout=10)
    print(f"  daemon killed (exit={DAEMON.poll()})")
    DAEMON = None


# ── speech + PTT helpers ────────────────────────────────────────────────────

def synthesize_rate(text: str, rate: int) -> bytes:
    """Offline Windows SAPI -> wav bytes at the given voice rate (silent:
    stream bound to the temp file only, never to the output device)."""
    import win32com.client

    spk = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(str(SAPI_TMP), 3, False)  # SSFMCreateForWrite
    spk.AudioOutputStream = stream
    spk.Rate = rate
    spk.Speak(text, 0)  # sync
    stream.Close()
    data = SAPI_TMP.read_bytes()
    SAPI_TMP.unlink(missing_ok=True)
    return data


def wav_bytes_to_pcm16k(wav_bytes: bytes) -> bytes:
    """wav (SAPI 22.05 kHz) -> mono int16 @16 kHz + 1.0 s trailing silence
    (16 kHz matches MicCapture.SAMPLE_RATE; silence finalizes the VAD chunk)."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch == 2:
        pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.int16)
    if rate != 16000:
        n_new = int(len(pcm) * 16000 / rate)
        x_new = np.linspace(0.0, float(len(pcm) - 1), n_new)
        pcm = np.interp(x_new, np.arange(len(pcm), dtype=np.float64),
                        pcm.astype(np.float64)).astype(np.int16)
    pcm = np.concatenate([pcm, np.zeros(16000, dtype=np.int16)])
    return pcm.tobytes()


async def inject_pcm(ws: Any, pcm: bytes) -> tuple[bool, int]:
    payload = {
        "type": "inject_speech_chunk",
        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
    }
    ack = await send_expect(
        ws, payload, lambda m: m.get("type") in ("ack", "error"), 10.0
    )
    ok = (ack is not None and ack.get("type") == "ack"
          and ack.get("setting") == "inject_speech_chunk")
    return ok, int((ack or {}).get("bytes", 0))


def ptt_down() -> None:
    global _u32
    if _u32 is None:
        import ctypes

        _u32 = ctypes.windll.user32
    _u32.keybd_event(VK_CTRL, 0, 0, 0)
    _u32.keybd_event(VK_ALT, 0, 0, 0)
    time.sleep(0.06)
    _u32.keybd_event(VK_SPACE, 0, 0, 0)


def ptt_up() -> None:
    global _u32
    if _u32 is None:
        import ctypes

        _u32 = ctypes.windll.user32
    _u32.keybd_event(VK_SPACE, 0, KEYUP, 0)
    time.sleep(0.05)
    _u32.keybd_event(VK_ALT, 0, KEYUP, 0)
    _u32.keybd_event(VK_CTRL, 0, KEYUP, 0)


def fg_title() -> str:
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())


def notepad_titles() -> list[str]:
    titles: list[str] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if "notepad" in t.lower():
                titles.append(t)
        return True

    win32gui.EnumWindows(cb, None)
    return titles


# ── one full utterance: PTT + inject + collect trans/stress/reply ───────────

async def deliver(ws: Any, text: str, rate: int) -> dict[str, Any]:
    pcm = wav_bytes_to_pcm16k(synthesize_rate(text, rate))
    presses_before = line_count("PTT pressed")
    disp_before = line_count("dispatching without wake word")
    start = len(WIRE)
    ptt_down()
    ptt_seen = await wait_line("PTT pressed", presses_before + 1, 6.0)
    await asyncio.sleep(0.3)
    inj_ok, inj_bytes = await inject_pcm(ws, pcm)
    await asyncio.sleep(0.5)
    ptt_up()
    # Reply is the LAST wire event of the turn — wait for it first, then
    # pick trans/stress from the already-collected window (no double wait).
    reply = await wait_wire(
        lambda m: m.get("type") == "assistant_response"
        and m.get("source") == "voice",
        240.0, start,
    )
    trans = next((m for _t, m in WIRE[start:]
                  if m.get("type") == "transcription"), None)
    stress = next((m for _t, m in WIRE[start:]
                   if m.get("type") == "transcription_stress"), None)
    return {
        "inj_ok": inj_ok, "inj_bytes": inj_bytes, "ptt_seen": ptt_seen,
        "dispatched": line_count("dispatching without wake word") > disp_before,
        "trans": trans, "stress": stress, "reply": reply, "start": start,
    }


# ── T1: offline units ───────────────────────────────────────────────────────

def t1_units() -> None:
    print("--- T1: offline units (config default/env override + builder) ---")
    from blankslate.llm.ollama_client import ChatMessage
    from blankslate.voice import stress as S

    c = load_config()
    rec("T1 config default stress_adaptation_enabled=True",
        c.stress_adaptation_enabled is True, "")
    os.environ["BS_STRESS_ADAPTATION_ENABLED"] = "0"
    try:
        c2 = load_config()
        rec("T1 env override BS_STRESS_ADAPTATION_ENABLED=0",
            c2.stress_adaptation_enabled is False, "")
    finally:
        os.environ.pop("BS_STRESS_ADAPTATION_ENABLED", None)
    c3 = load_config()
    rec("T1 default restored after env pop",
        c3.stress_adaptation_enabled is True, "")

    base = [ChatMessage(role="system", content="SYS"),
            ChatMessage(role="user", content="hello")]
    ser = lambda ms: json.dumps([(m.role, m.content) for m in ms])
    before = ser(base)
    rushed = {"urgency": 0.8, "band": "rushed"}
    calm = {"urgency": 0.2, "band": "calm"}

    out = S.apply_stress_bias(base, calm, enabled=True, text="hi")
    rec("T1 enabled+calm => no bias", ser(out) == before, "")

    out = S.apply_stress_bias(base, rushed, enabled=True, text="hi")
    rec("T1 enabled+rushed => system bias suffix appended",
        len(out) == len(base) + 1
        and out[-1].role == "system"
        and out[-1].content == S.BIAS_SUFFIX, "")

    out = S.apply_stress_bias(base, rushed, enabled=False, text="hi")
    rec("T1 disabled => byte-identical assembly", ser(out) == before, "")

    out = S.apply_stress_bias(base, rushed, enabled=True,
                              text="explain it in detail")
    rec("T1 rushed+detail request => no bias", ser(out) == before, "")

    rec("T1 detail keyword guard",
        S.has_detail_request("tell me the long version")
        and S.has_detail_request("step by step please")
        and not S.has_detail_request("along the way")
        and not S.has_detail_request(PHRASE), "")

    sil = np.zeros(32000, dtype=np.int16).tobytes()
    rec("T1 silence PCM => no reading (None)",
        S.compute_from_pcm(sil, "hi") is None, "")

    rec("T1 suffix advisory (rushed + brief)",
        "rushed" in S.BIAS_SUFFIX and "brief" in S.BIAS_SUFFIX.lower(),
        S.BIAS_SUFFIX[:70])

    busy = [c2_.laddr.port for c2_ in psutil.net_connections(kind="tcp")
            if getattr(c2_, "laddr", None) and c2_.status == "LISTEN"
            and c2_.laddr.port == PORT]
    rec("T1 gate port 8803 free pre-spawn", not busy, f"listening={busy}")


# ── T2: spawn + connect + focus target ──────────────────────────────────────

async def t2_connect() -> Any:
    global DRAIN_TASK, USER_NOTEPAD_OPEN
    print("\n--- T2: ws subscribe + STT ready + scratch Notepad focus ---")
    ws = await websockets.connect(f"ws://127.0.0.1:{PORT}", open_timeout=5)

    async def drain() -> None:
        try:
            async for raw in ws:
                try:
                    WIRE.append((time.time(), json.loads(raw)))
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            pass

    DRAIN_TASK = asyncio.create_task(drain())
    await asyncio.sleep(0.4)
    rec("T2 gate ws subscribed (drain running)", True, f"wire={len(WIRE)}")

    stt = await wait_line("Voice pipeline: STT ready", 1, 60.0)
    degraded = line_count("STT degraded") > 0
    rec("T2 STT model ready before dispatch", stt and not degraded,
        f"ready={stt} degraded={degraded}")

    existing = notepad_titles()
    USER_NOTEPAD_OPEN = bool(existing and not all(SCRATCH_PREFIX in t
                                                  for t in existing))
    if existing and not USER_NOTEPAD_OPEN:
        await REG.get("app_kill").handler({"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")

    SCRATCH_FILE.write_text("st3 scratch line one\n", encoding="utf-8")
    os.startfile(str(SCRATCH_FILE))
    await asyncio.sleep(2.5)
    if SCRATCH_PREFIX not in fg_title().lower():
        await REG.get("switch_window").handler({"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.6)
    rec("T2 scratch Notepad open and foreground",
        SCRATCH_PREFIX in fg_title().lower(), f"fg={fg_title()!r}")
    return ws


# ── T3: calm utterance ──────────────────────────────────────────────────────

async def t3_calm(ws: Any) -> None:
    global CALM, REPLY_CALM
    print("\n--- T3: calm utterance (SAPI rate 0) ---")
    d = await deliver(ws, PHRASE, CALM_RATE)
    CALM = d
    rec("T3 PTT held + injection accepted + dispatched",
        d["ptt_seen"] and d["inj_ok"] and d["dispatched"],
        f"ptt={d['ptt_seen']} inj={d['inj_ok']} bytes={d['inj_bytes']} disp={d['dispatched']}")
    t = d["trans"] or {}
    rec("T3 STT transcription broadcast (utterance text)",
        "northern lights" in str(t.get("text", "")).lower(),
        repr(str(t.get("text", ""))[:70]))
    s = d["stress"] or {}
    u = float(s.get("urgency", -1.0))
    rec("T3 stress metadata present, band=calm",
        bool(s) and s.get("band") == "calm",
        f"urgency={u:.4f} band={s.get('band')} features={s.get('features')}")
    r = d["reply"] or {}
    REPLY_CALM = str(r.get("text", ""))
    rec("T3 voice reply: intent=chat, zero tools",
        r.get("intent") == "chat" and list(r.get("tools") or []) == [],
        f"intent={r.get('intent')} tools={r.get('tools')}")
    n = line_count("stress bias applied")
    rec("T3 calm: NO bias log line", n == 0, f"count={n}")


# ── T4: rushed utterance ────────────────────────────────────────────────────

async def t4_rushed(ws: Any) -> None:
    global RUSHED, REPLY_RUSHED
    print("\n--- T4: rushed utterance (SAPI rate 8) ---")
    d = await deliver(ws, PHRASE, RUSHED_RATE)
    RUSHED = d
    s_calm = CALM.get("stress") or {}
    s = d["stress"] or {}
    u_calm = float(s_calm.get("urgency", -1.0))
    u_rush = float(s.get("urgency", -1.0))
    rec("T4 wire carries stress.urgency, rushed > calm, band=rushed",
        bool(s) and u_rush > u_calm and s.get("band") == "rushed",
        f"calm={u_calm:.4f} rushed={u_rush:.4f} band={s.get('band')}")
    n = line_count("stress bias applied")
    rec("T4 rushed: bias log applied exactly once", n == 1,
        next((ln.strip()[-70:] for ln in LINES if "stress bias applied" in ln),
             "MISSING"))
    r = d["reply"] or {}
    REPLY_RUSHED = str(r.get("text", ""))
    rec("T4 voice reply: intent=chat, zero tools",
        d["reply"] is not None and r.get("intent") == "chat"
        and list(r.get("tools") or []) == [],
        f"intent={r.get('intent')} tools={r.get('tools')} "
        f"dispatched={d['dispatched']}")


# ── T5: text path zero delta ────────────────────────────────────────────────

async def t5_text(ws: Any) -> None:
    print("\n--- T5: TEXT path zero-delta (no stress artifacts) ---")
    start = len(WIRE)
    b0 = line_count("stress bias applied")
    reply = await send_expect(
        ws, {"type": "send_text_message", "text": TEXT_TURN},
        lambda m: m.get("type") in ("text_reply", "error"), 180.0,
    )
    rec("T5 text turn answered", reply is not None
        and reply.get("type") == "text_reply",
        f"type={(reply or {}).get('type')} intent={(reply or {}).get('intent')}")
    stress_msgs = [m for _t, m in WIRE[start:]
                   if m.get("type") == "transcription_stress"]
    rec("T5 text path: no transcription_stress on wire",
        not stress_msgs, f"found={len(stress_msgs)}")
    b1 = line_count("stress bias applied")
    rec("T5 text path: no new bias log line", b1 == b0,
        f"before={b0} after={b1}")


# ── T6: runtime toggle off ──────────────────────────────────────────────────

async def t6_toggle(ws: Any) -> None:
    print("\n--- T6: set_stress_adaptation {enabled:false} + 3rd utterance ---")
    ack = await send_expect(
        ws, {"type": "set_stress_adaptation", "enabled": False},
        lambda m: m.get("type") == "ack"
        and m.get("setting") == "stress_adaptation", 10.0,
    )
    rec("T6 toggle ack (setting=stress_adaptation, enabled=false)",
        ack is not None and ack.get("enabled") is False,
        f"ack={ack}")
    b0 = line_count("stress bias applied")
    d = await deliver(ws, PHRASE, RUSHED_RATE)
    s = d["stress"] or {}
    rec("T6 stress still computed+broadcast while disabled",
        bool(s) and s.get("band") == "rushed",
        f"urgency={float(s.get('urgency', -1.0)):.4f} band={s.get('band')}")
    b1 = line_count("stress bias applied")
    rec("T6 toggle OFF: NO new bias log for rushed utterance",
        b1 == b0, f"before={b0} after={b1}")
    rec("T6 voice reply still arrives (turn unaffected)",
        d["reply"] is not None,
        f"reply_len={len(str((d['reply'] or {}).get('text', '')))}")


# ── T7: behavioral reply length ─────────────────────────────────────────────

def t7_behavioral() -> None:
    print("\n--- T7: reply word counts (calm vs rushed) ---")
    wc = lambda t: len(str(t).split())
    w1, w2 = wc(REPLY_CALM), wc(REPLY_RUSHED)
    print(f"  calm   reply ({w1}w): {REPLY_CALM[:160]!r}")
    print(f"  rushed reply ({w2}w): {REPLY_RUSHED[:160]!r}")
    rec("T7 rushed reply word count <= calm reply", w1 > 0 and w2 > 0
        and w2 <= w1, f"calm={w1}w rushed={w2}w")


# ── T8: decrypted conversation scan (post-kill) ─────────────────────────────

def t8_conv_scan() -> None:
    print("\n--- T8: decrypted conversation has no stress residue ---")
    from blankslate import vault

    if not CONV.exists():
        rec("T8 conversation file present", False, "missing")
        return
    raw = CONV.read_bytes()
    try:
        txt = vault.open_envelope(raw) if vault.is_envelope(raw) else raw
        data = json.loads(txt)
        ok = True
    except Exception as exc:
        rec("T8 conversation decrypts + parses", False,
            f"{type(exc).__name__}: {exc}")
        return
    n = len(data) if isinstance(data, list) else -1
    rec("T8 conversation decrypts + parses", ok,
        f"records={n} envelope={vault.is_envelope(raw)}")
    flat = json.dumps(data, ensure_ascii=False).lower()
    rec("T8 no urgency field persisted", '"urgency"' not in flat, "")
    rec("T8 no transcription_stress persisted",
        "transcription_stress" not in flat, "")
    rec("T8 no bias suffix persisted", "sounds rushed" not in flat, "")
    rec("T8 voice turns persisted (utterance in history)",
        "northern lights" in flat, "")


# ── T9: full restore (before tl5) ───────────────────────────────────────────

async def t9_restore() -> None:
    print("\n--- T9: full restore (conv/tasks/timeline/rules + rag + Notepad) ---")
    for key, path in (("conv", CONV), ("tasks", TASKS),
                      ("timeline", TIMELINE_DB), ("rules", RULES_FILE)):
        if BACKUP.get(key) is not None:
            path.write_bytes(BACKUP[key])  # type: ignore[arg-type]
            rec(f"T9 {key} byte-identical",
                path.read_bytes() == BACKUP[key],
                f"{path.stat().st_size}B")
        else:
            path.unlink(missing_ok=True)
            rec(f"T9 {key} removed (absent pre-gate)", not path.exists(), "")

    if not BACKUP.get("rag_existed"):
        (DAEMON_DIR / "data" / "rag_memory.sqlite3").unlink(missing_ok=True)
        (DAEMON_DIR / "data" / "rag_memory.sqlite3.lock").unlink(missing_ok=True)
        rec("T9 rag db+lock removed (absent pre-gate)",
            not (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists(), "")
    else:
        rec("T9 pre-existing rag left for its own lifecycle", True,
            "existence only")

    nt = notepad_titles()
    if nt and not USER_NOTEPAD_OPEN and all(SCRATCH_PREFIX in t for t in nt):
        await REG.get("app_kill").handler({"query": "notepad"})
        await asyncio.sleep(0.5)
        nt = notepad_titles()
    rec("T9 scratch Notepad closed (user Notepad untouched)",
        not any(SCRATCH_PREFIX in t for t in nt), f"remaining={nt}")
    SCRATCH_FILE.unlink(missing_ok=True)
    SAPI_TMP.unlink(missing_ok=True)


# ── T10: full tl5 re-run (mandated) ─────────────────────────────────────────

def t10_tl5() -> None:
    print("\n--- T10: full tl5 re-run (mandated) — OUTPUT BELOW ---")
    try:
        proc = subprocess.run(
            [sys.executable, "test_tl5_audio.py"],
            cwd=str(DAEMON_DIR), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=1500,
        )
    except subprocess.TimeoutExpired:
        rec("T10 tl5 re-run 67/67 + rc 0", False, "timed out after 1500s")
        return
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr.strip():
        print("--- tl5 stderr (tail) ---")
        print(proc.stderr[-1500:])
    ok = proc.returncode == 0 and "GATE_RESULT: 67/67 passed" in proc.stdout
    rec("T10 tl5 re-run GATE_RESULT 67/67 + rc 0", ok,
        f"rc={proc.returncode}")


# ── T11: final cleanup ──────────────────────────────────────────────────────

def t11_final() -> None:
    print("\n--- T11: final cleanup (ports, strays, residue, env, conv) ---")
    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port == PORT]
    rec("T11 gate port 8803 free", not busy, f"listening={busy}")

    strays = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = p.info.get("cmdline") or []
        if any("run_daemon.py" in str(a) for a in cl):
            strays.append(p.info.get("pid"))
    rec("T11 no stray daemon processes", not strays, f"pids={strays}")

    if BACKUP.get("conv") is not None and CONV.exists():
        rec("T11 conversation byte-identical after tl5",
            CONV.read_bytes() == BACKUP["conv"],
            f"{CONV.stat().st_size}B")

    rec("T11 SAPI temp removed", not SAPI_TMP.exists(), str(SAPI_TMP))
    nt = notepad_titles()
    rec("T11 no scratch Notepad left open",
        not any(SCRATCH_PREFIX in t for t in nt), f"remaining={nt}")

    gate_vars = ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_GATE_HOOKS",
                 "BS_STRESS_ADAPTATION_ENABLED")
    leaked = [k for k in gate_vars if k in os.environ]
    rec("T11 env clean (no gate vars leaked into os.environ)",
        not leaked, f"leaked={leaked}")
    SAPI_TMP.unlink(missing_ok=True)
    SCRATCH_FILE.unlink(missing_ok=True)


# ── main ────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("=" * 72)
    print("ST3 GATE — voice-stress adaptation (Phase 3)")
    print(f"  phrase={PHRASE!r} calm_rate={CALM_RATE} rushed_rate={RUSHED_RATE}")
    print("=" * 72)

    t1_units()

    conv = CONV
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    BACKUP["tasks"] = TASKS.read_bytes() if TASKS.exists() else None
    BACKUP["timeline"] = TIMELINE_DB.read_bytes() if TIMELINE_DB.exists() else None
    BACKUP["rules"] = RULES_FILE.read_bytes() if RULES_FILE.exists() else None
    BACKUP["rag_existed"] = (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists()
    print(f"  BACKUP: conv={'bytes' if BACKUP['conv'] is not None else 'absent'} "
          f"tasks={'bytes' if BACKUP['tasks'] is not None else 'absent'} "
          f"rag_existed={BACKUP['rag_existed']}")

    spawn_ok = spawn()
    rec("T2 daemon reached IPC listening (port 8803)", spawn_ok,
        f"exit={DAEMON.poll() if DAEMON else 'gone'} lines={len(LINES)}")

    ws = None
    try:
        if spawn_ok:
            ws = await t2_connect()
            await t3_calm(ws)
            await t4_rushed(ws)
            await t5_text(ws)
            await t6_toggle(ws)
            t7_behavioral()
        else:
            for name in ("T3 calm delivery", "T4 rushed delivery",
                         "T5 text zero-delta", "T6 toggle off",
                         "T7 behavioral"):
                rec(f"{name} (skipped: spawn failed)", False, "")
    except Exception as exc:
        rec("T3-T7 delivery phase completed without exception", False,
            f"{type(exc).__name__}: {exc}")
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if DRAIN_TASK is not None:
            DRAIN_TASK.cancel()
        kill_daemon()
        await asyncio.sleep(0.6)

    t8_conv_scan()
    await t9_restore()
    t10_tl5()
    t11_final()

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _note in RESULTS if ok)
    print("=" * 72)
    for name, ok, note in RESULTS:
        if not ok:
            print(f"  FAILED: {name}" + (f" -- {note}" if note else ""))
    print(f"GATE_RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
