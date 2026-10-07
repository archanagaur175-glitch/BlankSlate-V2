"""FD5 GATE - Full-duplex streaming voice (Phase 5 of the 7-phase expansion).

Design: Phase 5 spec — additive mode switch (cfg.voice_mode, default "ptt"),
new isolated blankslate/voice/streaming.py (continuous MicCapture + per-frame
partial transcription every ~600ms -> transcription_partial {final:false} +
barge-in), voice/tts.py gains a cancellable handle (stop()/is_speaking/
on_state -> tts_state broadcasts), HUD types.ts + App.tsx gain
transcription_partial + tts_state rendered in BOTH compact and full views.
The PTT baseline pipeline (voice/pipeline.py, mic.py's PTT path) is untouched.

TEST-DEVIATION (recorded as required): speech is delivered through the
established tl5 env-gated inject_speech_chunk hook (BS_GATE_HOOKS=1) with
SAPI-synthesized PCM, exactly like tl5/st3 — no human speaks on cue. In
streaming mode the hook replays the injected post-VAD PCM as paced 30 ms
frames through the SAME _on_frame segment logic the live mic uses, so
partial transcription and barge-in onset fire for real over the wire, then
finalize at the chunk boundary. TTS playback is REAL (pocket_tts, enabled).

Ground truth: wire traffic (transcription_partial / transcription /
assistant_response / tts_state), daemon stdout (mode-selection line, STT
ready, barge-in log), source scans, in-process barge/cancel units, npm
typecheck, full test_tl5_audio.py re-run as a subprocess (spec hard
requirement: PTT unchanged).

Spec assertion map:
(1) default config voice_mode == "ptt" + streaming unit behavior ...... T1
(2) streaming conversation: partials -> reply TTS (tts_state
    speaking:true) -> utterance #2 mid-playback -> playback stopped +
    tts_state interrupted:true + utterance #2 dispatched as a new turn
    (real end-to-end over the wire) ............................... T3/T4
(3) PTT unchanged hard requirement: default-config daemon log shows the
    PTT pipeline selected + full tl5 run (67/67) as subprocess ...... T7
(4) HUD: types + both views + typecheck .......................... T5
(5) source invariants (cancellable TTS, no sd.stop() that would kill the
    mic stream, pipeline.py untouched by this phase) .............. T1/T6
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
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

# npm/tsc output can contain non-cp1252 glyphs; keep print safe.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
PORT = 8805

AUDIO_EXT = {".wav", ".pcm", ".flac", ".mp3", ".ogg", ".m4a", ".aac",
             ".opus", ".wma"}
CONV = DAEMON_DIR / "data" / "conversation_history.json"
TASKS = DAEMON_DIR / "data" / "tasks.sqlite3"
TIMELINE_DB = DAEMON_DIR / "data" / "timeline.sqlite3"
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"

UTT1 = "Tell me a long story about a lighthouse keeper and the stormy sea."
UTT2 = "Stop talking please, that is enough."
UTT1_KEYS = ("lighthouse", "story", "long", "tell", "stormy", "sea")
UTT2_KEYS = ("stop", "talking", "enough", "please")

SAPI_TMP = Path(tempfile.gettempdir()) / "fd5_gate_sapi.wav"
VK_CTRL, VK_ALT, VK_SPACE = 0x11, 0x12, 0x20
KEYUP = 0x0002
_u32 = None

RESULTS: list[tuple[str, bool, str]] = []
DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WIRE: list[tuple[float, dict[str, Any]]] = []
DRAIN_TASK: asyncio.Task | None = None
WS: Any = None
BACKUP: dict[str, object] = {}
SNAP_PRE: set[str] = set()
DAEMON_LOG = Path(tempfile.gettempdir()) / "fd5_daemon_stdout.log"


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


async def ping(ws: Any) -> bool:
    m = await send_expect(ws, {"type": "ping"},
                          lambda x: x.get("type") in ("pong", "error"), 5.0)
    return m is not None and m.get("type") == "pong"


def partials_since(idx: int) -> list[dict[str, Any]]:
    return [m for _t, m in WIRE[idx:]
            if m.get("type") == "transcription_partial"
            and str(m.get("text", "")).strip()]


def has_key(text: str, keys: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in keys)


# ── spawn / kill (house pattern; two variants) ──────────────────────────────

def _spawn(env_extra: dict[str, str], env_drop: tuple[str, ...] = (),
           archive_to: str | None = None) -> bool:
    global DAEMON
    mark = len(LINES)
    if archive_to and DAEMON_LOG.exists():
        DAEMON_LOG.replace(Path(tempfile.gettempdir()) / archive_to)
    DAEMON_LOG.unlink(missing_ok=True)
    env = dict(os.environ)
    for k in env_drop:
        env.pop(k, None)
    env.update(env_extra)
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


def spawn_streaming() -> bool:
    """Streaming-mode daemon: TTS ENABLED (spec), gate hooks on, port 8805."""
    return _spawn({
        "BS_IPC_PORT": str(PORT),
        "BS_VOICE_MODE": "streaming",
        "BS_TTS_ENABLED": "True",
        "BS_GATE_HOOKS": "1",
        "PYTHONIOENCODING": "utf-8",
    }, env_drop=("BS_GESTURE_ENABLED", "BS_STREAMING_PARTIAL_INTERVAL",
                 "BS_STREAMING_BARGE_IN_MS", "BS_STREAMING_BARGE_LEVEL"))


def spawn_default() -> bool:
    """Pure-default daemon (no BS_VOICE_MODE) — proves PTT is selected."""
    return _spawn({
        "BS_IPC_PORT": str(PORT),
        "PYTHONIOENCODING": "utf-8",
    }, env_drop=("BS_VOICE_MODE", "BS_GATE_HOOKS", "BS_TTS_ENABLED",
                 "BS_GESTURE_ENABLED"),
        archive_to="fd5_streaming_daemon_stdout.log")


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


# ── speech + PTT helpers (tl5-proven machinery, copied verbatim) ────────────

def synthesize(text: str) -> bytes:
    """Offline Windows SAPI -> wav bytes. SILENT: stream bound to file only."""
    import win32com.client

    spk = win32com.client.Dispatch("SAPI.SpVoice")
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(str(SAPI_TMP), 3, False)  # SSFMCreateForWrite
    spk.AudioOutputStream = stream
    spk.Speak(text, 0)  # sync
    stream.Close()
    data = SAPI_TMP.read_bytes()
    SAPI_TMP.unlink(missing_ok=True)
    return data


def wav_bytes_to_pcm16k(wav_bytes: bytes) -> bytes:
    """wav (SAPI 22.05 kHz) -> mono int16 @16 kHz + 1.0 s trailing silence."""
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


# ── filesystem snapshot ─────────────────────────────────────────────────────

def audio_snapshot() -> set[str]:
    found: set[str] = set()
    for root in (DAEMON_DIR / "data", DAEMON_DIR / "logs",
                 Path(tempfile.gettempdir())):
        if not root.exists():
            continue
        for ext in AUDIO_EXT:
            for p in root.rglob(f"*{ext}"):
                found.add(str(p))
    return found


# ── T1: offline units (config + source invariants + barge/cancel units) ────

def t1_units() -> None:
    print("--- T1: config default (ptt) + source invariants + barge units ---")
    from blankslate.config import DaemonConfig, load_config

    rec("T1 DaemonConfig().voice_mode == 'ptt' (baseline default)",
        DaemonConfig().voice_mode == "ptt", f"got={DaemonConfig().voice_mode}")
    c = DaemonConfig()
    rec("T1 streaming defaults (0.6s / 250ms / 0.25)",
        (c.streaming_partial_interval_s, c.streaming_barge_in_ms,
         c.streaming_barge_min_level) == (0.6, 250, 0.25),
        f"{c.streaming_partial_interval_s}/{c.streaming_barge_in_ms}/{c.streaming_barge_min_level}")
    os.environ["BS_VOICE_MODE"] = "streaming"
    try:
        rec("T1 env override BS_VOICE_MODE=streaming",
            load_config().voice_mode == "streaming", "")
    finally:
        os.environ.pop("BS_VOICE_MODE", None)
    rec("T1 default restored after env pop",
        load_config().voice_mode == "ptt", "")

    pipe_src = (DAEMON_DIR / "blankslate" / "voice" / "pipeline.py").read_text(encoding="utf-8")
    rec("T1 PTT pipeline.py untouched by Phase 5",
        "on_frame" not in pipe_src
        and "streaming" not in pipe_src
        and "transcription_partial" not in pipe_src,
        "no streaming hooks leaked into pipeline.py")

    mic_src = (DAEMON_DIR / "blankslate" / "voice" / "mic.py").read_text(encoding="utf-8")
    rec("T1 mic.py seam exists AND defaults to None (PTT path identical)",
        "on_frame: Callable[[bytes, bool], None] | None = None" in mic_src
        and "self._on_frame = on_frame" in mic_src, "")

    tts_src = (DAEMON_DIR / "blankslate" / "voice" / "tts.py").read_text(encoding="utf-8")
    rec("T1 tts.py: cancellable handle (stop/is_speaking/on_state)",
        "def stop(self)" in tts_src and "def is_speaking" in tts_src
        and "on_state" in tts_src, "")
    rec("T1 tts.py uses explicit OutputStream — never sd.stop() "
        "(sd.stop would kill the mic InputStream)",
        "OutputStream" in tts_src and "sd.stop()" not in tts_src, "")

    stream_src = (DAEMON_DIR / "blankslate" / "voice" / "streaming.py").read_text(encoding="utf-8")
    rec("T1 streaming.py: partial broadcast + inflight cancel present",
        '"transcription_partial"' in stream_src
        and "_cancel_inflight" in stream_src
        and ".cancel()" in stream_src, "")

    # behavioral units: barge onset (echo guard, once-per-turn, re-arm) + cancel
    from blankslate.config import DaemonConfig
    from blankslate.voice.streaming import StreamingVoicePipeline

    p = StreamingVoicePipeline(DaemonConfig(), on_transcription=None, ipc_state=None)

    class FakeTTS:
        def __init__(self) -> None:
            self.stopped = 0

        def stop(self) -> None:
            self.stopped += 1

    fake = FakeTTS()
    p.set_tts_control(lambda: fake)
    p.notify_tts_state(True, False)
    loud = b"\x40\x20" * 480
    quiet = b"\x00\x00" * 480
    for _ in range(5):
        p._on_frame(quiet, True)
    rec("T1 unit: quiet speech never barges (echo guard)", fake.stopped == 0,
        f"stopped={fake.stopped}")
    for _ in range(12):  # 12 * 30ms = 360ms >= 250ms sustained
        p._on_frame(loud, True)
    rec("T1 unit: sustained loud speech barges exactly once",
        fake.stopped == 1, f"stopped={fake.stopped}")
    for _ in range(30):
        p._on_frame(loud, True)
    rec("T1 unit: one barge per TTS speaking window",
        fake.stopped == 1, f"stopped={fake.stopped}")
    p.notify_tts_state(False, True)
    p.notify_tts_state(True, False)
    for _ in range(12):
        p._on_frame(loud, True)
    rec("T1 unit: re-armed on next speaking window", fake.stopped == 2,
        f"stopped={fake.stopped}")

    import concurrent.futures as cf

    fut: cf.Future = cf.Future()
    p._inflight = fut
    p._cancel_inflight()
    rec("T1 unit: pending reply task canceled (barge/new turn)",
        fut.cancelled(), "")
    p._inflight = None


# ── T2: streaming spawn ─────────────────────────────────────────────────────

async def t2_spawn(spawn_ok: bool) -> Any | None:
    print("\n--- T2: streaming daemon spawn (voice_mode=streaming) ---")
    rec("T2 daemon reached IPC listening (port 8805)", spawn_ok,
        f"exit={DAEMON.poll() if DAEMON else 'gone'} lines={len(LINES)}")
    if not spawn_ok:
        return None

    ok = await wait_line("voice mode: streaming (full-duplex StreamingVoicePipeline)", 1, 5.0)
    rec("T2 stdout: streaming pipeline selected", ok,
        next((ln.strip()[-70:] for ln in LINES if "voice mode" in ln), "MISSING"))
    rec("T2 stdout: PTT baseline line ABSENT (not double-constructed)",
        line_count("voice mode: ptt") == 0, "")

    rec("T2 stdout: STT ready (streaming pipeline)",
        await wait_line("Streaming pipeline: STT ready", 1, 180.0),
        next((ln.strip()[-60:] for ln in LINES if "STT ready" in ln), "MISSING"))
    rec("T2 stdout: full-duplex active + PTT hotkey additive",
        line_count("Streaming pipeline: full-duplex active") >= 1
        and line_count("PTT started") >= 1, "")

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

    global DRAIN_TASK, WS
    WS = ws
    DRAIN_TASK = asyncio.create_task(drain())
    await asyncio.sleep(0.5)
    rec("T2 ping -> pong", await ping(ws), "")
    return ws


# ── T3: utterance #1 — partials -> dispatch -> TTS starts ───────────────────

async def t3_turn1(ws: Any) -> None:
    print("\n--- T3: utterance #1 — partials + dispatch + reply TTS starts ---")
    ptt_down()
    try:
        start = len(WIRE)
        await ws.send(json.dumps({
            "type": "inject_speech_chunk",
            "pcm_b64": base64.b64encode(
                wav_bytes_to_pcm16k(synthesize(UTT1))).decode("ascii"),
        }))
        ack = await wait_wire(
            lambda m: m.get("type") == "ack"
            and m.get("setting") == "inject_speech_chunk"
            or m.get("type") == "error", 10.0, start)
        rec("T3 inject ack (streaming replay started)",
            ack is not None and ack.get("type") == "ack"
            and ack.get("setting") == "inject_speech_chunk", f"ack={ack}")

        part = await wait_wire(
            lambda m: m.get("type") == "transcription_partial"
            and str(m.get("text", "")).strip()
            and m.get("final") is False, 30.0, start)
        rec("T3 transcription_partial {final:false} on wire",
            part is not None, f"msg={part}")
        rec("T3 partial text matches utterance #1 content",
            part is not None and has_key(str(part.get("text", "")), UTT1_KEYS),
            f"text={part.get('text') if part else None!r}")

        final = await wait_wire(
            lambda m: m.get("type") == "transcription"
            and has_key(str(m.get("text", "")), UTT1_KEYS), 45.0, start)
        rec("T3 final transcription for utterance #1", final is not None,
            f"text={final.get('text') if final else None!r}")

        resp = await wait_wire(
            lambda m: m.get("type") == "assistant_response"
            and m.get("source") == "voice", 90.0, start)
        rec("T3 reply dispatched as a voice turn (assistant_response)",
            resp is not None,
            f"intent={resp.get('intent') if resp else None} "
            f"text={str(resp.get('text', ''))[:60] if resp else None!r}")
    finally:
        ptt_up()

    tts1 = await wait_wire(
        lambda m: m.get("type") == "tts_state" and m.get("speaking") is True,
        240.0, 0)  # deadline covers first pocket_tts model load
    rec("T3 tts_state {speaking:true} — reply TTS started (TTS enabled)",
        tts1 is not None, f"msg={tts1}")


# ── T4: utterance #2 mid-playback — barge-in, new turn dispatch ─────────────

async def t4_barge(ws: Any) -> None:
    print("\n--- T4: utterance #2 mid-playback -> barge-in + new turn ---")
    start = len(WIRE)
    ptt_down()
    try:
        await ws.send(json.dumps({
            "type": "inject_speech_chunk",
            "pcm_b64": base64.b64encode(
                wav_bytes_to_pcm16k(synthesize(UTT2))).decode("ascii"),
        }))
        ack = await wait_wire(
            lambda m: m.get("type") == "ack"
            and m.get("setting") == "inject_speech_chunk"
            or m.get("type") == "error", 10.0, start)
        rec("T4 inject ack (utterance #2 replay)", ack is not None, f"ack={ack}")

        intr = await wait_wire(
            lambda m: m.get("type") == "tts_state"
            and m.get("speaking") is False and m.get("interrupted") is True,
            30.0, start)
        rec("T4 tts_state {speaking:false, interrupted:true} — playback stopped",
            intr is not None, f"msg={intr}")

        part2 = await wait_wire(
            lambda m: m.get("type") == "transcription_partial"
            and str(m.get("text", "")).strip(), 30.0, start)
        rec("T4 partial(s) observed for utterance #2",
            part2 is not None, f"msg={part2}")

        final2 = await wait_wire(
            lambda m: m.get("type") == "transcription"
            and has_key(str(m.get("text", "")), UTT2_KEYS), 45.0, start)
        rec("T4 final transcription for utterance #2", final2 is not None,
            f"text={final2.get('text') if final2 else None!r}")

        resp2 = await wait_wire(
            lambda m: m.get("type") == "assistant_response"
            and m.get("source") == "voice", 90.0, start)
        rec("T4 utterance #2 dispatched as a NEW turn (assistant_response)",
            resp2 is not None,
            f"text={str(resp2.get('text', ''))[:60] if resp2 else None!r}")
    finally:
        ptt_up()

    rec("T4 daemon stdout: barge-in logged (ground truth)",
        await wait_line("barge-in: sustained speech during TTS", 1, 10.0),
        next((ln.strip()[-70:] for ln in LINES if "barge-in" in ln), "MISSING"))
    rec("T4 daemon healthy after barge-in (ping -> pong)", await ping(ws), "")


# ── T5: HUD — types + BOTH views + typecheck ────────────────────────────────

def t5_hud() -> None:
    print("\n--- T5: transcription_partial + tts_state in BOTH HUD views ---")
    types = (HUD_DIR / "src" / "types.ts").read_text(encoding="utf-8")
    app = (HUD_DIR / "src" / "App.tsx").read_text(encoding="utf-8")

    rec("T5 types.ts carries transcription_partial variant",
        '{ type: "transcription_partial"; text: string; final: boolean }' in types, "")
    rec("T5 types.ts carries tts_state variant",
        '{ type: "tts_state"; speaking: boolean; interrupted: boolean }' in types, "")
    rec("T5 App.tsx handles transcription_partial + tts_state",
        'case "transcription_partial"' in app and 'case "tts_state"' in app, "")
    rec("T5 compact view renders partial pill + TTS chip",
        'data-testid="partial-pill"' in app and 'data-testid="tts-pill"' in app, "")
    rec("T5 full view renders partial line + TTS state",
        'data-testid="partial-line"' in app and 'data-testid="tts-state"' in app, "")

    proc = subprocess.run("npm run typecheck", cwd=str(HUD_DIR),
                          shell=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-1:]
    rec("T5 npm run typecheck exits 0", proc.returncode == 0,
        f"> tsc --noEmit {tail}")


# ── T6: mid-run fs check + wire stats ───────────────────────────────────────

def t6_fs_and_stats() -> None:
    print("\n--- T6: no audio files on disk during run + wire evidence ---")
    new = audio_snapshot() - SNAP_PRE
    rec("T6 no new audio files (TTS/SAPI leave none behind)", not new,
        f"new={sorted(new)[:5]}")
    partial_n = sum(1 for _t, m in WIRE if m.get("type") == "transcription_partial")
    tts_states = [m for _t, m in WIRE if m.get("type") == "tts_state"]
    rec("T6 wire carried partials (>=2 across both turns)", partial_n >= 2,
        f"partials={partial_n}")
    rec("T6 wire tts_state sequence includes speaking + interrupted",
        any(m.get("speaking") is True for m in tts_states)
        and any(m.get("interrupted") is True for m in tts_states),
        f"states={tts_states}")


# ── T7: PTT baseline hard requirement ───────────────────────────────────────

def t7_ptt_baseline() -> None:
    print("\n--- T7: PTT baseline — default spawn log + full tl5 re-run ---")
    from blankslate.config import DaemonConfig

    rec("T7 default config is ptt (asserted again per spec)",
        DaemonConfig().voice_mode == "ptt", "")

    mark = len(LINES)
    ok = spawn_default()
    rec("T7 default daemon spawned (no BS_VOICE_MODE)", ok,
        f"exit={DAEMON.poll() if DAEMON else 'gone'}")
    if ok:
        t7_lines = LINES[mark:]
        rec("T7 stdout: PTT baseline pipeline selected (not streaming)",
            any("voice mode: ptt (baseline VoicePipeline)" in l
                for l in t7_lines)
            and not any("full-duplex" in l for l in t7_lines),
            next((ln.strip()[-70:] for ln in t7_lines if "voice mode" in ln),
                 "MISSING"))
        rec("T7 stdout: PTT started",
            any("Voice pipeline: PTT started" in l for l in t7_lines), "")
        rec("T7 stdout: streaming line ABSENT in default mode",
            not any("Streaming pipeline" in l for l in t7_lines), "")
    kill_daemon()
    time.sleep(1.0)

    print("  running full test_tl5_audio.py as subprocess (spec hard requirement)...")
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "test_tl5_audio.py"],
        cwd=str(DAEMON_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1200,
    )
    tl5_out = (proc.stdout or "") + (proc.stderr or "")
    print("  ----- test_tl5_audio.py output (pasted in full) -----")
    for line in tl5_out.splitlines():
        print(f"  | {line}")
    print("  ----- end tl5 output -----")
    rec("T7 tl5 re-run exit code 0", proc.returncode == 0,
        f"rc={proc.returncode}")
    rec("T7 tl5 GATE_RESULT: 67/67 (PTT unchanged)", "GATE_RESULT: 67/67" in tl5_out,
        next((ln for ln in tl5_out.splitlines() if "GATE_RESULT" in ln), "MISSING"))


# ── T8: kill + BACKUP restore + final cleanup ───────────────────────────────

async def t8_restore() -> None:
    print("\n--- T8: kill + BACKUP restore + final cleanup ---")
    kill_daemon()
    await asyncio.sleep(0.6)

    for key, path in (("conv", CONV), ("tasks", TASKS),
                      ("timeline", TIMELINE_DB), ("rules", RULES_FILE)):
        if BACKUP.get(key) is not None:
            path.write_bytes(BACKUP[key])  # type: ignore[arg-type]
            rec(f"T8 {key} byte-identical",
                path.read_bytes() == BACKUP[key],
                f"{path.stat().st_size}B")
        else:
            path.unlink(missing_ok=True)
            rec(f"T8 {key} removed (absent pre-gate)", not path.exists(), "")

    if not BACKUP.get("rag_existed"):
        (DAEMON_DIR / "data" / "rag_memory.sqlite3").unlink(missing_ok=True)
        (DAEMON_DIR / "data" / "rag_memory.sqlite3.lock").unlink(missing_ok=True)
        rec("T8 rag db+lock removed (absent pre-gate)",
            not (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists(), "")
    else:
        rec("T8 pre-existing rag left for its own lifecycle", True, "existence only")

    new = audio_snapshot() - SNAP_PRE
    rec("T8 no audio files after kill", not new, f"new={sorted(new)[:5]}")

    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port == PORT]
    rec("T8 gate port 8805 free", not busy, f"listening={busy}")

    strays = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cl = p.info.get("cmdline") or []
        if any("run_daemon.py" in str(a) for a in cl):
            strays.append(p.info.get("pid"))
    rec("T8 no stray daemon processes", not strays, f"pids={strays}")

    leaked = [k for k in ("BS_IPC_PORT", "BS_VOICE_MODE", "BS_TTS_ENABLED",
                          "BS_GATE_HOOKS", "BS_STREAMING_PARTIAL_INTERVAL",
                          "BS_STREAMING_BARGE_IN_MS", "BS_STREAMING_BARGE_LEVEL")
              if k in os.environ]
    rec("T8 env clean (no gate vars leaked)", not leaked, f"leaked={leaked}")


# ── main ────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("=" * 72)
    print("FD5 GATE — full-duplex streaming voice (Phase 5)")
    print("  default stays ptt (tl5 re-run proves baseline); streaming mode")
    print("  proven over the wire: partials -> TTS -> barge-in -> new turn.")
    print("=" * 72)

    SNAP_PRE.update(audio_snapshot())
    t1_units()

    conv = CONV
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    BACKUP["tasks"] = TASKS.read_bytes() if TASKS.exists() else None
    BACKUP["timeline"] = TIMELINE_DB.read_bytes() if TIMELINE_DB.exists() else None
    BACKUP["rules"] = RULES_FILE.read_bytes() if RULES_FILE.exists() else None
    BACKUP["rag_existed"] = (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists()

    spawn_ok = spawn_streaming()
    ws = await t2_spawn(spawn_ok)
    try:
        if ws is not None:
            await t3_turn1(ws)
            await t4_barge(ws)
            t5_hud()
            t6_fs_and_stats()
        else:
            for name in ("T3 turn 1", "T4 barge-in", "T5 HUD", "T6 fs/stats"):
                rec(f"{name} (skipped: spawn failed)", False, "")
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if DRAIN_TASK is not None:
            DRAIN_TASK.cancel()
        kill_daemon()
        await asyncio.sleep(0.8)
        t7_ptt_baseline()
        await t8_restore()

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
