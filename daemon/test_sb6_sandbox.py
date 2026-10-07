"""Phase 6b gate - sandbox containment (IPC auth, loopback, dedicated account).

Design doc sections 4-5. Runs against the PRODUCTION control-plane port 8765
with token auth enabled (design amendment: the loopback gap is closed at the
APPLICATION layer — a TCP connect is allowed, any command without the
per-session token is not):

T0  port 8765 free (production daemon stopped)
T1  tokenless WebSocket handshake rejected HTTP 401 (general IPC hardening,
    not sandbox-specific — applies to every local process)
T2  wrong-token WebSocket handshake rejected HTTP 401
T3  token file roundtrip (HUD path): handshake accepted, ping/pong works
T4  OS-level gap still open: raw TCP connect succeeds while auth is on
T5  bs-sandbox child process: TCP connect OK, HTTP handshake 401,
    token file unreadable (profile DACL + repo deny-ACE)
T6  source asserts: main.py issues the token + production-only auth,
    bridge.rs presents it on connect

Phase 2 (spawn/job/containment, isolated port 8806):

T7  sandbox config defaults + BS_SANDBOX_* env overrides
T8  propose gated on sandbox_enabled (error while off; broadcast while on)
T9  no-confirm negative: no run dir created, no process spawned (psutil)
T10 confirm benign: exit 0, stdout captured, scratch persisted (script.py,
    stdout.log, output/)
T11 illegal transitions: double-confirm + dismiss-after-confirm -> error
T12 dismiss: sandbox_result status=dismissed, no run dir
T13 proposal TTL -> timed_out broadcast + error reply, nothing spawned
T14 wall-clock deadline: reason=deadline, timed_out, watchdog log line
T15 memory bomb: reason=memory, timed_out
T16 write outside scratch denied (PermissionError), file absent
T17 outbound network to 1.1.1.1:443 blocked (net_isolation)
T18 daemon survival: ping/pong after heavy runs
T19 purge: counts["sandbox"] aged/fresh + exact legacy shape without root
T20 ports 8765/8806 free after gate + gate scratch cleanup

rec(), env cleaned, GATE_RESULT + exit code.
"""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import websockets
from websockets.exceptions import InvalidStatus

from blankslate.config import load_config
from blankslate.ipc.server import IPCState, start_ipc_server

DAEMON_DIR = Path(__file__).resolve().parent
TOKEN_PATH = DAEMON_DIR / "data" / "ipc_token"
SCRATCH_DIR = DAEMON_DIR / "data" / "sandbox"
CHILD_PATH = SCRATCH_DIR / "sb6_child.py"
CRED_PATH = DAEMON_DIR / "data" / "sandbox_user.cred"
META_PATH = DAEMON_DIR / "data" / "sandbox_user.json"
PORT = 8765
PORT2 = 8806
ENTROPY = b"BlankSlate.v2.sandbox.v1"
USED_IDS: list[str] = []

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


# ── T0: port precheck ─────────────────────────────────────────────────────────

def t0_port_free() -> bool:
    print("\n--- T0: port 8765 free (production daemon stopped) ---")
    s = socket.socket()
    s.settimeout(1.5)
    in_use = s.connect_ex(("127.0.0.1", PORT)) == 0
    s.close()
    rec("T0 port 8765 free", not in_use,
        "daemon running? stop it first" if in_use else "refused as expected")
    return not in_use


# ── T1-T4: wire tests against an auth-enforcing server on :8765 ──────────────

async def t1_t4_wire(token: str) -> None:
    print("\n--- T1-T4: auth handshake + OS gap on :8765 ---")

    try:
        await websockets.connect(f"ws://127.0.0.1:{PORT}")
    except InvalidStatus as exc:
        code = exc.response.status_code
        rec("T1 tokenless handshake rejected 401", code == 401, f"status={code}")
    except Exception as exc:
        rec("T1 tokenless handshake rejected 401", False,
            f"{type(exc).__name__}: {exc}")
    else:
        rec("T1 tokenless handshake rejected 401", False, "handshake ACCEPTED")

    wrong = "wrong-" + secrets.token_hex(8)
    try:
        await websockets.connect(f"ws://127.0.0.1:{PORT}?token={wrong}")
    except InvalidStatus as exc:
        code = exc.response.status_code
        rec("T2 wrong-token handshake rejected 401", code == 401, f"status={code}")
    except Exception as exc:
        rec("T2 wrong-token handshake rejected 401", False,
            f"{type(exc).__name__}: {exc}")
    else:
        rec("T2 wrong-token handshake rejected 401", False, "handshake ACCEPTED")

    file_token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    rec("T3 token file roundtrip (HUD path)", file_token == token,
        f"len={len(file_token)}")
    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{PORT}?token={file_token}"
        ) as ws:
            await ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(await asyncio.wait_for(ws.recv(), 5))
            rec("T3 valid token: ping -> pong", pong.get("type") == "pong",
                json.dumps(pong))
    except Exception as exc:
        rec("T3 valid token: ping -> pong", False, f"{type(exc).__name__}: {exc}")

    try:
        s = socket.create_connection(("127.0.0.1", PORT), 3)
        s.close()
        tcp_ok = True
        note = "TCP connect succeeded while auth on (OS gap exists by design)"
    except OSError as exc:
        tcp_ok = False
        note = str(exc)
    rec("T4 raw TCP connect succeeds (OS-level gap open)", tcp_ok, note)


# ── T5: bs-sandbox child (S7 path) against the auth-enforcing server ─────────

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]


def _dpapi_unprotect(blob: bytes) -> str | None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.LocalFree.restype = ctypes.c_void_p
    cry = ctypes.WinDLL("crypt32", use_last_error=True)
    in_blob = _DataBlob(
        len(blob), ctypes.cast(ctypes.c_char_p(blob), ctypes.c_void_p)
    )
    ent = _DataBlob(
        len(ENTROPY), ctypes.cast(ctypes.c_char_p(ENTROPY), ctypes.c_void_p)
    )
    out_blob = _DataBlob()
    if not cry.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(ent), None, None, 0,
        ctypes.byref(out_blob),
    ):
        return None
    raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    k32.LocalFree(out_blob.pbData)
    return raw.decode("utf-8")


_CHILD_SRC = r'''
import json, socket

out = {}
try:
    s = socket.create_connection(("127.0.0.1", 8765), 3)
    out["tcp_ok"] = True
    s.close()
except Exception as e:
    out["tcp_ok"] = False
    out["tcp_err"] = str(e)
try:
    s = socket.create_connection(("127.0.0.1", 8765), 3)
    req = (
        "GET / HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode("ascii"))
    out["http_status"] = s.recv(4096).split(b"\r\n", 1)[0].decode("ascii", "replace")
    s.close()
except Exception as e:
    out["http_status"] = "ERR " + str(e)
try:
    open(r"%TOKEN_PATH%", encoding="utf-8").read()
    out["token_read"] = "readable"
except Exception as e:
    out["token_read"] = "denied"
    out["token_read_err"] = str(e)
print(json.dumps(out))
'''


def _spawn_bs_sandbox(child: Path) -> dict:
    from ctypes import wintypes

    meta = json.loads(META_PATH.read_text(encoding="utf-8-sig"))
    pw = _dpapi_unprotect(CRED_PATH.read_bytes())
    if pw is None:
        return {"error": "dpapi unprotect failed"}

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CreateProcessWithLogonW.argtypes = [
        wintypes.LPWSTR, wintypes.LPWSTR, wintypes.LPWSTR, wintypes.DWORD,
        wintypes.LPVOID, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    ]
    adv.CreateProcessWithLogonW.restype = wintypes.BOOL

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.c_ulong),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    sa = SecurityAttributes()
    sa.nLength = ctypes.sizeof(SecurityAttributes)
    sa.bInheritHandle = True
    rd, wr = wintypes.HANDLE(), wintypes.HANDLE()
    k32.CreatePipe(ctypes.byref(rd), ctypes.byref(wr), ctypes.byref(sa), 0)

    class StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    si = StartupInfo()
    si.cb = ctypes.sizeof(StartupInfo)
    si.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
    si.hStdOutput = wr
    si.hStdError = wr
    pi = ProcessInformation()
    cmdline = ctypes.create_unicode_buffer(f'"{sys.executable}" "{child}"')
    ok = adv.CreateProcessWithLogonW(
        ctypes.create_unicode_buffer(meta.get("user", "bs-sandbox")),
        ctypes.create_unicode_buffer(socket.gethostname()),
        ctypes.create_unicode_buffer(pw),
        0, None, cmdline, 0x08000000, None, str(SCRATCH_DIR),
        ctypes.byref(si), ctypes.byref(pi),
    )
    k32.CloseHandle(wr)
    if not ok:
        k32.CloseHandle(rd)
        return {"error": f"CreateProcessWithLogonW failed: {ctypes.get_last_error()}"}
    k32.WaitForSingleObject(pi.hProcess, 20000)
    data = b""
    while True:
        chunk = ctypes.create_string_buffer(4096)
        n = wintypes.DWORD()
        if not k32.ReadFile(rd, chunk, 4096, ctypes.byref(n), None) or n.value == 0:
            break
        data += chunk.raw[: n.value]
    k32.CloseHandle(rd)
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)
    lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    for line in reversed(lines):
        if line.strip().startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": "no child output", "raw": lines[-3:]}


def t5_bs_sandbox_child() -> None:
    print("\n--- T5: bs-sandbox child vs auth-enforcing server ---")
    if not CRED_PATH.exists() or not META_PATH.exists():
        rec("T5 bs-sandbox containment", False,
            "SETUP NOT RUN (scripts/sandbox_user_setup.ps1 -Mode enable)")
        return
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    CHILD_PATH.write_text(
        _CHILD_SRC.replace("%TOKEN_PATH%", str(TOKEN_PATH)), encoding="utf-8"
    )
    try:
        out = _spawn_bs_sandbox(CHILD_PATH)
    finally:
        try:
            CHILD_PATH.unlink()
        except OSError:
            pass
    if "error" in out:
        rec("T5 bs-sandbox containment", False, json.dumps(out))
        return
    status = str(out.get("http_status", ""))
    rec("T5 bs-sandbox: raw TCP connect to :8765", out.get("tcp_ok") is True,
        json.dumps(out))
    rec("T5 bs-sandbox: WS handshake -> 401 (no session, no command)",
        "401" in status, f"http_status={status!r}")
    rec("T5 bs-sandbox: token file unreadable", out.get("token_read") == "denied",
        f"{out.get('token_read')} ({out.get('token_read_err', '')})")


# ── T6: source asserts (production wiring) ────────────────────────────────────

def t6_source() -> None:
    print("\n--- T6: production wiring (source) ---")
    main_src = (DAEMON_DIR / "blankslate" / "main.py").read_text(encoding="utf-8")
    rec("T6 main.py issues per-session token at startup",
        "_issue_ipc_token()" in main_src and "secrets.token_urlsafe" in main_src, "")
    rec("T6 main.py auth production-only (frozen / BS_IPC_AUTH=1)",
        '_ipc_auth_enabled' in main_src and 'sys' in main_src
        and 'BS_IPC_AUTH' in main_src and 'frozen' in main_src, "")
    srv_src = (DAEMON_DIR / "blankslate" / "ipc" / "server.py").read_text(
        encoding="utf-8"
    )
    rec("T6 server.py enforces token before any message (401 guard)",
        "_token_guard" in srv_src and "compare_digest" in srv_src
        and "process_request" in srv_src, "")
    bridge = (
        DAEMON_DIR.parent / "hud" / "src-tauri" / "src" / "bridge.rs"
    ).read_text(encoding="utf-8")
    rec("T6 bridge.rs presents ?token= on every connect",
        "hud_token()" in bridge and "?token=" in bridge, "")


# ── T7: config defaults + env overrides ──────────────────────────────────────

def t7_config() -> None:
    print("\n--- T7: sandbox config defaults + env overrides ---")
    d = load_config()
    rec("T7 defaults disabled/30s/512MB/14d/120s",
        d.sandbox_enabled is False and d.sandbox_timeout_s == 30.0
        and d.sandbox_memory_mb == 512 and d.sandbox_ttl_days == 14.0
        and d.sandbox_proposal_ttl_s == 120.0,
        f"enabled={d.sandbox_enabled} timeout={d.sandbox_timeout_s} "
        f"mem={d.sandbox_memory_mb} ttl={d.sandbox_ttl_days} "
        f"prop={d.sandbox_proposal_ttl_s}")
    env = {
        "BS_SANDBOX_ENABLED": "1",
        "BS_SANDBOX_TIMEOUT": "2.5",
        "BS_SANDBOX_MEMORY_MB": "64",
        "BS_SANDBOX_TTL_DAYS": "1.5",
        "BS_SANDBOX_PROPOSAL_TTL": "9",
    }
    os.environ.update(env)
    try:
        e = load_config()
    finally:
        for k in env:
            os.environ.pop(k, None)
    rec("T7 env overrides applied",
        e.sandbox_enabled is True and e.sandbox_timeout_s == 2.5
        and e.sandbox_memory_mb == 64 and e.sandbox_ttl_days == 1.5
        and e.sandbox_proposal_ttl_s == 9.0,
        f"enabled={e.sandbox_enabled} timeout={e.sandbox_timeout_s} "
        f"mem={e.sandbox_memory_mb} ttl={e.sandbox_ttl_days} "
        f"prop={e.sandbox_proposal_ttl_s}")


# ── T8-T18: wire + spawn tests on :8806 ──────────────────────────────────────

async def _expect(ws, want: str, timeout: float = 15.0) -> dict | None:
    """Drain messages until `want` arrives (unrelated messages skipped)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            m = json.loads(await asyncio.wait_for(
                ws.recv(), max(0.5, end - time.monotonic())
            ))
        except asyncio.TimeoutError:
            return None
        if m.get("type") == want:
            return m
    return None


async def _drain(ws, window: float = 0.25) -> None:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), window)
    except asyncio.TimeoutError:
        pass


async def _send_expect(ws, msg: dict, want: str, timeout: float = 15.0) -> dict | None:
    await _drain(ws)
    await ws.send(json.dumps(msg))
    return await _expect(ws, want, timeout)


async def _propose(ws, script: str, purpose: str, timeout: float = 15.0) -> dict | None:
    m = await _send_expect(
        ws, {"type": "sandbox_propose", "script": script, "purpose": purpose},
        "sandbox_proposal", timeout,
    )
    if m is not None:
        USED_IDS.append(m["id"])
    return m


def _psutil_has(marker: str) -> bool:
    import psutil

    for p in psutil.process_iter(["cmdline"]):
        try:
            cl = p.info["cmdline"] or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if any(marker in part for part in cl):
            return True
    return False


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


async def t8_t18_wire(ws, state, cfg) -> None:
    def setcfg(**kw) -> None:
        state.cfg = dataclasses.replace(state.cfg, **kw)

    print("\n--- T8: propose gated on sandbox_enabled ---")
    setcfg(sandbox_enabled=False)
    try:
        m = await _send_expect(
            ws, {"type": "sandbox_propose", "script": "print(1)",
                 "purpose": "disabled probe"},
            "error",
        )
        rec("T8 propose rejected while sandbox_enabled=False",
            m is not None and "disabled" in str(m.get("detail", "")),
            json.dumps(m))
    finally:
        state.cfg = cfg

    hello = await _propose(ws, "print('hello-sb6')", "benign hello")
    fields = hello is not None and all(
        k in hello for k in ("id", "script", "purpose", "limits", "expires_at")
    )
    rec("T8 propose -> sandbox_proposal broadcast (all §5 fields)", fields,
        json.dumps(hello) if hello else "timeout")
    if hello is None:
        rec("T9-T18 aborted", False, "no proposal to drive the rest")
        return
    pid_a = hello["id"]

    print("\n--- T9: no-confirm negative ---")
    await asyncio.sleep(1.0)
    run_dir_a = SCRATCH_DIR / pid_a
    rec("T9 propose alone: no run dir", not run_dir_a.exists(), str(run_dir_a))
    rec("T9 propose alone: no process spawned (psutil)",
        not _psutil_has(pid_a), f"marker={pid_a}")

    print("\n--- T10: confirm benign (single spawn point) ---")
    res = await _send_expect(
        ws, {"type": "sandbox_confirm", "id": pid_a}, "sandbox_result", 30.0,
    )
    ok = (res is not None and res.get("status") == "executed"
          and res.get("exit_code") == 0 and res.get("timed_out") is False
          and "hello-sb6" in str(res.get("stdout", "")))
    rec("T10 confirm: exit 0, timed_out False, stdout captured", ok,
        json.dumps({k: res.get(k) for k in ("status", "exit_code", "timed_out",
                                            "reason", "stdout")} if res else res))
    if res is not None and res.get("scratch"):
        sd = Path(res["scratch"])
        rec("T10 scratch persisted (script.py + stdout.log + output/)",
            (sd / "script.py").exists() and (sd / "stdout.log").exists()
            and (sd / "output").is_dir()
            and "hello-sb6" in (sd / "stdout.log").read_text(encoding="utf-8"),
            str(sd))
    else:
        rec("T10 scratch persisted (script.py + stdout.log + output/)", False,
            "no result")

    print("\n--- T11: illegal transitions ---")
    m = await _send_expect(
        ws, {"type": "sandbox_confirm", "id": pid_a}, "error",
    )
    rec("T11 double-confirm rejected (illegal)",
        m is not None and "illegal" in str(m.get("detail", "")), json.dumps(m))
    m = await _send_expect(
        ws, {"type": "sandbox_dismiss", "id": pid_a}, "error",
    )
    rec("T11 dismiss-after-executed rejected (illegal)",
        m is not None and "illegal" in str(m.get("detail", "")), json.dumps(m))

    print("\n--- T12: dismiss flow ---")
    p2 = await _propose(ws, "print('never')", "dismiss me")
    rec("T12 proposal received", p2 is not None, json.dumps(p2))
    if p2 is not None:
        m = await _send_expect(
            ws, {"type": "sandbox_dismiss", "id": p2["id"]}, "sandbox_result",
        )
        rec("T12 dismiss -> sandbox_result status=dismissed",
            m is not None and m.get("status") == "dismissed", json.dumps(m))
        rec("T12 dismiss spawns nothing (no run dir)",
            not (SCRATCH_DIR / p2["id"]).exists(), p2["id"])

    print("\n--- T13: proposal TTL -> timed_out ---")
    setcfg(sandbox_proposal_ttl_s=0.5)
    p3 = await _propose(ws, "print('too late')", "ttl probe")
    state.cfg = cfg
    rec("T13 short-ttl proposal received", p3 is not None,
        json.dumps(p3))
    if p3 is not None:
        await asyncio.sleep(0.8)
        m = await _send_expect(
            ws, {"type": "sandbox_confirm", "id": p3["id"]}, "sandbox_result",
        )
        rec("T13 confirm after TTL -> timed_out broadcast",
            m is not None and m.get("status") == "timed_out", json.dumps(m))
        m2 = await _expect(ws, "error")
        rec("T13 error reply (nothing spawned)",
            m2 is not None and "timed out" in str(m2.get("detail", "")),
            json.dumps(m2))
        rec("T13 no run dir after TTL", not (SCRATCH_DIR / p3["id"]).exists(),
            p3["id"])

    print("\n--- T14: wall-clock deadline ---")
    lg = logging.getLogger("blankslate.sandbox.runner")
    cap = _LogCapture()
    saved_level = lg.level
    lg.addHandler(cap)
    lg.setLevel(logging.INFO)
    setcfg(sandbox_timeout_s=2.0)
    try:
        p4 = await _propose(ws, "while True:\n    pass", "infinite loop", 30.0)
        rec("T14 loop proposal received", p4 is not None, json.dumps(p4))
        if p4 is not None:
            m = await _send_expect(
                ws, {"type": "sandbox_confirm", "id": p4["id"]},
                "sandbox_result", 30.0,
            )
            ok = (m is not None and m.get("status") == "executed"
                  and m.get("timed_out") is True
                  and m.get("reason") == "deadline"
                  and 1.0 <= float(m.get("duration_s", 0)) <= 10.0)
            rec("T14 watchdog: reason=deadline, timed_out, ~2s duration", ok,
                json.dumps(m))
            rec("T14 distinct runner log line captured",
                any("watchdog fired" in ln for ln in cap.lines),
                json.dumps(cap.lines[-3:]))
    finally:
        state.cfg = cfg
        lg.removeHandler(cap)
        lg.setLevel(saved_level)

    print("\n--- T15: memory bomb (job memory limit) ---")
    setcfg(sandbox_memory_mb=64)
    try:
        p5 = await _propose(
            ws,
            "b = []\nwhile True:\n    b.append(bytearray(8 * 1024 * 1024))",
            "memory bomb", 30.0,
        )
        rec("T15 bomb proposal received", p5 is not None, json.dumps(p5))
        if p5 is not None:
            m = await _send_expect(
                ws, {"type": "sandbox_confirm", "id": p5["id"]},
                "sandbox_result", 30.0,
            )
            ok = (m is not None and m.get("timed_out") is True
                  and m.get("reason") == "memory")
            rec("T15 job memory limit: reason=memory, timed_out", ok,
                json.dumps({k: m.get(k) for k in
                            ("status", "exit_code", "timed_out", "reason",
                             "duration_s")} if m else m)
                + (" stderr=" + str(m.get("stderr", ""))[:200] if m else ""))
    finally:
        state.cfg = cfg

    print("\n--- T16: write outside scratch denied ---")
    esc = DAEMON_DIR / "data" / "escape_sb6.txt"
    p6 = await _propose(
        ws, f"open(r'{esc}', 'w').write('escape')", "escape probe", 30.0,
    )
    rec("T16 escape proposal received", p6 is not None, json.dumps(p6))
    if p6 is not None:
        m = await _send_expect(
            ws, {"type": "sandbox_confirm", "id": p6["id"]},
            "sandbox_result", 30.0,
        )
        stderr = str(m.get("stderr", "")) if m else ""
        rec("T16 non-zero exit + PermissionError in stderr",
            m is not None and m.get("exit_code") not in (0, None)
            and ("PermissionError" in stderr or "Access is denied" in stderr),
            f"exit={m.get('exit_code') if m else None} stderr={stderr[:160]!r}")
        rec("T16 escape file never created", not esc.exists(), str(esc))

    print("\n--- T17: outbound network blocked (net_isolation) ---")
    p7 = await _propose(
        ws,
        "import socket\nsocket.create_connection(('1.1.1.1', 443), 4)\n"
        "print('NET-OPEN')",
        "net probe", 30.0,
    )
    rec("T17 net proposal received", p7 is not None, json.dumps(p7))
    if p7 is not None:
        m = await _send_expect(
            ws, {"type": "sandbox_confirm", "id": p7["id"]},
            "sandbox_result", 45.0,
        )
        stderr = str(m.get("stderr", "")) if m else ""
        stdout = str(m.get("stdout", "")) if m else ""
        markers = ("10060", "10013", "timed out", "OSError", "TimeoutError",
                   "PermissionError", "blocked", "refused")
        rec("T17 connect blocked: NET-OPEN absent, non-zero exit, "
            "firewall-family error",
            m is not None and "NET-OPEN" not in stdout
            and m.get("exit_code") not in (0, None)
            and any(k in stderr for k in markers),
            f"exit={m.get('exit_code') if m else None} "
            f"dur={m.get('duration_s') if m else None} "
            f"stderr={stderr[:160]!r}")
        rec("T17 child did not hang to full 30s budget",
            m is not None and float(m.get("duration_s", 99)) < 12.0,
            f"dur={m.get('duration_s') if m else None}")

    print("\n--- T18: daemon survival after heavy runs ---")
    await _drain(ws)
    m = await _send_expect(ws, {"type": "ping"}, "pong")
    rec("T18 ping -> pong after all spawn tests", m is not None,
        json.dumps(m))


# ── T19: purge sandbox TTL (injected dirs) ───────────────────────────────────

def t19_purge() -> None:
    print("\n--- T19: purge counts[\"sandbox\"] aged/fresh + legacy shape ---")
    import tempfile

    from blankslate.rag import service as rag_service
    from blankslate.vault.purge import purge_once

    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="sb6_purge_"))
    nd = tmp / "notes"
    nd.mkdir()
    try:
        dd_a = tmp / "a"
        dd_a.mkdir()
        rag_service.configure(tmp / "a_rag.sqlite3")
        rag_service.ingest_count()  # force store creation
        counts_a = purge_once(cfg, None, data_dir=dd_a, notes_dir=nd)
        rec("T19 no sandbox/ dir -> exact legacy counts shape",
            set(counts_a) == {"conversation", "notes", "captures", "rag"},
            f"keys={sorted(counts_a)} counts={counts_a}")

        dd_b = tmp / "b"
        (dd_b / "sandbox").mkdir(parents=True)
        aged = dd_b / "sandbox" / "sbx-aged"
        fresh = dd_b / "sandbox" / "sbx-fresh"
        aged.mkdir()
        fresh.mkdir()
        os.utime(aged, (time.time() - 30 * 86400,) * 2)
        rag_service.configure(tmp / "b_rag.sqlite3")
        rag_service.ingest_count()
        counts_b = purge_once(cfg, None, data_dir=dd_b, notes_dir=nd)
        rec("T19 aged run dir purged, fresh kept (counts[sandbox]=1)",
            counts_b.get("sandbox") == 1 and not aged.exists()
            and fresh.exists(), f"counts={counts_b}")
        rag_service.close_store()

        src = (DAEMON_DIR / "blankslate" / "vault" / "purge.py").read_text(
            encoding="utf-8"
        )
        rec("T19 purge.py conditional counts[\"sandbox\"] + TTL sweep",
            'counts["sandbox"] = removed' in src and "shutil.rmtree" in src
            and "sandbox_ttl_days" in src, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── T20: ports clean + scratch cleanup ───────────────────────────────────────

def t20_cleanup() -> None:
    print("\n--- T20: ports free after gate + gate scratch cleanup ---")
    for port, label in ((PORT, "8765"), (PORT2, "8806")):
        s = socket.socket()
        s.settimeout(1.5)
        in_use = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        rec(f"T20 port {label} free after gate", not in_use,
            "still bound" if in_use else "refused as expected")

    removed = 0
    for pid in USED_IDS:
        d = SCRATCH_DIR / pid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            removed += 1
    rec("T20 gate scratch dirs cleaned",
        removed == len(USED_IDS), f"{removed}/{len(USED_IDS)}")
    esc = DAEMON_DIR / "data" / "escape_sb6.txt"
    if esc.exists():
        esc.unlink()
    rec("T20 no escape file left behind", not esc.exists(), str(esc))


# ── phase 2 driver: auth-enforcing server on :8806 ───────────────────────────

async def phase2() -> None:
    print("\n=== Phase 2: spawn/job/containment on :8806 ===")
    t7_config()

    s = socket.socket()
    s.settimeout(1.5)
    in_use = s.connect_ex(("127.0.0.1", PORT2)) == 0
    s.close()
    rec("T0b port 8806 free", not in_use,
        "port busy — clean up before the gate" if in_use else "refused")
    if in_use:
        return

    env = {"BS_IPC_PORT": str(PORT2), "BS_SANDBOX_ENABLED": "1"}
    os.environ.update(env)
    try:
        cfg2 = load_config()
    finally:
        for k in env:
            os.environ.pop(k, None)
    rec("T0b cfg2 targets :8806 + sandbox_enabled",
        cfg2.ipc_port == PORT2 and cfg2.sandbox_enabled,
        f"port={cfg2.ipc_port} enabled={cfg2.sandbox_enabled}")

    state2 = IPCState(ctx=SimpleNamespace(turn_count=0), llm=None, cfg=cfg2)
    token2 = secrets.token_urlsafe(32)
    server = asyncio.create_task(start_ipc_server(cfg2, state2, token=token2))
    await asyncio.sleep(0.5)
    if server.done():
        server.cancel()
        rec("T0b sandbox server started on :8806", False,
            f"exited: {server.exception()}")
        return
    rec("T0b sandbox server started on :8806", True, "token auth ON")

    try:
        async with websockets.connect(
            f"ws://127.0.0.1:{PORT2}?token={token2}"
        ) as ws:
            await t8_t18_wire(ws, state2, cfg2)
    except Exception as exc:
        rec("T8-T18 wire phase completed", False,
            f"{type(exc).__name__}: {exc}")
    finally:
        server.cancel()
        try:
            await server
        except (asyncio.CancelledError, Exception):
            pass

    t19_purge()
    t20_cleanup()


def t6b_source() -> None:
    print("\n--- T6b: Phase 6b production wiring (source) ---")
    srv_src = (DAEMON_DIR / "blankslate" / "ipc" / "server.py").read_text(
        encoding="utf-8"
    )
    rec("T6b server: exactly ONE sandbox_execute use (import + single call)",
        srv_src.count("sandbox_execute") == 2,
        f"count={srv_src.count('sandbox_execute')}")
    confirm_at = srv_src.find('msg_type in ("sandbox_confirm", "sandbox_dismiss")')
    call_at = srv_src.find("sandbox_execute, prop")
    rec("T6b spawn call sits inside the sandbox_confirm branch",
        confirm_at > 0 and call_at > confirm_at,
        f"confirm@{confirm_at} call@{call_at}")
    reg_src = (DAEMON_DIR / "blankslate" / "sandbox" / "registry.py").read_text(
        encoding="utf-8"
    )
    rec("T6b registry: illegal + timed_out transitions",
        '"illegal"' in reg_src and '"timed_out"' in reg_src
        and "expires_at" in reg_src, "")
    run_src = (DAEMON_DIR / "blankslate" / "sandbox" / "runner.py").read_text(
        encoding="utf-8"
    )
    rec("T6b runner: logon + suspended spawn + job assign + kill + caps",
        all(k in run_src for k in (
            "CreateProcessWithLogonW", "CREATE_SUSPENDED",
            "AssignProcessToJobObject", "TerminateJobObject",
            "redact_text", "TRUNC_MARKER",
        )), "")
    purge_src = (DAEMON_DIR / "blankslate" / "vault" / "purge.py").read_text(
        encoding="utf-8"
    )
    rec("T6b purge: conditional sandbox key + TTL + PURGE_INTERVAL_S intact",
        'counts["sandbox"] = removed' in purge_src
        and "shutil.rmtree" in purge_src
        and "PURGE_INTERVAL_S = 900.0" in purge_src, "")
    hud_app = (DAEMON_DIR.parent / "hud" / "src" / "App.tsx").read_text(
        encoding="utf-8"
    )
    hud_card = (
        DAEMON_DIR.parent / "hud" / "src" / "components" / "SandboxCard.tsx"
    ).read_text(encoding="utf-8")
    rec("T6b HUD: proposal/result handling + Confirm/Dismiss card",
        'case "sandbox_proposal"' in hud_app
        and 'case "sandbox_result"' in hud_app
        and "sandbox_confirm" in hud_card
        and "sandbox_dismiss" in hud_card, "")


# ── runner ────────────────────────────────────────────────────────────────────

async def run_gate() -> None:
    if not t0_port_free():
        return

    token = secrets.token_urlsafe(32)
    original_token = None
    if TOKEN_PATH.exists():
        original_token = TOKEN_PATH.read_text(encoding="utf-8")
    TOKEN_PATH.write_text(token, encoding="utf-8")

    os.environ["BS_IPC_PORT"] = str(PORT)
    try:
        cfg = load_config()
    finally:
        os.environ.pop("BS_IPC_PORT", None)
    rec("T1 cfg targets :8765", cfg.ipc_port == PORT, f"port={cfg.ipc_port}")

    state = IPCState(ctx=SimpleNamespace(turn_count=0), llm=None)
    server = asyncio.create_task(start_ipc_server(cfg, state, token=token))
    await asyncio.sleep(0.5)
    if server.done():
        server.cancel()
        rec("T1 auth server started on :8765", False,
            f"exited: {server.exception()}")
        return
    rec("T1 auth server started on :8765", True, "token auth ON")

    try:
        await t1_t4_wire(token)
        await asyncio.to_thread(t5_bs_sandbox_child)
        t6_source()
    finally:
        server.cancel()
        try:
            await server
        except (asyncio.CancelledError, Exception):
            pass
        try:
            if original_token is None:
                TOKEN_PATH.unlink()
            else:
                TOKEN_PATH.write_text(original_token, encoding="utf-8")
        except OSError:
            pass

    t6b_source()
    await phase2()


def main() -> None:
    asyncio.run(run_gate())
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    sys.exit(0 if total > 0 and passed == total else 1)


if __name__ == "__main__":
    main()
