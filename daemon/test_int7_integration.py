"""Phase 7 gate — ALL six expansion features + sandbox on ONE daemon (port 8807).

The P7 plan, verbatim intent: a single daemon, walked through every feature
in sequence over ONE websocket, proving they coexist — pill show/hide ->
diagnosis propose/confirm -> stress on/off utterances -> camera session +
wave -> streaming chat + barge-in -> switch to PTT -> sandbox benign + the
four containment cases — with a stability assertion after every element,
ports clean at the end, lifecycle BACKUP restored, GATE_RESULT + exit code.

T0  port 8807 free (pre-spawn) + BACKUP: conversation/tasks/timeline/rules
    bytes + rag lifecycle flag.
T1  spawn the SINGLE daemon: BS_VOICE_MODE=streaming, BS_TTS_ENABLED=True,
    BS_GATE_HOOKS=1, BS_SANDBOX_ENABLED=1 with BS_SANDBOX_TIMEOUT=2.5 /
    BS_SANDBOX_MEMORY_MB=64 (containment limits ride env — the daemon is a
    separate process, so no in-gate cfg mutation is possible), monitor
    tuned so only CRASH anomalies can fire (cpu/gpu 101, battery -1),
    BS_DIAGNOSIS_COOLDOWN=5, REAL Ollama at 127.0.0.1:11434 (integration
    means the real model brain). Assert: IPC listening, streaming pipeline
    selected (and ptt baseline line absent), STT ready, full-duplex active,
    PTT hotkey line, diagnostics service started, no STT degradation.
T2  ws connect + wire drain: on-connect camera_state active:false.
    S1 stability (ping -> pong + process alive).
B1  PILL SHOW/HIDE: spawn a uniquely-titled top-level target window;
    overlay_show -> ack + overlay broadcast carrying x/y that match the
    GATE's own physical-pixel computation (per-monitor-v2 DPI thread, the
    same formula as blankslate/overlay.py) and target_hwnd ground truth;
    overlay_hide -> ack + broadcast action=hide; unknown window -> error.
    S2.
B2  DIAGNOSIS PROPOSE/CONFIRM (real LLM): crash app (Add-Type null deref)
    -> AV exit code -> Application-log Event 1000 (win32evtlog ground
    truth) -> diagnosis broadcast kind=crash (up to 3 attempts with fresh
    image paths; per-path cooldown, LLM failure = no broadcast = retry) ->
    negative proof (nothing executed, no bs_int7_crash_* process) ->
    diagnose_confirm -> ack + executed broadcast with relaunched_pid (the
    relaunched marker-sleeper verified alive via psutil) -> second confirm
    rejected (illegal transition). S3.
B3  STRESS ON/OFF: rushed SAPI utterance delivered under PTT -> final
    transcription + transcription_stress band=rushed + assistant_response
    + "stress bias applied" daemon log +1; set_stress_adaptation
    {enabled:false} -> ack; second rushed utterance -> stress STILL
    computed and broadcast (advisory), bias log count unchanged, reply
    still arrives; re-enable -> ack. S4.
B4  CAMERA + WAVE: set_gesture_mode {enabled:true} -> ack + camera_state
    active:true; inject_gesture wave (BS_GATE_HOOKS=1) -> ack +
    gesture_event {wave, hide_overlay} + overlay broadcast action=hide
    (the P1 pill path reused by the P4 action map); set_gesture_mode
    {enabled:false} -> camera_state active:false. S5.
B5  STREAMING CHAT + BARGE-IN: PTT-held utterance #1 -> inject ack ->
    transcription_partial {final:false} -> transcription -> assistant_response
    (source=voice) -> tts_state {speaking:true} (first TTS model load may
    take minutes); utterance #2 injected mid-playback -> tts_state
    {speaking:false, interrupted:true} -> partials -> transcription ->
    NEW assistant_response turn; "barge-in" daemon log line; ping. S6.
B6  SWITCH TO PTT (additive hotkey inside the SAME streaming daemon):
    (a) NO-PTT utterance without wake word -> transcription on the wire but
        NO dispatch (no assistant_response, no "dispatching without wake
        word") — the gate is real; (b) keybd_event ctrl+alt+space ->
        "PTT pressed" observed by pynput -> utterance -> "dispatching
        without wake word" + assistant_response — force-dispatch works.
    The separate full test_tl5_audio.py re-run (check (a)) covers the PTT
    baseline daemon. S7.
B7  SANDBOX ON THE SAME DAEMON: propose -> sandbox_proposal broadcast with
    all §5 fields and limits reflecting the spawn env; propose-alone
    negative (no run dir, no process — single spawn point preserved);
    confirm benign -> executed exit 0 + stdout captured + scratch
    persisted; double-confirm and dismiss-after-executed -> illegal;
    then the four containment cases: wall-clock deadline (reason=deadline,
    timed_out, ~2.5 s, runner "watchdog fired" log in daemon stdout),
    memory bomb (reason=memory), write-outside (non-zero + PermissionError
    + escape file never created), outbound network (NET-OPEN absent,
    firewall-family error, child did not hang to budget); daemon survival
    ping; gate-created run dirs removed. S8.
T-final: target window destroyed, relaunched + stray crash processes
    killed, ws closed, daemon killed, port 8807 free, no stray daemons,
    BACKUP restored byte-identical, sandbox/escape/SAPI residue gone, no
    gate env leaked into os.environ, GATE_RESULT + exit code.

Repeated checks (pasted separately by the caller): (a) full
test_tl5_audio.py, (b) the four containment recs inside B7 above,
(c) test_new5_isolation.py.

Ground truth: raw WS wire messages, daemon stdout, win32evtlog Application
log reads, psutil process tables, win32 window rects measured in the same
physical-pixel space the daemon uses, file existence, exit codes — never
assumptions. One daemon, one ws, one drain for the entire sequence.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import websockets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(__file__).resolve().parent
PORT = 8807

GATE_T0 = time.time()

# Voice material (wake words are "hey blank" / "blank slate" / "hey
# assistant" — none of these utterances contain one).
UTT_STRESS1 = "Tell me what causes the northern lights in the sky."
UTT_STRESS2 = "What is the boiling point of water?"
UTT1 = "Tell me a long story about a lighthouse keeper and the stormy sea."
UTT2 = "Stop talking please, that is enough."
UTT_NEG = "The cobalt ferry drifted past the amber pier."
UTT_POS = "Please describe the weather pattern over the ocean."

STRESS1_KEYS = ("northern", "lights", "aurora", "sky")
STRESS2_KEYS = ("boiling", "water")
UTT1_KEYS = ("lighthouse", "story", "long", "tell", "stormy", "sea")
UTT2_KEYS = ("stop", "talking", "enough", "please")
NEG_KEYS = ("cobalt", "ferry", "amber", "pier")
POS_KEYS = ("weather", "ocean", "pattern", "describe")

RUSHED_RATE = 8

SAPI_TMP = Path(tempfile.gettempdir()) / "int7_gate_sapi.wav"
DAEMON_LOG = Path(tempfile.gettempdir()) / "int7_daemon_stdout.log"
VK_CTRL, VK_ALT, VK_SPACE = 0x11, 0x12, 0x20
KEYUP = 0x0002
_u32 = None

PILL_W, PILL_H = 240, 56
TARGET_TITLE = f"BS Int7 Target {os.getpid()}"

CONV = DAEMON_DIR / "data" / "conversation_history.json"
TASKS = DAEMON_DIR / "data" / "tasks.sqlite3"
TIMELINE_DB = DAEMON_DIR / "data" / "timeline.sqlite3"
RULES_FILE = DAEMON_DIR / "data" / "watch_rules.json"
SANDBOX_DIR = DAEMON_DIR / "data" / "sandbox"
ESCAPE_FILE = DAEMON_DIR / "data" / "escape_int7.txt"

RESULTS: list[tuple[str, bool, str]] = []
LINES: list[str] = []
WIRE: list[tuple[float, dict[str, Any]]] = []
BACKUP: dict[str, object] = {}
USED_IDS: list[str] = []
KILLED_PIDS: list[int] = []
SCRATCH_DIRS: list[Path] = []

DAEMON: subprocess.Popen | None = None
DRAIN_TASK: asyncio.Task | None = None
TARGET: dict[str, Any] = {"hwnd": 0, "ready": threading.Event()}


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


async def stable(ws: Any, tag: str) -> None:
    ok = await ping(ws)
    alive = DAEMON is not None and DAEMON.poll() is None
    rec(f"{tag} daemon stable (ping -> pong + never crashed)", ok and alive,
        f"pong={ok} alive={alive} exit={DAEMON.poll() if DAEMON else 'gone'}")


def has_key(text: str, keys: tuple[str, ...]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in keys)


def is_ack_setting(setting: str, **fields: Any) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        if not (m.get("type") == "ack" and m.get("setting") == setting):
            return False
        return all(m.get(k) == v for k, v in fields.items())
    return pred


def is_error_detail(substr: str) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        return m.get("type") == "error" and substr in str(m.get("detail", ""))
    return pred


# ── spawn / kill ────────────────────────────────────────────────────────────

def spawn_int7() -> bool:
    """The ONE daemon for the whole gate (fd5/dg2 house pattern)."""
    global DAEMON
    mark = len(LINES)
    DAEMON_LOG.unlink(missing_ok=True)
    env = dict(os.environ)
    for k in ("BS_VOICE_MODE", "BS_TTS_ENABLED", "BS_GATE_HOOKS",
              "BS_IPC_AUTH", "BS_IPC_PORT", "BS_SANDBOX_ENABLED",
              "BS_SANDBOX_TIMEOUT", "BS_SANDBOX_MEMORY_MB",
              "BS_SANDBOX_PROPOSAL_TTL", "BS_STRESS_ADAPTATION_ENABLED",
              "BS_DIAGNOSTICS_ENABLED", "BS_DIAGNOSIS_COOLDOWN",
              "BS_MONITOR_POLL_INTERVAL", "BS_MONITOR_CPU_THRESHOLD",
              "BS_MONITOR_GPU_THRESHOLD", "BS_MONITOR_BATTERY_THRESHOLD",
              "BS_MONITOR_SUSTAINED_SAMPLES", "BS_GESTURE_ENABLED",
              "BS_STREAMING_PARTIAL_INTERVAL", "BS_STREAMING_BARGE_IN_MS",
              "BS_STREAMING_BARGE_LEVEL"):
        env.pop(k, None)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_VOICE_MODE": "streaming",
        "BS_TTS_ENABLED": "True",
        "BS_GATE_HOOKS": "1",
        "BS_SANDBOX_ENABLED": "1",
        "BS_SANDBOX_TIMEOUT": "2.5",
        "BS_SANDBOX_MEMORY_MB": "64",
        # Crash is the ONLY anomaly input (P7 plan): silence cpu/gpu/battery.
        "BS_MONITOR_POLL_INTERVAL": "0.5",
        "BS_MONITOR_CPU_THRESHOLD": "101",
        "BS_MONITOR_GPU_THRESHOLD": "101",
        "BS_MONITOR_BATTERY_THRESHOLD": "-1",
        "BS_MONITOR_SUSTAINED_SAMPLES": "2",
        "BS_DIAGNOSIS_COOLDOWN": "5",
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


# ── speech + PTT helpers (tl5/fd5/st3-proven machinery) ─────────────────────

def synthesize(text: str, rate: int = 0) -> bytes:
    """Offline Windows SAPI -> wav bytes at the given voice rate. SILENT:
    the stream is bound to the temp file only, never to the output device."""
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


def inject_payload(text: str, rate: int = 0) -> dict[str, Any]:
    pcm = wav_bytes_to_pcm16k(synthesize(text, rate))
    return {
        "type": "inject_speech_chunk",
        "pcm_b64": base64.b64encode(pcm).decode("ascii"),
    }


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


async def settle_tts(start: int, cap: float = 120.0) -> bool:
    """After a reply we do not want TTS speaking into the next block."""
    spoke = any(m.get("type") == "tts_state" and m.get("speaking")
                for _t, m in WIRE[start:])
    if not spoke:
        return True
    return await wait_wire(
        lambda m: m.get("type") == "tts_state" and not m.get("speaking"),
        cap, start) is not None


# ── target window for the pill (ov1 machinery, click-log stripped) ──────────

def create_target_window() -> bool:
    def worker() -> None:
        import win32con
        import win32gui

        hinst = win32gui.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hInstance = hinst
        wc.lpszClassName = f"BSInt7Target{os.getpid()}"
        wc.lpszMenuName = ""
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW + 1

        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        wc.lpfnWndProc = wndproc
        win32gui.RegisterClass(wc)
        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST, wc.lpszClassName, TARGET_TITLE,
            win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
            80, 80, 420, 300, 0, 0, hinst, None,
        )
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        TARGET["hwnd"] = hwnd
        TARGET["ready"].set()
        win32gui.PumpMessages()

    threading.Thread(target=worker, daemon=True).start()
    return bool(TARGET["ready"].wait(10))


def measure_expected_pill() -> dict[str, int]:
    """Gate-side physical-pixel measurement: same thread DPI context
    (per-monitor-v2) and the same formula as blankslate/overlay.py."""
    out: dict[str, int] = {}

    def work() -> None:
        import ctypes
        import win32gui

        try:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(
                ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        except Exception:
            pass
        hwnd = int(TARGET["hwnd"])
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        x = right - PILL_W - 16
        y = top + 16
        if x < left + 8:
            x = left + 8
        if y + PILL_H > bottom - 8:
            y = max(left, bottom - 8 - PILL_H)
        u = ctypes.windll.user32
        vx = u.GetSystemMetrics(76)
        vy = u.GetSystemMetrics(77)
        vw = u.GetSystemMetrics(78)
        vh = u.GetSystemMetrics(79)
        x = max(vx, min(x, vx + vw - PILL_W))
        y = max(vy, min(y, vy + vh - PILL_H))
        out["x"] = int(x)
        out["y"] = int(y)

    th = threading.Thread(target=work)
    th.start()
    th.join(5)
    return out


def destroy_target_window() -> bool:
    hwnd = int(TARGET.get("hwnd") or 0)
    if not hwnd:
        return True
    try:
        import win32con
        import win32gui

        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        return False
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            import win32gui

            if not win32gui.IsWindow(hwnd):
                return True
        except Exception:
            return True
        time.sleep(0.2)
    return False


# ── crash app / event-log ground truth (dg2 machinery) ──────────────────────

CRASH_CS = """
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
public class {cls} {{
    public static void Main(string[] args) {{
        string exe = Process.GetCurrentProcess().MainModule.FileName;
        string marker = exe + ".marker";
        if (File.Exists(marker)) {{ Thread.Sleep(120000); return; }}
        File.WriteAllText(marker, "1");
        Action boom = (Action)Marshal.GetDelegateForFunctionPointer(
            new IntPtr(0xDEADBEEF), typeof(Action));
        boom();
    }}
}}
"""


def _utc_since(epoch: float) -> str:
    t = datetime.fromtimestamp(epoch, tz=timezone.utc) - timedelta(seconds=2)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def compile_crash_app(scratch: Path, name: str) -> Path | None:
    exe = scratch / name
    cls = "CrashInt7" + "".join(ch for ch in name if ch.isdigit())
    src = CRASH_CS.format(cls=cls)
    ps = (
        f"$src = @'\n{src}\n'@; "
        f"Add-Type -TypeDefinition $src -OutputAssembly '{exe}' "
        f"-OutputType ConsoleApplication"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    return exe if proc.returncode == 0 and exe.exists() else None


def spawn_crash(exe: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [str(exe)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


async def wait_event1000(name: str, timeout: float) -> str | None:
    """Gate-side ground truth: real Application-log read for Event 1000."""
    import pywintypes
    import win32evtlog

    xpath = (
        "*[System[(EventID=1000) and "
        f"TimeCreated[@SystemTime>='{_utc_since(GATE_T0)}']]]"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            query = win32evtlog.EvtQuery(
                "Application", win32evtlog.EvtQueryForwardDirection, xpath)
            found = None
            while True:
                try:
                    handles = win32evtlog.EvtNext(query, 64, 2000)
                except pywintypes.error:
                    break
                if not handles:
                    break
                for h in handles:
                    xml = win32evtlog.EvtRender(h, win32evtlog.EvtRenderEventXml)
                    if name in xml:
                        found = xml
                        break
                if found:
                    break
            if found:
                return found
        except Exception:
            pass
        await asyncio.sleep(1.0)
    return None


def process_by_name_prefix(prefix: str) -> list[int]:
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = (p.info["name"] or "").lower()
            if n.startswith(prefix.lower()):
                out.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def is_diagnosis(m: dict[str, Any]) -> bool:
    return m.get("type") == "monitor_alert" and m.get("category") == "diagnosis"


def _crash_diagnosis_for(exe: Path) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        return (is_diagnosis(m)
                and m.get("status") == "awaiting_confirmation"
                and isinstance(m.get("anomaly"), dict)
                and m["anomaly"].get("kind") == "crash"
                and str(m["anomaly"].get("image_path", "")).lower()
                .endswith(exe.name.lower()))
    return pred


# ── sandbox wire helpers (sb6 §5 sequences against the live daemon) ─────────

async def sb_propose(ws: Any, script: str, purpose: str,
                     timeout: float = 20.0) -> dict[str, Any] | None:
    m = await send_expect(
        ws, {"type": "sandbox_propose", "script": script, "purpose": purpose},
        lambda x: x.get("type") in ("sandbox_proposal", "error"), timeout)
    if m is not None and m.get("type") == "sandbox_proposal":
        USED_IDS.append(str(m.get("id", "")))
        return m
    return None


async def sb_confirm_id(ws: Any, pid: str,
                        timeout: float = 45.0) -> dict[str, Any] | None:
    return await send_expect(
        ws, {"type": "sandbox_confirm", "id": pid},
        lambda x: (x.get("type") == "sandbox_result" and x.get("id") == pid)
        or x.get("type") == "error", timeout)


def psutil_has(marker: str) -> bool:
    for p in psutil.process_iter(["cmdline"]):
        try:
            cl = p.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(marker in part for part in cl):
            return True
    return False


# ── T0: port + BACKUP ───────────────────────────────────────────────────────

def t0_pre() -> None:
    print("\n--- T0: port 8807 free + BACKUP snapshot ---")
    busy = [c.laddr.port for c in psutil.net_connections(kind="tcp")
            if getattr(c, "laddr", None) and c.status == "LISTEN"
            and c.laddr.port == PORT]
    rec("T0 port 8807 free pre-spawn", not busy, f"listening={busy}")

    for key, path in (("conv", CONV), ("tasks", TASKS),
                      ("timeline", TIMELINE_DB), ("rules", RULES_FILE)):
        BACKUP[key] = path.read_bytes() if path.exists() else None
    BACKUP["rag_existed"] = (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists()
    print(f"  BACKUP: conv={'bytes' if BACKUP['conv'] is not None else 'absent'} "
          f"tasks={'bytes' if BACKUP['tasks'] is not None else 'absent'} "
          f"timeline={'bytes' if BACKUP['timeline'] is not None else 'absent'} "
          f"rules={'bytes' if BACKUP['rules'] is not None else 'absent'} "
          f"rag_existed={BACKUP['rag_existed']}")


# ── T1/T2: spawn + connect ──────────────────────────────────────────────────

def t1_spawn() -> bool:
    print("\n--- T1: single daemon spawn (streaming + hooks + sandbox env) ---")
    ok = spawn_int7()
    rec("T1 daemon IPC listening on gate port", ok,
        f"port={PORT} exit={DAEMON.poll() if DAEMON else 'gone'} lines={len(LINES)}")
    if not ok:
        return False
    rec("T1 stdout: streaming pipeline selected (voice mode: streaming)",
        any("voice mode: streaming (full-duplex StreamingVoicePipeline)" in l
            for l in LINES),
        next((l.strip()[-70:] for l in LINES if "voice mode" in l), "MISSING"))
    rec("T1 stdout: ptt baseline line absent (not double-constructed)",
        line_count("voice mode: ptt") == 0, "")
    rec("T1 stdout: diagnostics service started",
        line_count("diagnostics: started") >= 1,
        next((l.strip()[-70:] for l in LINES if "diagnostics:" in l), "MISSING"))
    rec("T1 stdout: PTT hotkey additive listener started",
        line_count("PTT started") >= 1, "")
    return True


async def t2_connect() -> Any:
    global DRAIN_TASK
    print("\n--- T2: ws subscribe + readiness + on-connect camera_state ---")
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
    await asyncio.sleep(0.5)

    ok = await wait_line("Streaming pipeline: STT ready", 1, 180.0)
    rec("T2 stdout: STT ready (streaming pipeline)", ok,
        next((l.strip()[-60:] for l in LINES if "STT ready" in l), "MISSING"))
    degraded = line_count("STT degraded") > 0
    rec("T2 stdout: no STT degradation", not degraded, "")
    rec("T2 stdout: full-duplex active",
        line_count("Streaming pipeline: full-duplex active") >= 1, "")

    cam = await wait_wire(
        lambda m: m.get("type") == "camera_state"
        and m.get("active") is False, 8.0, 0)
    rec("T2 on-connect camera_state {active:false} pushed",
        cam is not None, f"msg={cam}")
    rec("T2 gate ws subscribed (drain running) + ping -> pong",
        await ping(ws), f"wire={len(WIRE)}")
    return ws


# ── B1: pill show/hide ──────────────────────────────────────────────────────

async def b1_pill(ws: Any) -> None:
    print("\n--- B1: pill show/hide (overlay_show/hide + physical-pixel truth) ---")
    created = create_target_window()
    rec("B1 target window created + visible", created
        and bool(TARGET.get("hwnd")), f"hwnd={TARGET.get('hwnd')}")
    if not created:
        for nm in ("B1 overlay_show -> ack+broadcast (action=show, x/y)",
                   "B1 gate-computed physical pill origin == daemon ack x/y",
                   "B1 overlay_hide -> ack+broadcast (action=hide)",
                   "B1 unknown window -> error"):
            rec(nm, False, "skipped: no target window")
        return

    expected = measure_expected_pill()
    rec("B1 gate measured expected pill origin (per-monitor-v2 thread)",
        expected.get("x") is not None, f"expected={expected}")

    idx = len(WIRE)
    ack = await send_expect(
        ws, {"type": "overlay_show", "target_title": TARGET_TITLE,
             "text": "integration pill"},
        lambda m: (m.get("type") == "ack" and m.get("setting") == "overlay")
        or m.get("type") == "error", 12.0)
    evt = await wait_wire(
        lambda m: m.get("type") == "overlay" and m.get("action") == "show",
        5.0, idx)
    rec("B1 overlay_show -> ack + overlay broadcast (action=show)",
        ack is not None and ack.get("action") == "show" and evt is not None,
        f"ack_action={(ack or {}).get('action')} evt={evt}")

    ax, ay = int((ack or {}).get("x", -1)), int((ack or {}).get("y", -1))
    ex, ey = int(expected.get("x", -9999)), int(expected.get("y", -9999))
    rec("B1 gate-computed physical pill origin == daemon ack x/y",
        abs(ax - ex) <= 2 and abs(ay - ey) <= 2,
        f"ack=({ax},{ay}) expected=({ex},{ey})")
    rec("B1 ack target_hwnd == our window (win32 ground truth)",
        int((ack or {}).get("target_hwnd", 0)) == int(TARGET["hwnd"]),
        f"ack_hwnd={(ack or {}).get('target_hwnd')} ours={TARGET['hwnd']}")
    rec("B1 broadcast carries identical coords + text",
        evt is not None and int(evt.get("x", -1)) == ax
        and int(evt.get("y", -1)) == ay
        and str(evt.get("text", "")) == "integration pill",
        f"evt=({(evt or {}).get('x')},{(evt or {}).get('y')})")

    idx = len(WIRE)
    ack2 = await send_expect(
        ws, {"type": "overlay_hide"},
        lambda m: (m.get("type") == "ack" and m.get("setting") == "overlay")
        or m.get("type") == "error", 12.0)
    evt2 = await wait_wire(
        lambda m: m.get("type") == "overlay" and m.get("action") == "hide",
        5.0, idx)
    rec("B1 overlay_hide -> ack + overlay broadcast (action=hide)",
        ack2 is not None and ack2.get("action") == "hide" and evt2 is not None,
        f"ack={ack2} evt={evt2}")

    err = await send_expect(
        ws, {"type": "overlay_show", "target_title": "__NoSuchWindowZZ__",
             "text": "nope"},
        lambda m: m.get("type") == "error", 12.0)
    rec("B1 unknown window -> error (no visible window matches)",
        err is not None and "no visible window" in str(err.get("detail", "")),
        f"detail={(err or {}).get('detail')}")


# ── B2: diagnosis propose/confirm (crash trigger, real LLM) ─────────────────

async def b2_diagnosis(scratch: Path) -> dict[str, Any] | None:
    print("\n--- B2: diagnosis propose/confirm (crash -> Event 1000 -> LLM) ---")
    diag: dict[str, Any] | None = None
    any_compile = any_av = any_ev = False
    last_rc: int | None = None

    # qwen2.5:0.5b sometimes returns non-whitelisted action spellings
    # ("Relaunch the application") which the daemon correctly refuses to
    # broadcast — retry the full crash cycle with fresh image paths, and cut
    # the wait short the moment the daemon logs the whitelist rejection.
    for attempt in range(1, 9):
        print(f"  (crash attempt {attempt}/8)")
        exe = compile_crash_app(
            scratch, f"bs_int7_crash_{os.getpid()}_{attempt}.exe")
        if exe is None:
            continue
        any_compile = True
        lines_mark = len(LINES)
        proc = spawn_crash(exe)
        try:
            last_rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            last_rc = None
        if last_rc != 3221225477:
            continue
        any_av = True
        xml = await wait_event1000(exe.name, 30)
        if xml is None:
            continue
        any_ev = True
        diag = None
        deadline = time.time() + 45
        while time.time() < deadline:
            diag = await wait_wire(_crash_diagnosis_for(exe), 1.0)
            if diag is not None:
                break
            if any("not in whitelist" in l for l in LINES[lines_mark:]):
                diag = None
                break
        if diag is not None:
            break
        await asyncio.sleep(6)

    rec("B2 crash app compiled (Add-Type)", any_compile, "")
    rec("B2 crash app died with AV exit code (0xC0000005)",
        any_av, f"rc={last_rc}")
    rec("B2 Event 1000 names the faulting exe (win32evtlog ground truth)",
        any_ev, "")
    rec("B2 diagnosis broadcast (kind=crash, real LLM proposal)",
        diag is not None,
        f"id={(diag or {}).get('id')} anomaly={(diag or {}).get('anomaly')}")

    if diag is None:
        for nm in ("B2 proposal fields non-empty + relaunch path == image_path "
                   "(daemon ground truth)",
                   "B2 negative proof: nothing executed, no process spawned",
                   "B2 confirm -> ack status=executed with relaunched_pid",
                   "B2 executed broadcast for id",
                   "B2 relaunched process alive (psutil marker-sleeper)",
                   "B2 second confirm rejected (illegal transition)"):
            rec(nm, False, "skipped: no diagnosis")
        return None

    did = str(diag.get("id", ""))
    path = str((diag.get("action_params") or {}).get("path", ""))
    img = str((diag.get("anomaly") or {}).get("image_path", ""))
    rec("B2 proposal fields non-empty + relaunch path == image_path "
        "(daemon ground truth)",
        bool(diag.get("summary")) and bool(diag.get("cause"))
        and bool(diag.get("proposal")) and diag.get("action") == "relaunch_app"
        and path == img and img != "",
        f"action={diag.get('action')} path={path!r}")

    await asyncio.sleep(3.0)
    executed = any(is_diagnosis(m) and m.get("id") == did
                   and m.get("status") in ("executed", "dismissed")
                   for _t, m in WIRE)
    stray = process_by_name_prefix("bs_int7_crash_")
    rec("B2 negative proof: nothing executed, no process spawned",
        not executed and not stray, f"executed={executed} pids={stray}")
    return diag


async def b2_confirm_part(ws: Any, diag: dict[str, Any]) -> None:
    did = str(diag.get("id", ""))
    ack = await send_expect(
        ws, {"type": "diagnose_confirm", "id": did},
        lambda m: (m.get("type") == "ack" and m.get("setting") == "diagnosis")
        or m.get("type") == "error", 15.0)
    rec("B2 confirm -> ack status=executed with relaunched_pid",
        ack is not None and ack.get("status") == "executed"
        and int(ack.get("relaunched_pid") or 0) > 0, f"ack={ack}")

    evt = await wait_wire(
        lambda m: is_diagnosis(m) and m.get("id") == did
        and m.get("status") == "executed", 8.0)
    rec("B2 executed broadcast for id", evt is not None,
        f"pid={(evt or {}).get('relaunched_pid')}")

    pid = int((ack or {}).get("relaunched_pid") or 0)
    alive = name_ok = False
    if pid > 0:
        for _ in range(10):
            try:
                p = psutil.Process(pid)
                alive = p.is_running()
                name_ok = (p.name() or "").lower().startswith("bs_int7_crash_")
                if alive and name_ok:
                    break
            except psutil.NoSuchProcess:
                pass
            await asyncio.sleep(0.3)
    rec("B2 relaunched process alive (psutil marker-sleeper)",
        bool(alive and name_ok), f"pid={pid} alive={alive} name_ok={name_ok}")
    if pid > 0:
        KILLED_PIDS.append(pid)

    again = await send_expect(
        ws, {"type": "diagnose_confirm", "id": did},
        is_error_detail("illegal transition"), 10.0)
    rec("B2 second confirm rejected (illegal transition)", again is not None,
        f"error={(again or {}).get('detail')}")


# ── B3: stress on/off ───────────────────────────────────────────────────────

async def _voice_turn(ws: Any, text: str, rate: int) -> dict[str, Any]:
    """PTT-held utterance (fd5 hold-through pattern): press, inject, hold
    until the turn's assistant_response, release."""
    presses_before = line_count("PTT pressed")
    start = len(WIRE)
    ptt_down()
    try:
        await wait_line("PTT pressed", presses_before + 1, 6.0)
        await asyncio.sleep(0.3)
        ack = await send_expect(
            ws, inject_payload(text, rate),
            lambda m: (m.get("type") == "ack"
                       and m.get("setting") == "inject_speech_chunk")
            or m.get("type") == "error", 15.0)
        trans = await wait_wire(
            lambda m: m.get("type") == "transcription"
            and has_key(str(m.get("text", "")), _keys_for(text)), 45.0, start)
        stress = await wait_wire(
            lambda m: m.get("type") == "transcription_stress", 30.0, start)
        reply = await wait_wire(
            lambda m: m.get("type") == "assistant_response"
            and m.get("source") == "voice", 120.0, start)
    finally:
        ptt_up()
    return {"start": start, "ack": ack, "trans": trans, "stress": stress,
            "reply": reply, "ptt_seen": line_count("PTT pressed") > presses_before}


def _keys_for(text: str) -> tuple[str, ...]:
    if text == UTT_STRESS1:
        return STRESS1_KEYS
    if text == UTT_STRESS2:
        return STRESS2_KEYS
    if text == UTT1:
        return UTT1_KEYS
    if text == UTT2:
        return UTT2_KEYS
    if text == UTT_NEG:
        return NEG_KEYS
    if text == UTT_POS:
        return POS_KEYS
    return ("",)


async def b3_stress(ws: Any) -> None:
    print("\n--- B3: stress on/off (rushed utterances under PTT) ---")
    n0 = line_count("stress bias applied")

    d1 = await _voice_turn(ws, UTT_STRESS1, RUSHED_RATE)
    rec("B3 utt1 inject ack", d1["ack"] is not None
        and d1["ack"].get("type") == "ack", f"ack={d1['ack']}")
    rec("B3 utt1 final transcription matches",
        d1["trans"] is not None
        and has_key(str(d1["trans"].get("text", "")), STRESS1_KEYS),
        f"text={(d1['trans'] or {}).get('text')}")
    s1 = d1["stress"] or {}
    u1 = float(s1.get("urgency", -1.0))
    rec("B3 utt1 transcription_stress band=rushed (real numbers)",
        s1.get("band") == "rushed" and 0.0 < u1 <= 1.0
        and bool(s1.get("features")),
        f"urgency={u1:.4f} band={s1.get('band')} features={s1.get('features')}")
    rec("B3 utt1 assistant_response (source=voice)",
        d1["reply"] is not None,
        f"text={str((d1['reply'] or {}).get('text', ''))[:60]!r}")
    rec("B3 utt1 'stress bias applied' logged (judge ground truth)",
        await wait_line("stress bias applied", n0 + 1, 10.0),
        f"count={line_count('stress bias applied')} (was {n0})")
    # The FIRST reply triggers the first pocket_tts model load, which runs
    # on the daemon's event loop (observed ~19 s IPC stall). Absorb the full
    # speak cycle here, otherwise the toggle below is queued behind the load
    # and its ack misses the wait window (observed failure: ack=None).
    await wait_wire(
        lambda m: m.get("type") == "tts_state" and m.get("speaking") is True,
        240.0, d1["start"])
    await settle_tts(d1["start"])

    ack_off = await send_expect(
        ws, {"type": "set_stress_adaptation", "enabled": False},
        is_ack_setting("stress_adaptation", enabled=False), 10.0)
    rec("B3 set_stress_adaptation {enabled:false} ack",
        ack_off is not None, f"ack={ack_off}")

    n1 = line_count("stress bias applied")
    d2 = await _voice_turn(ws, UTT_STRESS2, RUSHED_RATE)
    rec("B3 utt2 final transcription matches",
        d2["trans"] is not None
        and has_key(str(d2["trans"].get("text", "")), STRESS2_KEYS),
        f"text={(d2['trans'] or {}).get('text')}")
    s2 = d2["stress"] or {}
    u2 = float(s2.get("urgency", -1.0))
    rec("B3 stress STILL computed+broadcast while disabled (band=rushed)",
        s2.get("band") == "rushed" and 0.0 < u2 <= 1.0,
        f"urgency={u2:.4f} band={s2.get('band')}")
    rec("B3 utt2 assistant_response still arrives (reply unaffected)",
        d2["reply"] is not None,
        f"text={str((d2['reply'] or {}).get('text', ''))[:60]!r}")
    await asyncio.sleep(2.0)
    rec("B3 no NEW 'stress bias applied' while disabled",
        line_count("stress bias applied") == n1,
        f"count={line_count('stress bias applied')} (was {n1})")
    await wait_wire(
        lambda m: m.get("type") == "tts_state" and m.get("speaking") is True,
        120.0, d2["start"])
    await settle_tts(d2["start"])

    ack_on = await send_expect(
        ws, {"type": "set_stress_adaptation", "enabled": True},
        is_ack_setting("stress_adaptation", enabled=True), 10.0)
    rec("B3 set_stress_adaptation {enabled:true} re-enable ack",
        ack_on is not None, f"ack={ack_on}")
    await settle_tts(d2["start"])


# ── B4: camera session + wave ───────────────────────────────────────────────

async def b4_gesture(ws: Any) -> None:
    print("\n--- B4: camera session + wave (gesture -> pill action map) ---")
    idx = len(WIRE)
    ack = await send_expect(
        ws, {"type": "set_gesture_mode", "enabled": True},
        lambda m: (m.get("type") == "ack" and m.get("setting") == "gesture_mode")
        or m.get("type") == "error", 20.0)
    cam_on = await wait_wire(
        lambda m: m.get("type") == "camera_state"
        and m.get("active") is True, 15.0, idx)
    rec("B4 set_gesture_mode {enabled:true} -> ack + camera_state {active:true}",
        ack is not None and ack.get("active") is True and cam_on is not None,
        f"ack={ack} cam={cam_on}")

    idx = len(WIRE)
    gack = await send_expect(
        ws, {"type": "inject_gesture", "gesture": "wave"},
        lambda m: (m.get("type") == "ack"
                   and m.get("setting") == "inject_gesture")
        or m.get("type") == "error", 15.0)
    gev = await wait_wire(
        lambda m: m.get("type") == "gesture_event"
        and m.get("gesture") == "wave", 8.0, idx)
    ovl = await wait_wire(
        lambda m: m.get("type") == "overlay"
        and m.get("action") == "hide", 8.0, idx)
    rec("B4 inject wave -> ack + gesture_event {wave, hide_overlay}",
        gack is not None and gack.get("action") == "hide_overlay"
        and gev is not None and gev.get("action") == "hide_overlay",
        f"ack={gack} evt={gev}")
    rec("B4 wave reuses P1 pill path (overlay broadcast action=hide)",
        ovl is not None, f"evt={ovl}")

    idx = len(WIRE)
    ack2 = await send_expect(
        ws, {"type": "set_gesture_mode", "enabled": False},
        lambda m: (m.get("type") == "ack" and m.get("setting") == "gesture_mode")
        or m.get("type") == "error", 15.0)
    cam_off = await wait_wire(
        lambda m: m.get("type") == "camera_state"
        and m.get("active") is False, 10.0, idx)
    rec("B4 set_gesture_mode {enabled:false} -> camera_state {active:false}",
        ack2 is not None and ack2.get("active") is False and cam_off is not None,
        f"ack={ack2} cam={cam_off}")


# ── B5: streaming chat + barge-in ───────────────────────────────────────────

async def b5_chat(ws: Any) -> None:
    print("\n--- B5: streaming chat (partials -> reply TTS) + barge-in ---")
    start = len(WIRE)
    ptt_down()
    try:
        ack = await send_expect(
            ws, inject_payload(UTT1),
            lambda m: (m.get("type") == "ack"
                       and m.get("setting") == "inject_speech_chunk")
            or m.get("type") == "error", 15.0)
        rec("B5 utt1 inject ack (streaming replay started)",
            ack is not None and ack.get("type") == "ack", f"ack={ack}")
        part = await wait_wire(
            lambda m: m.get("type") == "transcription_partial"
            and str(m.get("text", "")).strip()
            and m.get("final") is False, 30.0, start)
        rec("B5 transcription_partial {final:false} on wire",
            part is not None, f"msg={part}")
        final = await wait_wire(
            lambda m: m.get("type") == "transcription"
            and has_key(str(m.get("text", "")), UTT1_KEYS), 45.0, start)
        rec("B5 final transcription for utt1",
            final is not None,
            f"text={(final or {}).get('text') if final else None!r}")
        resp = await wait_wire(
            lambda m: m.get("type") == "assistant_response"
            and m.get("source") == "voice", 120.0, start)
        rec("B5 utt1 dispatched as a voice turn (assistant_response)",
            resp is not None,
            f"text={str((resp or {}).get('text', ''))[:60] if resp else None!r}")
    finally:
        ptt_up()

    tts1 = await wait_wire(
        lambda m: m.get("type") == "tts_state" and m.get("speaking") is True,
        240.0, 0)  # covers first pocket_tts model load
    rec("B5 tts_state {speaking:true} — reply TTS started",
        tts1 is not None, f"msg={tts1}")

    start = len(WIRE)
    ptt_down()
    try:
        ack2 = await send_expect(
            ws, inject_payload(UTT2),
            lambda m: (m.get("type") == "ack"
                       and m.get("setting") == "inject_speech_chunk")
            or m.get("type") == "error", 15.0)
        rec("B5 utt2 inject ack (utterance #2 replay)", ack2 is not None,
            f"ack={ack2}")
        intr = await wait_wire(
            lambda m: m.get("type") == "tts_state"
            and m.get("speaking") is False and m.get("interrupted") is True,
            30.0, start)
        rec("B5 tts_state {speaking:false, interrupted:true} — playback stopped",
            intr is not None, f"msg={intr}")
        part2 = await wait_wire(
            lambda m: m.get("type") == "transcription_partial"
            and str(m.get("text", "")).strip(), 30.0, start)
        rec("B5 partial(s) observed for utt2", part2 is not None, f"msg={part2}")
        final2 = await wait_wire(
            lambda m: m.get("type") == "transcription"
            and has_key(str(m.get("text", "")), UTT2_KEYS), 45.0, start)
        rec("B5 final transcription for utt2",
            final2 is not None,
            f"text={(final2 or {}).get('text') if final2 else None!r}")
        resp2 = await wait_wire(
            lambda m: m.get("type") == "assistant_response"
            and m.get("source") == "voice", 120.0, start)
        rec("B5 utt2 dispatched as a NEW turn (assistant_response)",
            resp2 is not None,
            f"text={str((resp2 or {}).get('text', ''))[:60] if resp2 else None!r}")
    finally:
        ptt_up()

    barge0 = line_count("barge-in: sustained speech during TTS")
    rec("B5 daemon stdout: barge-in logged (ground truth)",
        await wait_line("barge-in: sustained speech during TTS",
                        max(1, barge0), 15.0),
        next((l.strip()[-70:] for l in LINES if "barge-in" in l), "MISSING"))
    await settle_tts(start, 180.0)


# ── B6: switch to PTT (additive hotkey inside the streaming daemon) ─────────

async def b6_ptt_switch(ws: Any) -> None:
    print("\n--- B6: switch to PTT (negative gate proof + force dispatch) ---")
    rec("B6 PTT hotkey listener active in this daemon (boot log)",
        line_count("PTT started") >= 1, "")

    # (a) no PTT, no wake word -> transcription but NO dispatch.
    dispatch0 = line_count("dispatching without wake word")
    start = len(WIRE)
    ack = await send_expect(
        ws, inject_payload(UTT_NEG),
        lambda m: (m.get("type") == "ack"
                   and m.get("setting") == "inject_speech_chunk")
        or m.get("type") == "error", 15.0)
    rec("B6(a) neg-utterance inject ack", ack is not None, f"ack={ack}")
    trans = await wait_wire(
        lambda m: m.get("type") == "transcription"
        and has_key(str(m.get("text", "")), NEG_KEYS), 45.0, start)
    rec("B6(a) transcription arrives without PTT (mic path live)",
        trans is not None,
        f"text={(trans or {}).get('text') if trans else None!r}")
    resp = await wait_wire(
        lambda m: m.get("type") == "assistant_response"
        and m.get("source") == "voice", 8.0, start)
    rec("B6(a) NO dispatch without PTT or wake word (negative proof)",
        resp is None and line_count("dispatching without wake word") == dispatch0,
        f"resp={resp} dispatch_count={line_count('dispatching without wake word')}"
        f" (was {dispatch0})")

    # (b) PTT press -> force dispatch.
    presses_before = line_count("PTT pressed")
    dispatch1 = line_count("dispatching without wake word")
    start = len(WIRE)
    ptt_down()
    try:
        seen = await wait_line("PTT pressed", presses_before + 1, 6.0)
        rec("B6(b) PTT hotkey observed by daemon (keybd_event ctrl+alt+space)",
            seen, f"count={line_count('PTT pressed')} (was {presses_before})")
        await asyncio.sleep(0.3)
        ack2 = await send_expect(
            ws, inject_payload(UTT_POS),
            lambda m: (m.get("type") == "ack"
                       and m.get("setting") == "inject_speech_chunk")
            or m.get("type") == "error", 15.0)
        rec("B6(b) PTT-held inject ack", ack2 is not None, f"ack={ack2}")
        trans2 = await wait_wire(
            lambda m: m.get("type") == "transcription"
            and has_key(str(m.get("text", "")), POS_KEYS), 45.0, start)
        rec("B6(b) final transcription under PTT",
            trans2 is not None,
            f"text={(trans2 or {}).get('text') if trans2 else None!r}")
        resp2 = await wait_wire(
            lambda m: m.get("type") == "assistant_response"
            and m.get("source") == "voice", 120.0, start)
        rec("B6(b) PTT force-dispatch: assistant_response WITHOUT wake word",
            resp2 is not None,
            f"text={str((resp2 or {}).get('text', ''))[:60] if resp2 else None!r}")
        rec("B6(b) daemon log 'dispatching without wake word' fired",
            line_count("dispatching without wake word") > dispatch1,
            f"count={line_count('dispatching without wake word')}"
            f" (was {dispatch1})")
    finally:
        ptt_up()
    await settle_tts(start, 180.0)


# ── B7: sandbox benign + four containment cases ─────────────────────────────

async def b7_sandbox(ws: Any) -> None:
    print("\n--- B7: sandbox on the SAME daemon (benign + 4 containment) ---")
    hello = await sb_propose(ws, "print('hello-int7')", "benign hello")
    fields = hello is not None and all(
        k in hello for k in ("id", "script", "purpose", "limits", "expires_at"))
    rec("B7 propose -> sandbox_proposal broadcast (all §5 fields)", fields,
        json.dumps(hello) if hello else "timeout")
    if hello is None:
        for nm in ("B7 limits reflect spawn env (timeout 2.5s / memory 64MB)",
                   "B7 propose alone: no run dir, no process (single spawn point)",
                   "B7 confirm benign: exit 0 + stdout captured",
                   "B7 scratch persisted (script.py + stdout.log + output/)",
                   "B7 double-confirm rejected (illegal)",
                   "B7 dismiss-after-executed rejected (illegal)",
                   "B7 containment: wall-clock deadline (reason=deadline)",
                   "B7 containment: memory bomb (reason=memory)",
                   "B7 containment: write-outside denied + file never created",
                   "B7 containment: outbound network blocked",
                   "B7 daemon survived all spawns (ping + alive)",
                   "B7 gate-created run dirs removed"):
            rec(nm, False, "skipped: no proposal")
        return
    pid = str(hello["id"])
    limits = hello.get("limits") or {}
    rec("B7 limits reflect spawn env (timeout 2.5s / memory 64MB)",
        float(limits.get("timeout_s", 0)) == 2.5
        and int(limits.get("memory_mb", 0)) == 64, f"limits={limits}")

    await asyncio.sleep(1.0)
    run_dir = SANDBOX_DIR / pid
    rec("B7 propose alone: no run dir, no process (single spawn point)",
        not run_dir.exists() and not psutil_has(pid), str(run_dir))

    res = await sb_confirm_id(ws, pid, 45.0)
    ok = (res is not None and res.get("type") == "sandbox_result"
          and res.get("status") == "executed" and res.get("exit_code") == 0
          and res.get("timed_out") is False
          and "hello-int7" in str(res.get("stdout", "")))
    rec("B7 confirm benign: exit 0 + stdout captured", ok,
        json.dumps({k: (res or {}).get(k) for k in
                    ("status", "exit_code", "timed_out", "reason", "stdout")}
                   if res else res))
    scratch = str((res or {}).get("scratch", ""))
    rec("B7 scratch persisted (script.py + stdout.log + output/)",
        scratch != "" and (Path(scratch) / "script.py").exists()
        and (Path(scratch) / "stdout.log").exists()
        and (Path(scratch) / "output").is_dir()
        and "hello-int7" in (Path(scratch) / "stdout.log").read_text(
            encoding="utf-8"),
        scratch or "no result")

    again = await send_expect(
        ws, {"type": "sandbox_confirm", "id": pid},
        is_error_detail("illegal"), 10.0)
    rec("B7 double-confirm rejected (illegal)", again is not None,
        f"error={(again or {}).get('detail')}")
    again2 = await send_expect(
        ws, {"type": "sandbox_dismiss", "id": pid},
        is_error_detail("illegal"), 10.0)
    rec("B7 dismiss-after-executed rejected (illegal)", again2 is not None,
        f"error={(again2 or {}).get('detail')}")

    # 1) wall-clock deadline.
    w0 = line_count("watchdog fired")
    p, r = await _propose_confirm(ws, "while True:\n    pass", "infinite loop")
    ok = (r is not None and r.get("type") == "sandbox_result"
          and r.get("status") == "executed" and r.get("timed_out") is True
          and r.get("reason") == "deadline"
          and 1.0 <= float(r.get("duration_s", 0)) <= 10.0)
    rec("B7 containment: wall-clock deadline (reason=deadline)",
        ok, json.dumps({k: (r or {}).get(k) for k in
                        ("status", "timed_out", "reason", "duration_s")}
                       if r else r))
    rec("B7 containment: runner 'watchdog fired' in daemon stdout",
        await wait_line("watchdog fired", w0 + 1, 15.0),
        f"count={line_count('watchdog fired')} (was {w0})")

    # 2) memory bomb.
    p, r = await _propose_confirm(
        ws,
        "b = []\nwhile True:\n    b.append(bytearray(8 * 1024 * 1024))",
        "memory bomb")
    rec("B7 containment: memory bomb (reason=memory)",
        r is not None and r.get("type") == "sandbox_result"
        and r.get("timed_out") is True and r.get("reason") == "memory",
        json.dumps({k: (r or {}).get(k) for k in
                    ("status", "exit_code", "timed_out", "reason",
                     "duration_s")} if r else r)
        + (" stderr=" + str((r or {}).get("stderr", ""))[:200] if r else ""))

    # 3) write outside scratch.
    p, r = await _propose_confirm(
        ws, f"open(r'{ESCAPE_FILE}', 'w').write('escape')", "escape probe")
    stderr = str((r or {}).get("stderr", "")) if r else ""
    rec("B7 containment: write-outside denied (non-zero + PermissionError)",
        r is not None and r.get("exit_code") not in (0, None)
        and ("PermissionError" in stderr or "Access is denied" in stderr),
        f"exit={(r or {}).get('exit_code')} stderr={stderr[:160]!r}")
    rec("B7 containment: escape file never created",
        not ESCAPE_FILE.exists(), str(ESCAPE_FILE))

    # 4) outbound network.
    p, r = await _propose_confirm(
        ws,
        "import socket\nsocket.create_connection(('1.1.1.1', 443), 4)\n"
        "print('NET-OPEN')",
        "net probe", timeout=60.0)
    stderr = str((r or {}).get("stderr", "")) if r else ""
    stdout = str((r or {}).get("stdout", "")) if r else ""
    markers = ("10060", "10013", "timed out", "OSError", "TimeoutError",
               "PermissionError", "blocked", "refused")
    rec("B7 containment: outbound network blocked (NET-OPEN absent)",
        r is not None and r.get("type") == "sandbox_result"
        and "NET-OPEN" not in stdout
        and r.get("exit_code") not in (0, None)
        and any(k in stderr for k in markers),
        f"exit={(r or {}).get('exit_code')} dur={(r or {}).get('duration_s')} "
        f"stderr={stderr[:160]!r}")
    rec("B7 containment: network child did not hang to budget",
        r is not None and float(r.get("duration_s", 99)) < 12.0,
        f"dur={(r or {}).get('duration_s')}")

    alive = DAEMON is not None and DAEMON.poll() is None
    rec("B7 daemon survived all spawns (ping + alive)",
        alive and await ping(ws), f"alive={alive}")

    for used in USED_IDS:
        shutil.rmtree(SANDBOX_DIR / used, ignore_errors=True)
    left = [u for u in USED_IDS if (SANDBOX_DIR / u).exists()]
    rec("B7 gate-created run dirs removed", not left, f"left={left}")


async def _propose_confirm(ws: Any, script: str, purpose: str,
                           timeout: float = 45.0) -> tuple[
                               dict[str, Any] | None, dict[str, Any] | None]:
    p = await sb_propose(ws, script, purpose, 20.0)
    if p is None:
        return None, None
    r = await sb_confirm_id(ws, str(p["id"]), timeout)
    return p, r


# ── T-final: restore + residue ──────────────────────────────────────────────

def t_final() -> None:
    print("\n--- T-final: kill + port free + BACKUP restore + residue ---")
    rec("T-final overlay target window destroyed", destroy_target_window(),
        f"hwnd={TARGET.get('hwnd')}")

    for pid in KILLED_PIDS:
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    strays_crash = process_by_name_prefix("bs_int7_crash_")
    time.sleep(0.5)
    strays_crash += [p for p in process_by_name_prefix("bs_int7_crash_")
                     if p not in strays_crash]
    rec("T-final relaunched/stray crash processes killed",
        not process_by_name_prefix("bs_int7_crash_"),
        f"checked={strays_crash}")

    if DRAIN_TASK is not None:
        DRAIN_TASK.cancel()

    # Tool-launched residue (hallucinated app_launch -> cmd /c start) survives
    # the parent: snapshot the daemon's children before the kill, terminate
    # the survivors afterwards.
    children: list[Any] = []
    if DAEMON is not None:
        try:
            children = psutil.Process(DAEMON.pid).children()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    kill_daemon()
    for child in children:
        try:
            child.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Under full-suite load the CUDA-heavy daemon can take >0.6 s to finish
    # tearing down after terminate() returns (port + process table linger):
    # poll until the port is truly released and no run_daemon.py process
    # remains. 10 s ceiling — past it the recs fail honestly.
    busy = None
    listening: list[Any] = []
    daemons: list[Any] = []
    deadline = time.time() + 10.0
    while True:
        busy = None
        try:
            import socket

            s = socket.socket()
            busy = s.connect_ex(("127.0.0.1", PORT)) == 0
            s.close()
        except Exception:
            pass
        listening = [c.laddr.port for c in psutil.net_connections(kind="tcp")
                     if getattr(c, "laddr", None) and c.status == "LISTEN"
                     and c.laddr.port == PORT]
        daemons = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = p.info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if any("run_daemon.py" in str(a) for a in cl):
                daemons.append(p.info.get("pid"))
        if busy is False and not listening and not daemons:
            break
        if time.time() >= deadline:
            break
        time.sleep(0.3)
    rec("T-final port 8807 free after kill",
        busy is False and not listening, f"connect={busy} listening={listening}")
    rec("T-final no stray daemon processes", not daemons, f"pids={daemons}")

    for key, path in (("conv", CONV), ("tasks", TASKS),
                      ("timeline", TIMELINE_DB), ("rules", RULES_FILE)):
        if BACKUP.get(key) is not None:
            path.write_bytes(BACKUP[key])  # type: ignore[arg-type]
            rec(f"T-final {key} restored byte-identical",
                path.read_bytes() == BACKUP[key], f"{path.stat().st_size}B")
        else:
            path.unlink(missing_ok=True)
            rec(f"T-final {key} removed (absent pre-gate)", not path.exists())
    if not BACKUP.get("rag_existed"):
        (DAEMON_DIR / "data" / "rag_memory.sqlite3").unlink(missing_ok=True)
        (DAEMON_DIR / "data" / "rag_memory.sqlite3.lock").unlink(missing_ok=True)
        rec("T-final rag db+lock removed (absent pre-gate)",
            not (DAEMON_DIR / "data" / "rag_memory.sqlite3").exists())
    else:
        rec("T-final pre-existing rag left for its own lifecycle", True, "")

    left_dirs = [u for u in USED_IDS if (SANDBOX_DIR / u).exists()]
    rec("T-final sandbox run dirs gone", not left_dirs, f"left={left_dirs}")
    rec("T-final escape file never created", not ESCAPE_FILE.exists(),
        str(ESCAPE_FILE))
    SAPI_TMP.unlink(missing_ok=True)
    rec("T-final SAPI temp removed", not SAPI_TMP.exists(), str(SAPI_TMP))

    leaked = [k for k in ("BS_IPC_PORT", "BS_VOICE_MODE", "BS_TTS_ENABLED",
                          "BS_GATE_HOOKS", "BS_SANDBOX_ENABLED",
                          "BS_IPC_AUTH", "BS_DIAGNOSIS_COOLDOWN")
              if k in os.environ]
    rec("T-final env clean (no gate vars in os.environ)", not leaked,
        f"leaked={leaked}")


# ── main ────────────────────────────────────────────────────────────────────

async def main() -> int:
    print("=" * 72)
    print("P7 GATE — one daemon, six features + sandbox in sequence (port 8807)")
    print("=" * 72)

    t0_pre()
    spawn_ok = t1_spawn()
    ws = None
    try:
        if spawn_ok:
            ws = await t2_connect()
            await stable(ws, "S1")
            await b1_pill(ws)
            await stable(ws, "S2")

            scratch = Path(tempfile.mkdtemp(prefix="bs_int7_"))
            SCRATCH_DIRS.append(scratch)
            diag = await b2_diagnosis(scratch)
            if diag is not None:
                await b2_confirm_part(ws, diag)
            await stable(ws, "S3")

            await b3_stress(ws)
            await stable(ws, "S4")
            await b4_gesture(ws)
            await stable(ws, "S5")
            await b5_chat(ws)
            await stable(ws, "S6")
            await b6_ptt_switch(ws)
            await stable(ws, "S7")
            await b7_sandbox(ws)
            await stable(ws, "S8")
        else:
            for name in ("B1 pill", "B2 diagnosis", "B3 stress", "B4 camera",
                         "B5 chat/barge", "B6 PTT switch", "B7 sandbox",
                         "S1-S8 stability"):
                rec(f"{name} (skipped: spawn failed)", False, "")
    except Exception as exc:
        rec("sequence B1-B7 completed without exception", False,
            f"{type(exc).__name__}: {exc}")
    finally:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        t_final()
        for d in SCRATCH_DIRS:
            shutil.rmtree(d, ignore_errors=True)

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
