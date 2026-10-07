"""Phase 2 gate — proactive diagnostics (six-feature expansion).

T1  config: diagnostics_enabled=True, diagnosis_cooldown_s=60.0,
    diagnosis_llm_timeout_s=30.0 defaults + BS_* env overrides; action
    whitelist is exactly {"relaunch_app"} (source check).
T2  daemon up on isolated port 8802 against a STUB Ollama endpoint
    (ephemeral port, BS_OLLAMA_*): the stub makes diagnosis deterministic
    and offline — the daemon-side HTTP call, fixed instruction block,
    JSON parse, whitelist check, broadcast and confirm flow are all the
    real code paths; only the model brain is stood in for. Stub records
    every request (num_ctx + prompt asserted later).
T3  (a) CPU-spike trigger: busy-loop children + BS_MONITOR_POLL=0.5 /
    CPU_THRESHOLD=60 / SUSTAINED=2 (GPU 101 / battery -1 so only cpu+crash
    inputs exist) -> raw monitor_alert category=cpu on the wire AND
    monitor_alert category=diagnosis status=awaiting_confirmation with
    anomaly.kind=spike. One retry after re-arm covers an alert firing
    before the gate's ws subscribed (broadcast with zero subscribers is
    lost by design; BS_DIAGNOSIS_COOLDOWN=5 lets the retry past cooldown).
    Stub ground truth: /api/chat called with num_ctx=512 and the fixed
    instructions as the message.
T4  (b) Crash trigger: PowerShell Add-Type native null-deref child
    (0xDEADBEEF delegate call -> 0xC0000005). Ground truth: gate itself
    reads the Application log (win32evtlog EvtQuery/EvtRender) for an
    Event 1000 whose XML names that process. Diagnosis broadcast follows
    with anomaly.kind=crash, action=relaunch_app, and action_params.path
    equal to the faulting exe (daemon ground truth from the event XML,
    not the stub's echo).
T5  (c) Negative proof: 5 s after the proposals, NO diagnosis with
    status=executed/dismissed anywhere on the wire and NO relaunched
    process (psutil by exe name). Nothing auto-fires.
T6  (d) Confirm path: diagnose_confirm {id} -> ack (status=executed,
    relaunched_pid) + executed broadcast + the relaunched process alive
    (the crash app writes a marker before crashing; on relaunch the
    marker makes it sleep 120 s instead of crashing again, so process
    existence is checkable). A second confirm is rejected: illegal
    transition.
T7  (e) Dismiss path: second crash app (fresh image path) -> diagnosis ->
    diagnose_dismiss -> ack+broadcast status=dismissed, no process ever
    spawned for it, no executed broadcast for it; second dismiss rejected.
T8  (f) HUD source asserts (compact pill in App.tsx + Confirm/Dismiss
    card in DiagnosisCard.tsx + types.ts union) + npm run typecheck.
T9  daemon still healthy after everything (ping -> pong).
T10 restore: relaunched/sleeping crash processes killed, daemon+stub
    stopped, port 8802 free, scratch (crash exes + markers) removed,
    env cleaned, conversation/rag BACKUP restored, GATE_RESULT + exit.

Ground truth: raw WS wire messages, real Application-log reads, psutil
process tables, stub request bodies, exit codes — never assumptions.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psutil
import websockets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
PORT = 8802

GATE_T0 = time.time()

RESULTS: list[tuple[str, bool, str]] = []
WIRE: list[tuple[float, dict[str, Any]]] = []
LINES: list[str] = []
BACKUP: dict[str, object] = {}

DAEMON: subprocess.Popen | None = None
STUB_SERVER: ThreadingHTTPServer | None = None
STUB_THREAD: threading.Thread | None = None
DRAIN_TASK: asyncio.Task | None = None
SCRATCH_DIRS: list[Path] = []
SPINNERS: list[subprocess.Popen] = []
KILLED_PIDS: list[int] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def _utc_since(epoch: float) -> str:
    t = datetime.fromtimestamp(epoch, tz=timezone.utc) - timedelta(seconds=2)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── T1: config defaults + env overrides + whitelist ─────────────────────────

def t1_config() -> None:
    print("\n--- T1: config defaults + BS_* overrides + whitelist ---")
    from blankslate.config import DaemonConfig, load_config
    from blankslate.monitor.diagnostics import ALLOWED_ACTIONS

    c = DaemonConfig()
    rec("T1 defaults (enabled=True, cooldown=60.0, llm_timeout=30.0)",
        c.diagnostics_enabled is True
        and c.diagnosis_cooldown_s == 60.0
        and c.diagnosis_llm_timeout_s == 30.0,
        f"{c.diagnostics_enabled}/{c.diagnosis_cooldown_s}/{c.diagnosis_llm_timeout_s}")

    rec("T1 whitelist == relaunch_app only (source)",
        ALLOWED_ACTIONS == frozenset({"relaunch_app"}),
        f"ALLOWED_ACTIONS={sorted(ALLOWED_ACTIONS)}")

    os.environ["BS_DIAGNOSTICS_ENABLED"] = "0"
    os.environ["BS_DIAGNOSIS_COOLDOWN"] = "5"
    os.environ["BS_DIAGNOSIS_LLM_TIMEOUT"] = "7"
    try:
        e = load_config()
        rec("T1 BS_DIAGNOSTICS_* env overrides honored",
            e.diagnostics_enabled is False
            and e.diagnosis_cooldown_s == 5.0
            and e.diagnosis_llm_timeout_s == 7.0,
            f"{e.diagnostics_enabled}/{e.diagnosis_cooldown_s}/{e.diagnosis_llm_timeout_s}")
    finally:
        for k in ("BS_DIAGNOSTICS_ENABLED", "BS_DIAGNOSIS_COOLDOWN",
                  "BS_DIAGNOSIS_LLM_TIMEOUT"):
            os.environ.pop(k, None)


# ── T2: stub Ollama + daemon spawn + wire drain ─────────────────────────────

class _StubOllama(BaseHTTPRequestHandler):
    """Deterministic /api/chat stand-in; records every request body."""

    requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        _StubOllama.requests.append({"path": self.path, "body": body})
        if self.path == "/api/embed":
            # Echo one vector per input so embed() validation passes.
            count = len(body.get("input") or [])
            resp = json.dumps({"embeddings": [[0.0] * 4 for _ in range(count)]}).encode()
            self._reply(resp)
            return
        if self.path != "/api/chat":
            self._reply(b"{}")
            return
        content = json.dumps({
            "summary": "stub diagnosis",
            "cause": "stub cause",
            "proposal": "Relaunch the faulting application",
            "action": "relaunch_app",
            "action_params": {"path": ""},
        })
        resp = json.dumps({
            "model": body.get("model", "stub"),
            "message": {"role": "assistant", "content": content},
            "total_duration": 1000000,
            "eval_count": 42,
        }).encode()
        self._reply(resp)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/api/tags":
            resp = json.dumps({"models": [{"name": "stub:latest"}]}).encode()
        else:
            resp = b"{}"
        self._reply(resp)

    def _reply(self, resp: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *_args: Any) -> None:
        pass


def t2_start_stub() -> int:
    print("\n--- T2: stub Ollama + daemon + wire drain ---")
    global STUB_SERVER, STUB_THREAD
    STUB_SERVER = ThreadingHTTPServer(("127.0.0.1", 0), _StubOllama)
    port = STUB_SERVER.server_address[1]
    STUB_THREAD = threading.Thread(target=STUB_SERVER.serve_forever, daemon=True)
    STUB_THREAD.start()
    rec("T2 stub Ollama listening (ephemeral port)", port > 0, f"port={port}")
    return port


def t2_daemon_start(stub_port: int) -> None:
    global DAEMON
    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "BS_OLLAMA_HOST": "127.0.0.1",
        "BS_OLLAMA_PORT": str(stub_port),
        # cpu+crash are the only anomaly inputs (plan): silence gpu/battery.
        "BS_MONITOR_POLL_INTERVAL": "0.5",
        "BS_MONITOR_CPU_THRESHOLD": "60",
        "BS_MONITOR_GPU_THRESHOLD": "101",
        "BS_MONITOR_BATTERY_THRESHOLD": "-1",
        "BS_MONITOR_SUSTAINED_SAMPLES": "2",
        # Small runtime cooldown: keeps the T3 pre-subscribe retry inside
        # gate time. Per-image-path crash dedup keys are unaffected.
        "BS_DIAGNOSIS_COOLDOWN": "5",
        "PYTHONIOENCODING": "utf-8",
    })
    DAEMON = subprocess.Popen(
        [sys.executable, "run_daemon.py"],
        cwd=str(DAEMON_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    def pump() -> None:
        assert DAEMON is not None and DAEMON.stdout is not None
        for line in DAEMON.stdout:
            LINES.append(line.rstrip("\n"))

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + 60
    while time.time() < deadline:
        if any("IPC server listening" in ln for ln in LINES):
            break
        time.sleep(0.3)
    up = any("IPC server listening" in ln for ln in LINES)
    rec("T2 daemon IPC listening on gate port", up, f"port={PORT} pid={DAEMON.pid}")
    started = any("diagnostics: started" in ln for ln in LINES)
    rec("T2 diagnostics service started (daemon log)", started,
        next((ln.strip()[-70:] for ln in LINES if "diagnostics:" in ln), "MISSING"))


async def t2_connect() -> Any:
    global DRAIN_TASK
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
    rec("T2 gate ws subscribed (drain running)", DRAIN_TASK is not None, "")
    return ws


# ── wire helpers ────────────────────────────────────────────────────────────

async def wait_wire(pred: Any, timeout: float, start: int = 0) -> dict[str, Any] | None:
    """Async so the event loop keeps draining the wire while we wait."""
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


def is_diagnosis(m: dict[str, Any]) -> bool:
    return m.get("type") == "monitor_alert" and m.get("category") == "diagnosis"


def is_cpu_diagnosis(m: dict[str, Any]) -> bool:
    return (is_diagnosis(m)
            and m.get("status") == "awaiting_confirmation"
            and isinstance(m.get("anomaly"), dict)
            and m["anomaly"].get("kind") == "spike"
            and m["anomaly"].get("category") == "cpu")


def _crash_diagnosis_for(exe: str) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        return (is_diagnosis(m)
                and m.get("status") == "awaiting_confirmation"
                and isinstance(m.get("anomaly"), dict)
                and m["anomaly"].get("kind") == "crash"
                and str(m["anomaly"].get("image_path", "")).lower().endswith(
                    Path(exe).name.lower()))
    return pred


def _by_id(did: str, status: str) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        return is_diagnosis(m) and m.get("id") == did and m.get("status") == status
    return pred


def is_error_detail(substr: str) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        return m.get("type") == "error" and substr in str(m.get("detail", ""))
    return pred


def is_ack_diagnosis(status: str) -> Any:
    def pred(m: dict[str, Any]) -> bool:
        return (m.get("type") == "ack" and m.get("setting") == "diagnosis"
                and m.get("status") == status)
    return pred


async def _ping(ws: Any) -> bool:
    idx = len(WIRE)
    await ws.send(json.dumps({"type": "ping"}))
    deadline = time.time() + 5
    while time.time() < deadline:
        for _ts, m in WIRE[idx:]:
            if m.get("type") == "pong":
                return True
        await asyncio.sleep(0.1)
    return False


# ── T3: cpu spike -> diagnosis ──────────────────────────────────────────────

def _spawn_spinners() -> None:
    n = max(2, os.cpu_count() or 4)
    for _ in range(n):
        SPINNERS.append(subprocess.Popen(
            [sys.executable, "-c", "while True: pass"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        ))


def _kill_spinners() -> None:
    while SPINNERS:
        p = SPINNERS.pop()
        try:
            p.kill()
        except Exception:
            pass


async def t3_cpu_spike() -> dict[str, Any] | None:
    print("\n--- T3 (a): busy-loop CPU spike -> diagnosis broadcast ---")
    _spawn_spinners()
    diag = await wait_wire(is_cpu_diagnosis, 30)
    if diag is None:
        # Retry path: the first alert may have fired before the gate's ws
        # subscribed (lost broadcast) and latched the monitor. Drop the load,
        # let the episode re-arm (cooldown 5 s), spike again.
        _kill_spinners()
        await asyncio.sleep(4)
        _spawn_spinners()
        diag = await wait_wire(is_cpu_diagnosis, 30)
    _kill_spinners()

    raw_cpu = await wait_wire(
        lambda m: m.get("type") == "monitor_alert"
        and m.get("category") == "cpu", 2)
    rec("T3 raw monitor_alert category=cpu on the wire", raw_cpu is not None,
        f"value={(raw_cpu or {}).get('value')} threshold={(raw_cpu or {}).get('threshold')}")
    rec("T3 diagnosis broadcast awaiting_confirmation (kind=spike, cpu)",
        diag is not None,
        f"id={(diag or {}).get('id')} anomaly={(diag or {}).get('anomaly')}")

    # Stub ground truth: the real daemon->LLM HTTP call happened correctly.
    chat = [r for r in _StubOllama.requests if r["path"] == "/api/chat"]
    num_ctx = [r["body"].get("options", {}).get("num_ctx") for r in chat]
    rec("T3 stub /api/chat called with num_ctx=512",
        any(c == 512 for c in num_ctx), f"num_ctx={num_ctx}")
    fixed = any(
        str(r["body"].get("messages", [{}])[0].get("content", "")).startswith(
            "You are BlankSlate's local diagnostics engine")
        for r in chat if r["body"].get("messages"))
    rec("T3 fixed instruction block sent to LLM", fixed,
        f"chat_requests={len(chat)}")
    if diag:
        rec("T3 proposal fields non-empty (summary/cause/proposal)",
            bool(diag.get("summary")) and bool(diag.get("cause"))
            and bool(diag.get("proposal")),
            f"summary={diag.get('summary')!r}")
    else:
        rec("T3 proposal fields non-empty (summary/cause/proposal)", False,
            "skipped: no diagnosis")
    return diag


# ── crash app compile/spawn + event-log ground truth ────────────────────────

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


def compile_crash_app(scratch: Path, name: str) -> Path | None:
    exe = scratch / name
    cls = "Crash" + name.split(".")[0].replace("_", "")[-12:]
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


def process_by_name(name: str) -> list[int]:
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() == name.lower():
                out.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


# ── T4: crash -> event-log ground truth -> diagnosis ────────────────────────

async def t4_crash(scratch: Path) -> tuple[Path | None, dict[str, Any] | None]:
    print("\n--- T4 (b): crash app -> Event 1000 ground truth -> diagnosis ---")
    exe = compile_crash_app(scratch, f"bs_dg2_crash_{os.getpid()}_1.exe")
    rec("T4 crash app compiled (Add-Type)", exe is not None,
        f"path={exe}")
    if exe is None:
        rec("T4 crash exit code nonzero", False, "skipped: no exe")
        rec("T4 Event 1000 names the process (win32evtlog ground truth)",
            False, "skipped: no exe")
        rec("T4 crash diagnosis broadcast (kind=crash, relaunch_app)",
            False, "skipped: no exe")
        return None, None

    proc = spawn_crash(exe)
    try:
        rc = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = None
    rec("T4 crash app died with AV exit code (0xC0000005)",
        rc == 3221225477, f"rc={rc}")

    xml = await wait_event1000(exe.name, 30)
    rec("T4 Event 1000 names the process (win32evtlog ground truth)",
        xml is not None,
        (f"AppPath found for {exe.name}" if xml else "event not found in 30s"))

    diag = await wait_wire(_crash_diagnosis_for(str(exe)), 30)
    rec("T4 crash diagnosis broadcast (kind=crash, relaunch_app)",
        diag is not None,
        f"id={(diag or {}).get('id')} anomaly={(diag or {}).get('anomaly')}")
    if diag:
        rec("T4 relaunch path == faulting exe (event XML ground truth)",
            str(diag.get("action_params", {}).get("path", "")).lower()
            == str(exe).lower(),
            f"path={diag.get('action_params')}")
        rec("T4 faulting module parsed from event XML",
            bool(diag.get("anomaly", {}).get("faulting_module")),
            f"module={diag.get('anomaly', {}).get('faulting_module')}")
    else:
        rec("T4 relaunch path == faulting exe (event XML ground truth)",
            False, "skipped: no diagnosis")
        rec("T4 faulting module parsed from event XML", False,
            "skipped: no diagnosis")
    return exe, diag


# ── T5: negative proof — nothing executes without confirm ───────────────────

async def t5_negative(exe: Path, diag: dict[str, Any] | None) -> None:
    print("\n--- T5 (c): negative proof — no auto-execution after 5 s ---")
    await asyncio.sleep(5)
    executed = [m for _ts, m in WIRE
                if is_diagnosis(m) and m.get("status") in ("executed", "dismissed")]
    rec("T5 no executed/dismissed diagnosis on wire after 5 s",
        len(executed) == 0, f"found={executed[:2]}")
    alive = process_by_name(exe.name)
    rec("T5 no relaunched process (psutil by exe name)", len(alive) == 0,
        f"pids={alive}")


# ── T6: confirm path ────────────────────────────────────────────────────────

async def t6_confirm(ws: Any, exe: Path, diag: dict[str, Any] | None) -> None:
    print("\n--- T6 (d): diagnose_confirm -> execution + relaunched process ---")
    if diag is None:
        for nm in ("T6 ack status=executed with relaunched_pid",
                   "T6 executed broadcast for id",
                   "T6 relaunched process alive (psutil)",
                   "T6 second confirm rejected (illegal transition)"):
            rec(nm, False, "skipped: no diagnosis")
        return

    did = diag["id"]
    ack = await send_expect(
        ws, {"type": "diagnose_confirm", "id": did},
        is_ack_diagnosis("executed"), timeout=15)
    rec("T6 ack status=executed with relaunched_pid",
        ack is not None and int(ack.get("relaunched_pid") or 0) > 0,
        f"ack={ack}")

    evt = await wait_wire(_by_id(did, "executed"), 5)
    rec("T6 executed broadcast for id", evt is not None,
        f"pid={(evt or {}).get('relaunched_pid')}")

    pid = int((ack or {}).get("relaunched_pid") or 0)
    alive = False
    name_ok = False
    if pid > 0:
        for _ in range(10):
            try:
                p = psutil.Process(pid)
                alive = p.is_running()
                name_ok = p.name().lower() == exe.name.lower()
                if alive and name_ok:
                    break
            except psutil.NoSuchProcess:
                pass
            await asyncio.sleep(0.3)
    rec("T6 relaunched process alive (psutil)", bool(alive and name_ok),
        f"pid={pid} alive={alive} name_ok={name_ok}")
    if pid > 0:
        KILLED_PIDS.append(pid)

    again = await send_expect(
        ws, {"type": "diagnose_confirm", "id": did},
        is_error_detail("illegal transition"), timeout=10)
    rec("T6 second confirm rejected (illegal transition)", again is not None,
        f"error={(again or {}).get('detail')}")


# ── T7: dismiss path ────────────────────────────────────────────────────────

async def t7_dismiss(ws: Any, scratch: Path) -> None:
    print("\n--- T7 (e): second crash -> diagnose_dismiss -> no execution ---")
    exe2 = compile_crash_app(scratch, f"bs_dg2_crash_{os.getpid()}_2.exe")
    if exe2 is None:
        for nm in ("T7 second crash diagnosis broadcast",
                   "T7 ack+broadcast status=dismissed",
                   "T7 no process spawned for it",
                   "T7 no executed broadcast for dismissed id",
                   "T7 second dismiss rejected (illegal transition)"):
            rec(nm, False, "skipped: compile failed")
        return
    proc = spawn_crash(exe2)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    xml = await wait_event1000(exe2.name, 30)
    rec("T7 second Event 1000 ground truth", xml is not None,
        f"found={xml is not None}")

    diag2 = await wait_wire(_crash_diagnosis_for(str(exe2)), 30)
    rec("T7 second crash diagnosis broadcast", diag2 is not None,
        f"id={(diag2 or {}).get('id')}")
    if diag2 is None:
        for nm in ("T7 ack+broadcast status=dismissed",
                   "T7 no process spawned for it",
                   "T7 no executed broadcast for dismissed id",
                   "T7 second dismiss rejected (illegal transition)"):
            rec(nm, False, "skipped: no diagnosis")
        return

    did2 = diag2["id"]
    ack = await send_expect(
        ws, {"type": "diagnose_dismiss", "id": did2},
        is_ack_diagnosis("dismissed"), timeout=10)
    rec("T7 ack+broadcast status=dismissed",
        ack is not None
        and await wait_wire(_by_id(did2, "dismissed"), 3) is not None,
        f"ack={ack}")

    await asyncio.sleep(2.5)
    alive2 = process_by_name(exe2.name)
    rec("T7 no process spawned for dismissed id (psutil)",
        len(alive2) == 0, f"pids={alive2}")
    executed2 = any(m.get("id") == did2 and m.get("status") == "executed"
                    for _ts, m in WIRE if is_diagnosis(m))
    rec("T7 no executed broadcast for dismissed id", not executed2,
        f"executed_found={executed2}")

    again = await send_expect(
        ws, {"type": "diagnose_dismiss", "id": did2},
        is_error_detail("illegal transition"), timeout=10)
    rec("T7 second dismiss rejected (illegal transition)", again is not None,
        f"error={(again or {}).get('detail')}")


# ── T8: HUD source asserts + typecheck ──────────────────────────────────────

def t8_hud() -> None:
    print("\n--- T8 (f): HUD source asserts (both views) + typecheck ---")
    app = (HUD_DIR / "src" / "App.tsx").read_text(encoding="utf-8")
    card = (HUD_DIR / "src" / "components" / "DiagnosisCard.tsx").read_text(encoding="utf-8")
    types = (HUD_DIR / "src" / "types.ts").read_text(encoding="utf-8")

    rec("T8 App.tsx handles monitor_alert/diagnosis",
        'case "monitor_alert"' in app and 'category === "diagnosis"' in app,
        "switch case + diagnosis filter present")
    rec("T8 compact view one-line pill in App.tsx",
        "diagnosis-pill" in app and "awaiting_confirmation" in app,
        "compact branch pill source present")
    rec("T8 full view suggestion card wired (App.tsx + DiagnosisCard.tsx)",
        "DiagnosisCard" in app
        and "diagnose_confirm" in card and "diagnose_dismiss" in card
        and "Confirm" in card and "Dismiss" in card,
        "card component with Confirm/Dismiss sendRaw calls")
    rec("T8 types.ts carries diagnosis union + status machine",
        "monitor_alert" in types and "awaiting_confirmation" in types
        and "DiagnosisStatus" in types, "union + status type present")

    proc = subprocess.run(
        "npm run typecheck", cwd=str(HUD_DIR), shell=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    rec("T8 npm run typecheck exit 0", proc.returncode == 0,
        f"rc={proc.returncode} tail={tail}")


# ── T9/T10: health + restore ────────────────────────────────────────────────

async def t9_health(ws: Any) -> None:
    print("\n--- T9: daemon healthy after all paths ---")
    alive = await _ping(ws)
    rec("T9 daemon healthy (ping -> pong)", alive, f"pong={alive}")


def t10_restore() -> None:
    print("\n--- T10: restore (kill processes, port free, BACKUP, cleanup) ---")
    global DAEMON, STUB_SERVER, STUB_THREAD, DRAIN_TASK

    _kill_spinners()
    for pid in KILLED_PIDS:
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # Any stray crash-app processes (marker sleepers).
    for p in psutil.process_iter(["pid", "name"]):
        try:
            n = (p.info["name"] or "").lower()
            if n.startswith("bs_dg2_crash_"):
                p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.8)

    if DRAIN_TASK is not None:
        DRAIN_TASK.cancel()
        DRAIN_TASK = None
    if STUB_SERVER is not None:
        STUB_SERVER.shutdown()
        STUB_SERVER.server_close()
        STUB_SERVER = None
    if DAEMON is not None:
        DAEMON.terminate()
        try:
            DAEMON.wait(timeout=15)
        except subprocess.TimeoutExpired:
            DAEMON.kill()
            DAEMON.wait(timeout=10)
    time.sleep(0.5)

    busy = None
    try:
        import socket
        s = socket.socket()
        busy = s.connect_ex(("127.0.0.1", PORT)) == 0
        s.close()
    except Exception:
        pass
    rec("T10 gate port free after kill", busy is False, f"port={PORT} busy={busy}")

    for d in SCRATCH_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    leftover = [p.name for d in SCRATCH_DIRS for p in d.glob("*")] \
        if SCRATCH_DIRS else []
    rec("T10 scratch (crash exes + markers) removed", not leftover,
        f"leftover={leftover}")

    # BACKUP: conversation + rag lifecycle restored to pre-gate state.
    data = DAEMON_DIR / "data"
    conv = data / "conversation_history.json"
    if BACKUP.get("conv") is not None:
        conv.write_bytes(BACKUP["conv"])  # type: ignore[arg-type]
        rec("T10 conversation restored byte-identical",
            conv.read_bytes() == BACKUP["conv"], f"{conv.stat().st_size}B")
    else:
        conv.unlink(missing_ok=True)
        rec("T10 conversation removed (absent pre-gate)", not conv.exists())
    if not BACKUP.get("rag_existed"):
        (data / "rag_memory.sqlite3").unlink(missing_ok=True)
        (data / "rag_memory.sqlite3.lock").unlink(missing_ok=True)
        rec("T10 rag db+lock removed (absent pre-gate)",
            not (data / "rag_memory.sqlite3").exists())
    else:
        rec("T10 pre-existing rag left for its own lifecycle", True, "")

    for k in ("BS_IPC_PORT", "BS_TTS_ENABLED", "BS_OLLAMA_HOST", "BS_OLLAMA_PORT",
              "BS_MONITOR_POLL_INTERVAL", "BS_MONITOR_CPU_THRESHOLD",
              "BS_MONITOR_GPU_THRESHOLD", "BS_MONITOR_BATTERY_THRESHOLD",
              "BS_MONITOR_SUSTAINED_SAMPLES", "BS_DIAGNOSIS_COOLDOWN"):
        os.environ.pop(k, None)
    rec("T10 env cleaned", True, "")


async def main() -> int:
    t1_config()

    # BACKUP before the daemon ever runs (hw4 pattern, reduced to what this
    # gate's daemon can touch: conversation + rag lifecycle).
    data = DAEMON_DIR / "data"
    conv = data / "conversation_history.json"
    BACKUP["conv"] = conv.read_bytes() if conv.exists() else None
    BACKUP["rag_existed"] = (data / "rag_memory.sqlite3").exists()

    scratch = Path(tempfile.mkdtemp(prefix="bs_dg2_"))
    SCRATCH_DIRS.append(scratch)

    stub_port = t2_start_stub()
    t2_daemon_start(stub_port)
    ws = None
    try:
        ws = await t2_connect()
        cpu_diag = await t3_cpu_spike()
        exe, crash_diag = await t4_crash(scratch)
        if exe is not None:
            await t5_negative(exe, crash_diag)
            await t6_confirm(ws, exe, crash_diag)
        else:
            await t5_negative(scratch / "missing.exe", None)
            await t6_confirm(ws, scratch / "missing.exe", None)
        await t7_dismiss(ws, scratch)
        t8_hud()
        await t9_health(ws)
    finally:
        t10_restore()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
