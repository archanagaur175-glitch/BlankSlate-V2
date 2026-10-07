"""TL5 GATE - Session audio (real daemon, Amendment 5 both directions).

Design: daemon/docs/TIMELINE_SECURITY_DESIGN.md section 8 (+ Amendment 5).
Ground truth: wire traffic, daemon stdout, raw sqlite while alive, on-disk
audio-file sweep, HUD tsc.

Speech path (user-approved env-gated hook): the acoustic air path
(speakers -> room -> mic) was validated technically this session (measured
pickup rms 8602 through the Hammer headset mic) but is NOT repeatable:
default output routing follows the BT headset (its own headphones are deaf
to its mic), system mute/volume are under live user control, and the device
list churns. Per user decision the gate spawns the daemon with
BS_GATE_HOOKS=1 and delivers the utterance as SAPI-synthesized 16 kHz PCM
through a test-only wire message (inject_speech_chunk) that lands at the
exact post-VAD speech-chunk boundary mic.py invokes. The hook is refused
unless the env var is set - checked in BOTH ipc server and voice pipeline.
Real-capture evidence kept: audio_level frames come from the live mic,
real webrtcvad chunking runs on ambient input (visible in stdout), PTT is
real keybd_event injection observed by pynput, and whisper -> redact_text
-> seal -> FTS run unmodified on the injected PCM. GATE HOOK stdout lines
prove the env gate was active on the daemon side.

Session audio means shared-capture subscription only: the daemon never
opens a second input stream and never writes an audio file (PCM lives in
a RAM queue only).

Tests:
T1  spawn (port 8798) + recovery/watcher/voice/STT-ready stdout + no new
    audio files at boot + volume watcher started.
T2  initial state carries timeline {active:false, audio:false, transcripts:0}
    + keepalive ping task started.
T3  registry 34; exact routing incl. 'begin timeline with audio' (clean
    {start_timeline}) + Phase 1/2 regression phrases.
T4  HUD: MIC indicator on timeline.audio in BOTH StatusBar + CompactWidget
    + types.ts field; npm run typecheck exits 0.
T5  scratch Notepad focused (neutral target for key injection).
T6  direction (a) START: 'begin timeline with audio' -> started with audio;
    pushed state audio:true; audio_level messages flow WITHOUT any PTT
    (session acquired the shared mic).
T7  direction (a) PTT while session active: hold ctrl+alt+space, inject the
    marker utterance while held, release -> voice path WINS the utterance
    (transcription broadcast + dispatch log), session records the transcript
    (search hit: process 'voice' + timestamp), audio_level keeps flowing
    (session held the mic across PTT release), daemon alive.
T8  subsequent capture WITHOUT PTT: 2nd injected marker -> 2nd search hit
    (mic still working afterwards; no wake word -> no dispatch).
T9  direction (a) STOP: audio:false pushed; audio_level messages CEASE
    (shared mic released).
T10 direction (b) PTT FIRST (held) then 'begin timeline with audio' ->
    attach does not drop the voice path (audio_level continues, then
    transcription broadcast + dispatch after the utterance) and the
    session records its transcript (transcripts>=1); then stop.
T11 ATTACK SIM + PCM constraint: with the daemon RUNNING, raw sqlite
    yields zero plaintext (spoken markers / screen text), no FTS files,
    and NO audio file appeared anywhere under the daemon tree during the
    whole session.
T12 final stop + FULL RESTORE (conv byte-identical, tasks, timeline db,
    watch_rules, Notepad, audio, ports, strays, residue, env).

Known warts carried from Phase 4 gate: 'stop timeline' co-fires app_kill
(avoided - gate uses 'end timeline'); bare 'search' co-fires the offline
web_search stub (deterministic prefix); marker phrases are chosen to route
to ZERO tools (probe-verified). Phase 5 wart: system volume/mute change
externally during runs (user at the machine - watcher-logged); the gate
only asserts the daemon never wrote volume (orchestrator evidence +
static call-site scan).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
import psutil
import websockets
from pycaw.pycaw import AudioUtilities

import win32gui
from blankslate.config import DaemonConfig, load_config
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.router import ToolRouter

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
PORT = 8798
SCRATCH_PREFIX = "blankslate_tl5"
MARKER_PHRASE = "The phase five marker reads ready."
SEARCH_Q = "phase marker"
TIMELINE_DB = DAEMON_DIR / "data" / "timeline.sqlite3"
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"
SCRATCH_FILE = Path(tempfile.gettempdir()) / f"{SCRATCH_PREFIX}.txt"
AUDIO_EXT = {".wav", ".pcm", ".flac", ".mp3", ".ogg", ".m4a", ".aac",
             ".opus", ".wma"}
SAPI_TMP = Path(tempfile.gettempdir()) / "tl5_gate_sapi.wav"

VK_CTRL, VK_ALT, VK_SPACE = 0x11, 0x12, 0x20
KEYUP = 0x0002
_u32 = None

RESULTS: list[tuple[str, bool, str]] = []
DAEMON: subprocess.Popen | None = None
LINES: list[str] = []
WS = None
WIRE: list[tuple[float, dict]] = []
BACKUP: dict[str, object] = {}
ORIG: dict[str, object] = {}
REG = ToolRegistry()
USER_NOTEPAD_OPEN = False
AUDIO_SNAP_PRE: set[str] = set()
DAEMON_LOG = Path(tempfile.gettempdir()) / "tl5_daemon_stdout.log"
WS_ERRS: list[str] = []
PINGS: list[tuple[float, float | None]] = []  # (ts, rtt_s | None=timeout)
VOL_CHANGES: list[tuple[float, float, bool]] = []  # (ts, level, mute)
_VOL_STOP = threading.Event()
PING_TASK: asyncio.Task | None = None


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def audio_files_under(root: Path) -> set[str]:
    found: set[str] = set()
    for ext in AUDIO_EXT:
        for p in root.rglob(f"*{ext}"):
            found.add(str(p))
    return found


async def recv_until(ws, pred, deadline: float) -> dict | None:
    end = time.time() + deadline
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.05, end - time.time()))
        except asyncio.TimeoutError:
            return None
        except Exception as exc:  # ConnectionClosed etc — record, fail honestly
            if len(WS_ERRS) < 5:
                WS_ERRS.append(f"{type(exc).__name__}: {exc}")
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
    try:
        await ws.send(json.dumps({"type": "send_text_message", "text": text}))
    except Exception as exc:
        if len(WS_ERRS) < 5:
            WS_ERRS.append(f"send: {type(exc).__name__}: {exc}")
        return None
    return await recv_until(
        ws, lambda m: m.get("type") in ("text_reply", "error"), deadline=deadline
    )


async def get_state(ws) -> dict | None:
    try:
        await ws.send(json.dumps({"type": "get_state"}))
    except Exception as exc:
        if len(WS_ERRS) < 5:
            WS_ERRS.append(f"send: {type(exc).__name__}: {exc}")
        return None
    return await recv_until(ws, lambda m: m.get("type") == "state", deadline=5.0)


async def drain(ws, seconds: float) -> list[dict]:
    wmark = len(WIRE)
    await recv_until(ws, lambda m: False, deadline=seconds)
    return [m for _t, m in WIRE[wmark:]]


def parse_hits(txt: str) -> int:
    m = re.search(r"(\d+) match\(es\)", txt or "")
    return int(m.group(1)) if m else 0


def line_count(substr: str) -> int:
    return sum(1 for l in LINES if substr in l)


async def wait_line(substr: str, n: int = 1, deadline: float = 10.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        if line_count(substr) >= n:
            return True
        await asyncio.sleep(0.2)
    return False


def spawn() -> bool:
    global DAEMON
    mark = len(LINES)
    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "BS_TIMELINE_INTERVAL": "1.0",
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


# ── speech + PTT helpers (env-gated hook delivery, real key injection) ──────

def synthesize(text: str) -> bytes:
    """Offline Windows SAPI -> wav bytes. SILENT: the voice stream is bound
    to the temp file only, never to the default output device."""
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
    """wav (SAPI 22.05 kHz) -> mono int16 @16 kHz + 1.0 s trailing silence.

    16 kHz matches MicCapture's SAMPLE_RATE so the daemon's STT sees the
    same format it would get from the real mic path."""
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


async def inject_marker(ws) -> tuple[bool, int]:
    """Synthesize MARKER_PHRASE and deliver it through the daemon's
    env-gated inject_speech_chunk hook. Returns (ok, bytes)."""
    pcm = wav_bytes_to_pcm16k(synthesize(MARKER_PHRASE))
    payload = json.dumps({
        "type": "inject_speech_chunk",
        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
    })
    try:
        await ws.send(payload)
    except Exception as exc:
        if len(WS_ERRS) < 5:
            WS_ERRS.append(f"inject send: {type(exc).__name__}: {exc}")
        return False, 0
    ack = await recv_until(
        ws, lambda m: m.get("type") in ("ack", "error"), deadline=10.0
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


# ── external-change watchers (user adjusts volume live during runs) ─────────

def volume_watcher() -> None:
    """Poll render volume/mute every 1s; log every EXTERNAL change with ts.
    The daemon never writes volume (asserted separately); this attributes
    observed changes to the environment, not the code under test."""
    import comtypes

    comtypes.CoInitialize()  # this thread owns its COM apartment
    try:
        render = AudioUtilities.GetSpeakers().EndpointVolume
        last = (round(render.GetMasterVolumeLevelScalar(), 4),
                bool(render.GetMute()))
        while not _VOL_STOP.wait(1.0):
            try:
                cur = (round(render.GetMasterVolumeLevelScalar(), 4),
                       bool(render.GetMute()))
            except Exception:
                continue
            if cur != last:
                VOL_CHANGES.append((time.time(), cur[0], cur[1]))
                last = cur
    finally:
        comtypes.CoUninitialize()


def scan_volume_writes() -> list[str]:
    """Static call-site scan: SetMasterVolumeLevelScalar/SetMute must exist
    ONLY in tools/hardware.py (the tool layer the orchestrator logs)."""
    bad: list[str] = []
    for p in (DAEMON_DIR / "blankslate").rglob("*.py"):
        if p.name == "hardware.py":
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"\.Set(?:MasterVolumeLevelScalar|Mute)\s*\(", src):
            bad.append(p.name)
    return sorted(set(bad))


async def keepalive_ping_loop(ws) -> None:
    """Control-frame ping every 2.5s; records RTT (None = >=5s stall).

    Client-side library auto-ping is disabled at connect so this loop is
    the single keepalive clock; a stalled daemon loop surfaces here as a
    timeout instead of silently killing the connection."""
    while True:
        await asyncio.sleep(2.5)
        try:
            # websockets 17: await ping() -> Future; await that -> rtt float
            inner = await ws.ping()
            rtt = float(await asyncio.wait_for(inner, timeout=5.0))
            PINGS.append((time.time(), rtt))
        except asyncio.TimeoutError:
            PINGS.append((time.time(), None))
        except Exception as exc:
            PINGS.append((time.time(), None))
            if len(WS_ERRS) < 5:
                WS_ERRS.append(f"keepalive: {type(exc).__name__}: {exc}")
            await asyncio.sleep(1.0)


# ── T1: spawn ───────────────────────────────────────────────────────────────

def t1_spawn() -> None:
    print("\n--- T1: spawn + voice/STT readiness + boot audio-file sweep ---")
    DAEMON_LOG.unlink(missing_ok=True)  # fresh daemon stdout log for post-mortem
    conv = DAEMON_DIR / "data" / "conversation_history.json"
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    tasks_db = DAEMON_DIR / "data" / "tasks.sqlite3"
    BACKUP["tasks"] = tasks_db.read_bytes() if tasks_db.exists() else None
    BACKUP["rules"] = RULES_FILE.read_bytes() if RULES_FILE.exists() else None
    BACKUP["timeline"] = TIMELINE_DB.read_bytes() if TIMELINE_DB.exists() else None

    render = AudioUtilities.GetSpeakers().EndpointVolume
    ORIG["level"] = render.GetMasterVolumeLevelScalar()
    ORIG["mute"] = bool(render.GetMute())
    ORIG["mic"] = bool(
        AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume.GetMute()
    )
    ORIG["ts"] = time.time()
    print(f"  originals: vol={ORIG['level']:.4f} sysmute={ORIG['mute']} micmute={ORIG['mic']}")
    _VOL_STOP.clear()
    VOL_CHANGES.clear()
    threading.Thread(target=volume_watcher, daemon=True).start()

    AUDIO_SNAP_PRE = audio_files_under(DAEMON_DIR)
    BACKUP["audio_snap"] = AUDIO_SNAP_PRE
    print(f"  pre-gate audio files under daemon tree: {len(AUDIO_SNAP_PRE)}")

    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN" and c.laddr.port == PORT]
    rec("T1 gate port free pre-spawn", not busy, f"listening={busy}")

    spawn_ok = spawn()
    rec("T1 daemon reached IPC listening", spawn_ok,
        f"exit={DAEMON.poll() if DAEMON else 'gone'} lines={len(LINES)}")

    def idx(n: str) -> int:
        return next((i for i, l in enumerate(LINES) if n in l), -1)

    i_rec, i_start = idx("vault recovery:"), idx("BlankSlate daemon started")
    rec("T1 recovery BEFORE daemon started (regression)",
        0 <= i_rec < i_start, f"recovery@{i_rec} started@{i_start}")
    rec("T1 watcher active (Phase 2 regression)",
        any("watcher: active" in l for l in LINES))
    rec("T1 PTT listener started (voice path alive)",
        line_count("PTT started") >= 1)

    stt_ready = False
    end = time.time() + 240
    while time.time() < end and DAEMON and DAEMON.poll() is None:
        if line_count("Voice pipeline: STT ready") >= 1:
            stt_ready = True
            break
        if line_count("Voice pipeline: STT degraded") >= 1:
            break
        time.sleep(0.5)
    rec("T1 shared STT ready (whisper for session audio)", stt_ready,
        "STT ready" if stt_ready else
        f"degraded={line_count('Voice pipeline: STT degraded')} exit={DAEMON.poll() if DAEMON else 'gone'}")

    post = audio_files_under(DAEMON_DIR)
    new = post - AUDIO_SNAP_PRE
    rec("T1 no audio files written at boot", not new,
        ", ".join(Path(p).name for p in sorted(new)))


# ── T2: initial state ───────────────────────────────────────────────────────

async def t2_state() -> None:
    print("\n--- T2: initial timeline state ---")
    global WS, PING_TASK
    # Library auto-ping off: keepalive_ping_loop owns the ping clock so a
    # stalled server shows up as recorded RTT evidence, not a dead socket.
    WS = await websockets.connect(f"ws://127.0.0.1:{PORT}", ping_interval=None)
    PING_TASK = asyncio.create_task(keepalive_ping_loop(WS))
    await WS.send(json.dumps({"type": "ping"}))
    pong = await recv_until(WS, lambda m: m.get("type") == "pong", deadline=5.0)
    rec("T2 ping -> pong", pong is not None)
    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T2 state carries {active:false, audio:false, transcripts:0}",
        tl.get("active") is False and tl.get("audio") is False
        and int(tl.get("transcripts", -1)) == 0,
        f"timeline={tl}")


# ── T3: registry / routing ──────────────────────────────────────────────────

async def t3_routing() -> None:
    print("\n--- T3: registry / routing (34 tools) ---")
    rec("T3 registry count 34", REG.count == 34, f"count={REG.count}")
    cfg = load_config()
    router = ToolRouter(cfg, REG)
    cases = [
        ("begin timeline with audio", {"start_timeline"}),
        ("begin timeline", {"start_timeline"}),
        ("end timeline", {"stop_timeline"}),
        ("search timeline for phase five marker", {"search_timeline", "web_search"}),
        ("remind me to call Ned tomorrow at 5pm", {"set_reminder"}),
        ("watch file logs/tl5.log", {"add_watch_rule"}),
    ]
    bad = []
    for q, want in cases:
        got = set((await router.route(q)).by_keyword)
        if got != want:
            bad.append(f"{q!r} -> {sorted(got)} (want {sorted(want)})")
    rec("T3 routing exact (6 phrases incl. audio start)", not bad,
        "; ".join(bad) if bad else "all exact")


# ── T4: HUD MIC indicator (Amendment: indicator covers audio) ──────────────

def t4_hud() -> None:
    print("\n--- T4: HUD MIC indicator on timeline.audio + typecheck ---")
    status = (HUD_DIR / "src" / "components" / "StatusBar.tsx").read_text(encoding="utf-8")
    compact = (HUD_DIR / "src" / "components" / "CompactWidget.tsx").read_text(encoding="utf-8")
    types = (HUD_DIR / "src" / "types.ts").read_text(encoding="utf-8")

    rec("T4 StatusBar shows MIC on timeline.audio",
        "timeline?.audio" in status and "timeline-audio-indicator" in status
        and "MIC" in status, "audio indicator found")
    rec("T4 CompactWidget shows MIC on timeline.audio",
        "timeline?.audio" in compact and "timeline-audio-indicator" in compact
        and "MIC" in compact, "audio indicator found")
    rec("T4 types.ts carries audio + transcripts fields",
        "audio: boolean" in types and "transcripts: number" in types)

    proc = subprocess.run(
        "npm run typecheck",
        cwd=str(HUD_DIR), capture_output=True, text=True, timeout=180, shell=True,
    )
    rec("T4 npm run typecheck exits 0", proc.returncode == 0,
        (proc.stdout + proc.stderr).strip().splitlines()[-1][:100]
        if proc.stdout or proc.stderr else "no output")


# ── T5: scratch Notepad (neutral focus target for key injection) ───────────

async def t5_setup_notepad() -> None:
    print("\n--- T5: scratch Notepad focused (key-injection target) ---")
    global USER_NOTEPAD_OPEN
    existing = notepad_titles()
    USER_NOTEPAD_OPEN = bool(existing and not all(SCRATCH_PREFIX in t for t in existing))
    if existing and not USER_NOTEPAD_OPEN:
        from blankslate.tools.registry import ToolRegistry as _R

        reg = _R()
        await reg.get("app_kill").handler({"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")

    SCRATCH_FILE.write_text("tl5 scratch line one\n", encoding="utf-8")
    os.startfile(str(SCRATCH_FILE))
    await asyncio.sleep(2.5)
    if SCRATCH_PREFIX not in fg_title().lower():
        reg2 = REG
        await reg2.get("switch_window").handler({"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.6)
    rec("T5 scratch Notepad open and foreground",
        SCRATCH_PREFIX in fg_title().lower(), f"fg={fg_title()!r}")


# ── T6: direction (a) start with audio ──────────────────────────────────────

async def t6_start_audio() -> None:
    print("\n--- T6: (a) 'begin timeline with audio' - session owns mic ---")
    wmark = len(WIRE)
    reply = await send_and_reply(WS, "begin timeline with audio")
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T6 tools=[start_timeline], reply says started with audio",
        tools == ["start_timeline"] and "started with audio" in txt,
        f"tools={tools} text={txt[:90]!r}")
    await drain(WS, 2.0)
    pushed = any(
        m.get("type") == "state" and (m.get("timeline") or {}).get("active") is True
        and (m.get("timeline") or {}).get("audio") is True
        for _t, m in WIRE[wmark:]
    )
    rec("T6 pushed state carries active:true + audio:true", pushed,
        f"wire_msgs={len(WIRE) - wmark}")

    levels = await drain(WS, 2.5)
    n_levels = sum(1 for m in levels if m.get("type") == "audio_level")
    rec("T6 audio_level flowing WITHOUT any PTT (session acquired mic)",
        n_levels >= 5, f"levels={n_levels} in 2.5s")

    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T6 get_state: active+audio, transcripts 0",
        tl.get("active") is True and tl.get("audio") is True
        and int(tl.get("transcripts", -1)) == 0, f"timeline={tl}")

    # baseline marker hits (old user rows, if any) for exact hit deltas later
    r = await send_and_reply(WS, f"search timeline for {SEARCH_Q}")
    t = str((r or {}).get("text", ""))
    BACKUP["base_hits"] = parse_hits(t) if "match(es)" in t else 0
    print(f"  baseline marker hits before utterances: {BACKUP['base_hits']}")


# ── T7: direction (a) PTT pressed while session active ──────────────────────

async def t7_ptt_during_session() -> None:
    print("\n--- T7: (a) PTT during session - voice path wins the utterance ---")
    presses_before = line_count("PTT pressed")
    ptt_down()
    got_press = await wait_line("PTT pressed", presses_before + 1, deadline=4.0)
    rec("T7 PTT hotkey observed by daemon (injected ctrl+alt+space)", got_press,
        f"presses={line_count('PTT pressed')}")
    await asyncio.sleep(0.3)

    t0 = time.time()
    wmark = len(WIRE)  # capture window starts BEFORE the injection
    ok, nbytes = await inject_marker(WS)
    rec("T7 marker PCM injected while PTT held (hook acked + daemon logged)",
        ok and line_count("GATE HOOK: injecting") >= 1,
        f"bytes={nbytes} hook_lines={line_count('GATE HOOK: injecting')}")
    await asyncio.sleep(0.5)  # ptt_initiated captured at the chunk boundary
    ptt_up()
    print(f"  utterance delivered in {time.time() - t0:.1f}s (env-gated hook)")

    rec("T7 daemon alive after PTT utterance", DAEMON is not None and DAEMON.poll() is None,
        f"exit={DAEMON.poll() if DAEMON else 'gone'}")

    # voice path logs dispatch from the STT thread — wait for it to fire
    dispatched = await wait_line("dispatching without wake word", 1, deadline=60.0)
    rec("T7 PTT dispatch executed (voice path handled it)", dispatched,
        "dispatch log seen" if dispatched else "no dispatch line")

    base = int(BACKUP.get("base_hits", 0))
    hit_txt = None
    end = time.time() + 60
    while time.time() < end:
        r = await send_and_reply(WS, f"search timeline for {SEARCH_Q}")
        t = str((r or {}).get("text", ""))
        if "match(es)" in t and parse_hits(t) >= base + 1:
            hit_txt = t
            break
        await asyncio.sleep(2.0)
    rec("T7 session transcript sealed + searchable",
        hit_txt is not None and parse_hits(hit_txt) >= base + 1,
        repr((hit_txt or "no hit in 60s")[:200]))
    # recv_until keeps the socket drained (WIRE grows, pongs get processed)
    # while waiting for the transcription broadcast queued before dispatch.
    await recv_until(
        WS,
        lambda m: m.get("type") == "transcription"
        and re.search(r"phase|marker", str(m.get("text", "")), re.I) is not None,
        deadline=30.0,
    )
    window = [m for _t, m in WIRE[wmark:]]
    trans = [m for m in window if m.get("type") == "transcription"
             and re.search(r"phase|marker", str(m.get("text", "")), re.I)]
    rec("T7 voice path transcription broadcast (PTT won the utterance)",
        bool(trans), f"matching={len(trans)} total_msgs={len(window)}")
    levels_after = sum(1 for m in window if m.get("type") == "audio_level")
    rec("T7 audio_level STILL flowing after PTT release (session held mic)",
        levels_after >= 5, f"levels={levels_after}")
    if hit_txt:
        rec("T7 hit metadata: process 'voice' + timestamp",
            re.search(r"\d{2}:\d{2}:\d{2}\s+voice\b", hit_txt) is not None,
            repr(hit_txt[:200]))
    else:
        rec("T7 hit metadata: process 'voice' + timestamp", False, "no hit text")

    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T7 transcripts >= 1 on state", int(tl.get("transcripts") or 0) >= 1,
        f"transcripts={tl.get('transcripts')}")
    rec("T7 no audio/worker crashes in stdout",
        line_count("MicCapture thread crashed") == 0
        and line_count("timeline audio transcript failed") == 0
        and line_count("timeline session audio subscribe failed") == 0)


# ── T8: subsequent capture without PTT ──────────────────────────────────────

async def t8_subsequent_capture() -> None:
    print("\n--- T8: subsequent capture WITHOUT PTT (mic still working) ---")
    base = int(BACKUP.get("base_hits", 0))
    ok, nbytes = await inject_marker(WS)
    rec("T8 2nd marker injected WITHOUT PTT (ambient path)", ok,
        f"bytes={nbytes} hook_lines={line_count('GATE HOOK: injecting')}")

    hit_txt = None
    end = time.time() + 60
    while time.time() < end:
        r = await send_and_reply(WS, f"search timeline for {SEARCH_Q}")
        t = str((r or {}).get("text", ""))
        if "match(es)" in t and parse_hits(t) >= base + 2:
            hit_txt = t
            break
        await asyncio.sleep(2.0)
    rec("T8 second capture produced 2nd sealed transcript (no PTT)",
        hit_txt is not None and parse_hits(hit_txt) >= base + 2,
        repr((hit_txt or "no hit in 60s")[:200]))

    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T8 transcripts >= 2 on state", int(tl.get("transcripts") or 0) >= 2,
        f"transcripts={tl.get('transcripts')}")


# ── T9: direction (a) stop - mic released ───────────────────────────────────

async def t9_stop_releases_mic() -> None:
    print("\n--- T9: (a) STOP - audio:false pushed, audio_level ceases ---")
    wmark = len(WIRE)
    reply = await send_and_reply(WS, "end timeline")
    tools = (reply or {}).get("tools") or []
    rec("T9 tools=[stop_timeline], reply confirms stop",
        tools == ["stop_timeline"] and "stopped" in str((reply or {}).get("text", "")),
        f"tools={tools}")
    await drain(WS, 2.0)
    pushed = any(
        m.get("type") == "state" and (m.get("timeline") or {}).get("active") is False
        and (m.get("timeline") or {}).get("audio") is False
        for _t, m in WIRE[wmark:]
    )
    rec("T9 pushed state clears active:false + audio:false", pushed)

    await asyncio.sleep(1.0)  # settle any straggler level broadcasts
    quiet = await drain(WS, 3.0)
    n_levels = sum(1 for m in quiet if m.get("type") == "audio_level")
    rec("T9 audio_level CEASES (shared mic released)", n_levels == 0,
        f"levels after stop={n_levels} in 3s")

    st = await get_state(WS)
    tl = (st or {}).get("timeline") or {}
    rec("T9 state: inactive + audio false", tl.get("active") is False
        and tl.get("audio") is False, f"timeline={tl}")


# ── T10: direction (b) PTT first, session attach mid-press ──────────────────

async def t10_ptt_then_session() -> None:
    print("\n--- T10: (b) PTT held FIRST, then session attaches mid-press ---")
    presses_before = line_count("PTT pressed")
    ptt_down()
    got_press = await wait_line("PTT pressed", presses_before + 1, deadline=4.0)
    rec("T10 PTT pressed (fresh mic start)", got_press)
    levels = await drain(WS, 2.0)
    n1 = sum(1 for m in levels if m.get("type") == "audio_level")
    rec("T10 audio_level flowing under PTT before session", n1 >= 5,
        f"levels={n1} in 2s")

    wmark = len(WIRE)
    reply = await send_and_reply(WS, "begin timeline with audio")
    tools = (reply or {}).get("tools") or []
    txt = str((reply or {}).get("text", ""))
    rec("T10 session attached while PTT held (started with audio)",
        tools == ["start_timeline"] and "started with audio" in txt,
        f"tools={tools} text={txt[:90]!r}")
    await drain(WS, 1.5)  # consume pushed state from the attach
    pushed = any(
        m.get("type") == "state" and (m.get("timeline") or {}).get("audio") is True
        for _t, m in WIRE[wmark:]
    )
    rec("T10 pushed state shows audio:true while PTT held", pushed)

    mid = await drain(WS, 2.0)
    n2 = sum(1 for m in mid if m.get("type") == "audio_level")
    rec("T10 audio_level CONTINUES across attach (voice path not dropped)",
        n2 >= 5, f"levels={n2} in 2s after attach")

    wmark2 = len(WIRE)  # capture window starts BEFORE the injection
    ok, nbytes = await inject_marker(WS)
    rec("T10 marker injected while PTT held + session attached", ok,
        f"bytes={nbytes} hook_lines={line_count('GATE HOOK: injecting')}")
    await asyncio.sleep(0.5)
    ptt_up()

    # broadcast precedes dispatch — check WIRE from before injection once
    # the dispatch line confirms the voice path ran end-to-end.
    dispatched = await wait_line("dispatching without wake word",
                                 line_count("dispatching without wake word") + 1,
                                 deadline=60.0)
    rec("T10 PTT dispatch still executed after attach", dispatched)

    tl: dict = {}
    end = time.time() + 30
    while time.time() < end:
        st = await get_state(WS)
        tl = (st or {}).get("timeline") or {}
        if tl.get("audio") is True and int(tl.get("transcripts") or 0) >= 1:
            break
        await asyncio.sleep(1.5)
    rec("T10 session recorded its transcript (transcripts>=1)",
        tl.get("audio") is True and int(tl.get("transcripts") or 0) >= 1,
        f"timeline={tl}")
    # keep the socket drained while waiting for the voice-path broadcast —
    # a non-reading poll would freeze WIRE and starve pong processing.
    await recv_until(
        WS,
        lambda m: m.get("type") == "transcription"
        and re.search(r"phase|marker", str(m.get("text", "")), re.I) is not None,
        deadline=30.0,
    )
    window = [m for _t, m in WIRE[wmark2:]]
    trans = [m for m in window if m.get("type") == "transcription"
             and re.search(r"phase|marker", str(m.get("text", "")), re.I)]
    rec("T10 voice path still works after attach (transcription broadcast)",
        bool(trans), f"matching={len(trans)} total_msgs={len(window)}")

    base = int(BACKUP.get("base_hits", 0))
    hits, hit_txt = 0, None
    end = time.time() + 60
    while time.time() < end:
        r = await send_and_reply(WS, f"search timeline for {SEARCH_Q}")
        t = str((r or {}).get("text", ""))
        if "match(es)" in t and parse_hits(t) >= base + 3:
            hit_txt, hits = t, parse_hits(t)
            break
        await asyncio.sleep(2.0)
    rec("T10 all marker transcripts searchable (base+3)",
        hits >= base + 3, f"hits={hits} base={base}"
        + ("" if hit_txt else " (no hit in 60s)"))
    rec("T10 daemon alive, no capture/audio crashes",
        DAEMON is not None and DAEMON.poll() is None
        and line_count("MicCapture thread crashed") == 0
        and line_count("timeline session audio subscribe failed") == 0,
        f"exit={DAEMON.poll() if DAEMON else 'gone'}")


# ── T11: attack sim + PCM constraint (session still active) ────────────────

def t11_attack_and_pcm() -> None:
    print("\n--- T11: ATTACK SIM (live db) + zero audio files on disk ---")
    now_snap = audio_files_under(DAEMON_DIR)
    pre = BACKUP.get("audio_snap") or set()
    new = now_snap - set(pre)
    rec("T11 no audio file written under daemon tree during session",
        not new, ", ".join(Path(p).name for p in sorted(new)))

    script = r'''
import sqlite3, sys, time
from pathlib import Path

db = Path(sys.argv[1])
raw = db.read_bytes()
low = raw.lower()
checks, leaks = [], []

conn = sqlite3.connect(str(db), timeout=10)
rows = conn.execute("SELECT id, created_at, payload FROM timeline").fetchall()
conn.close()

checks.append(("rows >= 3", len(rows) >= 3, f"n={len(rows)}"))

for needle in (b"phase 5", b"phase five", b"reads ready", b"marker", b"tl5 scratch"):
    if needle in low:
        leaks.append(needle.decode())
checks.append(("zero plaintext (spoken + screen text)", not leaks, f"leaks={leaks}"))

now = time.time()
sane = all(0 < r[1] <= now + 60 for r in rows)
checks.append(("created_at epochs only (documented leak surface)", sane, f"n={len(rows)}"))

fts_files = [p.name for p in db.parent.glob("*fts*")]
checks.append(("no FTS files on disk", not fts_files, f"files={fts_files}"))

ok = all(c[1] for c in checks)
for name, passed, note in checks:
    print(("ATTACK_OK " if passed else "ATTACK_FAIL ") + name + " -- " + note)
sys.exit(0 if ok else 1)
'''
    tmp = Path(tempfile.mkdtemp()) / "tl5_attack.py"
    tmp.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(tmp), str(TIMELINE_DB)],
        capture_output=True, text=True, timeout=60, cwd=str(DAEMON_DIR),
    )
    shutil.rmtree(tmp.parent, ignore_errors=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.strip().splitlines():
        print(f"  {line}")
    rec("T11 attack sim passes (raw db while session active)",
        proc.returncode == 0 and "ATTACK_FAIL" not in out, f"exit={proc.returncode}")


# ── T12/T13: final stop + restore ───────────────────────────────────────────

async def t12_final_stop() -> None:
    print("\n--- T12: final stop ---")
    wmark = len(WIRE)
    reply = await send_and_reply(WS, "end timeline")
    rec("T12 final stop (audio session closed)",
        "stopped" in str((reply or {}).get("text", "")))
    await drain(WS, 2.0)
    pushed = any(
        m.get("type") == "state" and (m.get("timeline") or {}).get("audio") is False
        for _t, m in WIRE[wmark:]
    )
    rec("T12 pushed state audio:false", pushed)


async def t13_restore() -> None:
    print("\n--- T13: FULL RESTORE ---")
    global PING_TASK
    if PING_TASK is not None:
        PING_TASK.cancel()
        try:
            await PING_TASK
        except (asyncio.CancelledError, Exception):
            pass
        PING_TASK = None
    _VOL_STOP.set()
    if WS is not None:
        try:
            await WS.close()
        except Exception:
            pass
    if DAEMON is not None and DAEMON.poll() is None:
        kill_daemon()
    time.sleep(0.6)  # let the stdout reader drain the final lines

    conv = DAEMON_DIR / "data" / "conversation_history.json"
    if BACKUP["conv"] is not None:
        conv.write_bytes(BACKUP["conv"])  # type: ignore[arg-type]
        rec("T13 conversation restored byte-identical",
            conv.read_bytes() == BACKUP["conv"], f"{conv.stat().st_size}B")
    else:
        conv.unlink(missing_ok=True)
        rec("T13 conversation removed (absent pre-gate)", not conv.exists())

    for side in ("", "-journal", "-wal", "-shm"):
        p = Path(str(TIMELINE_DB) + side)
        if BACKUP["timeline"] is None:
            p.unlink(missing_ok=True)
        elif side == "":
            p.write_bytes(BACKUP["timeline"])  # type: ignore[arg-type]
        else:
            p.unlink(missing_ok=True)
    rec("T13 timeline db back to pre-gate state",
        TIMELINE_DB.exists() == (BACKUP["timeline"] is not None),
        f"present={TIMELINE_DB.exists()}")

    tasks_db = DAEMON_DIR / "data" / "tasks.sqlite3"
    for side in ("", "-journal", "-wal", "-shm"):
        p = Path(str(tasks_db) + side)
        if BACKUP["tasks"] is None:
            p.unlink(missing_ok=True)
        elif side == "":
            p.write_bytes(BACKUP["tasks"])  # type: ignore[arg-type]
        else:
            p.unlink(missing_ok=True)
    rec("T13 tasks db back to pre-gate state",
        tasks_db.exists() == (BACKUP["tasks"] is not None))

    if BACKUP["rules"] is not None:
        rec("T13 watch_rules.json byte-equal",
            RULES_FILE.exists() and RULES_FILE.read_bytes() == BACKUP["rules"],
            f"{RULES_FILE.stat().st_size if RULES_FILE.exists() else 0}B")

    nt = notepad_titles()
    if nt and not USER_NOTEPAD_OPEN and all(SCRATCH_PREFIX in t for t in nt):
        await REG.get("app_kill").handler({"query": "notepad"})
        await asyncio.sleep(0.5)
        nt = notepad_titles()
    rec("T13 scratch Notepad closed (user Notepad untouched)",
        not any(SCRATCH_PREFIX in t for t in nt), f"remaining={nt}")
    SCRATCH_FILE.unlink(missing_ok=True)
    SAPI_TMP.unlink(missing_ok=True)

    render = AudioUtilities.GetSpeakers().EndpointVolume
    cap = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone()).EndpointVolume
    lvl = render.GetMasterVolumeLevelScalar()
    sys_m, mic_m = bool(render.GetMute()), bool(cap.GetMute())
    changed = (abs(lvl - float(ORIG["level"])) >= 0.001
               or sys_m is not bool(ORIG["mute"])
               or mic_m is not bool(ORIG["mic"]))
    vol_tool = any(re.search(r"ran tools \[[^\]]*(set_volume|set_mic|set_mute)", l)
                   for l in LINES)
    static_bad = scan_volume_writes()
    if not changed:
        rec("T13 audio untouched (level/sysmute/micmute)", True,
            f"vol={lvl:.4f} sys={sys_m} mic={mic_m}")
    elif not vol_tool and not static_bad:
        changes = [(time.strftime("%H:%M:%S", time.localtime(t)), l, m)
                   for t, l, m in VOL_CHANGES]
        rec("T13 audio changed EXTERNALLY only (user/device); daemon never wrote volume",
            True,
            f"vol {float(ORIG['level']):.4f}->{lvl:.4f} sys {bool(ORIG['mute'])}->{sys_m} "
            f"mic {bool(ORIG['mic'])}->{mic_m}; watcher saw {len(changes)} change(s): "
            f"{changes[:4]}; no set_volume/set_mic/set_mute tool ran; static scan clean")
    else:
        rec("T13 audio untouched (level/sysmute/micmute)", False,
            f"vol={lvl:.4f} sys={sys_m} mic={mic_m} vol_tool_ran={vol_tool} "
            f"static_writes={static_bad}")

    final_snap = audio_files_under(DAEMON_DIR)
    new = final_snap - set(BACKUP.get("audio_snap") or set())
    residue = list(DAEMON_DIR.glob("*.tmp")) + list((DAEMON_DIR / "data").glob("*.tmp"))
    rec("T13 no .tmp residue + no new audio files", not residue and not new,
        ", ".join(p.name for p in residue + [Path(x) for x in new]))

    used = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port in (8765, PORT)]
    rec("T13 gate ports free after kill", not used, f"listening={used}")

    me = os.getpid()
    strays = [p.pid for p in psutil.process_iter(["pid", "cmdline"])
              if p.pid != me
              and any(str(a).endswith("run_daemon.py") for a in (p.info["cmdline"] or []))]
    rec("T13 no stray daemons", not strays, f"pids={strays}")

    # evidence: shared capture must not cause unexpected dispatches/side effects
    wake = line_count("Wake word detected")
    rec("T13 no wake-word dispatch fired (no hallucination side effects)",
        wake == 0, f"count={wake} (daemon log: {DAEMON_LOG})")
    allowed = {"start_timeline", "stop_timeline", "search_timeline", "web_search"}
    ran: list[str] = []
    for l in LINES:
        m = re.search(r"ran tools \[([^\]]*)\]", l)
        if m:
            ran.append(m.group(1))
    bad = [r for r in ran
           if not set(re.findall(r"'([a-z_]+)'", r)) <= allowed]
    rec("T13 orchestrator ran ONLY wire/PTT-marker tools", not bad,
        f"runs={ran}" if not bad else f"UNEXPECTED={bad} runs={ran}")
    bad_pings = [(time.strftime("%H:%M:%S", time.localtime(t)), r)
                 for t, r in PINGS if r is None or r >= 8.0]
    worst = max((r for _t, r in PINGS if r is not None), default=0.0)
    rec("T13 no server keepalive stall (ping RTT < 8s throughout)",
        bool(PINGS) and not bad_pings,
        f"pings={len(PINGS)} worst={worst * 1000:.0f}ms"
        + (f" stalls={bad_pings[:3]}" if bad_pings else ""))

    hooks = line_count("GATE HOOK: injecting")
    rec("T13 exactly 3 env-gated hook injections logged by daemon", hooks == 3,
        f"hook_lines={hooks}")

    rec("T13 no websocket errors during gate", not WS_ERRS,
        "; ".join(WS_ERRS[:2]))
    rec("T13 daemon stdout log persisted for audit", DAEMON_LOG.exists(),
        str(DAEMON_LOG))


# ── main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    os.chdir(DAEMON_DIR)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("TL5 GATE - Session audio (real daemon, Amendment 5 both directions)")
    print("NOTE: utterances are delivered via env-gated hook (silent, no speakers).")
    try:
        t1_spawn()
        await t2_state()
        await t3_routing()
        t4_hud()
        await t5_setup_notepad()
        await t6_start_audio()
        await t7_ptt_during_session()
        await t8_subsequent_capture()
        await t9_stop_releases_mic()
        await t10_ptt_then_session()
        t11_attack_and_pcm()
        await t12_final_stop()
    finally:
        await t13_restore()
        for k in ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_TIMELINE_INTERVAL",
                  "BS_TIMELINE_ENABLED", "BS_TIMELINE_TTL_HOURS",
                  "BS_GATE_HOOKS"):
            os.environ.pop(k, None)
        print("  env cleaned")


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
