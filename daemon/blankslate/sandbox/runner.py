"""Sandbox runner — design doc §2 (job limits), §4 (spawn path), §7 (caps).

execute() is blocking (server calls it via asyncio.to_thread). Flow:
cred -> scratch dir -> job object limits -> CreateProcessWithLogonW
(CREATE_SUSPENDED) -> AssignProcessToJobObject -> ResumeThread ->
drain pipes with 64 KB/stream caps + redact_text -> wall-clock watchdog
(TerminateJobObject, reason=deadline) -> exit/reason + persisted logs.

Single spawn point is sandbox_confirm in ipc/server.py; nothing here is
ever called without the registry having settled the proposal.
"""

from __future__ import annotations

import ctypes
import logging
import socket
import time
from ctypes import wintypes
from pathlib import Path

from blankslate.sandbox import cred as sandbox_cred

logger = logging.getLogger("blankslate.sandbox.runner")

STREAM_CAP = 64 * 1024
TRUNC_MARKER = "…[truncated at 64 KB]"

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000080
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_MEM_EXIT_CODES = {0x80000017, 0xC0000017, 0x80000005}

SCRATCH_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "sandbox"


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BASIC_LIMITS(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcesses", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _EXT_LIMITS(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMITS),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _StartupInfo(ctypes.Structure):
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


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def execute(script: str, cfg, run_id: str) -> dict:
    """Run one confirmed script under bs-sandbox + job limits. Blocking."""
    from blankslate.guardrail.redact import redact_text

    try:
        user, password, python = sandbox_cred.load()
    except RuntimeError as exc:
        return {"error": str(exc)}

    timeout_s = float(getattr(cfg, "sandbox_timeout_s", 30.0))
    memory_mb = int(getattr(cfg, "sandbox_memory_mb", 512))

    run_dir = SCRATCH_ROOT / run_id
    out_path = run_dir / "stdout.log"
    err_path = run_dir / "stderr.log"
    try:
        (run_dir / "output").mkdir(parents=True, exist_ok=True)
        script_path = run_dir / "script.py"
        script_path.write_text(script, encoding="utf-8")
    except OSError as exc:
        return {"error": f"scratch not writable: {exc}"}

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    adv.CreateProcessWithLogonW.argtypes = [
        wintypes.LPWSTR, wintypes.LPWSTR, wintypes.LPWSTR, wintypes.DWORD,
        wintypes.LPVOID, wintypes.LPWSTR, wintypes.DWORD, wintypes.LPVOID,
        wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    ]
    adv.CreateProcessWithLogonW.restype = wintypes.BOOL

    sa = _SecurityAttributes()
    sa.nLength = ctypes.sizeof(_SecurityAttributes)
    sa.bInheritHandle = True
    out_rd, out_wr = wintypes.HANDLE(), wintypes.HANDLE()
    err_rd, err_wr = wintypes.HANDLE(), wintypes.HANDLE()
    k32.CreatePipe(ctypes.byref(out_rd), ctypes.byref(out_wr), ctypes.byref(sa), 0)
    k32.CreatePipe(ctypes.byref(err_rd), ctypes.byref(err_wr), ctypes.byref(sa), 0)

    job = k32.CreateJobObjectW(None, None)
    limits = _EXT_LIMITS()
    limits.BasicLimitInformation.LimitFlags = (
        JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_PROCESS_TIME
        | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    )
    limits.BasicLimitInformation.ActiveProcesses = 1
    limits.BasicLimitInformation.PerProcessUserTimeLimit = int(timeout_s * 2 * 1e7)
    limits.ProcessMemoryLimit = memory_mb * 1024 * 1024
    limits.JobMemoryLimit = memory_mb * 1024 * 1024
    if not k32.SetInformationJobObject(
        job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits), ctypes.sizeof(limits),
    ):
        k32.CloseHandle(job)
        k32.CloseHandle(out_rd); k32.CloseHandle(out_wr)
        k32.CloseHandle(err_rd); k32.CloseHandle(err_wr)
        return {"error": f"SetInformationJobObject failed: {ctypes.get_last_error()}"}

    si = _StartupInfo()
    si.cb = ctypes.sizeof(_StartupInfo)
    si.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
    si.hStdOutput = out_wr
    si.hStdError = err_wr
    pi = _ProcessInfo()
    cmdline = ctypes.create_unicode_buffer(f'"{python}" "{script_path}"')
    ok = adv.CreateProcessWithLogonW(
        ctypes.create_unicode_buffer(user),
        ctypes.create_unicode_buffer(socket.gethostname()),
        ctypes.create_unicode_buffer(password),
        0, None, cmdline,
        CREATE_SUSPENDED | CREATE_NO_WINDOW,
        None, str(run_dir), ctypes.byref(si), ctypes.byref(pi),
    )
    k32.CloseHandle(out_wr)
    k32.CloseHandle(err_wr)
    if not ok:
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        k32.CloseHandle(out_rd); k32.CloseHandle(err_rd)
        return {"error": f"spawn failed (CreateProcessWithLogonW: {err})"}

    started = time.monotonic()
    try:
        if not k32.AssignProcessToJobObject(job, pi.hProcess):
            k32.TerminateProcess(pi.hProcess, 1)
            k32.WaitForSingleObject(pi.hProcess, 3000)
            return {"error": f"AssignProcessToJobObject failed: {ctypes.get_last_error()}"}
        k32.ResumeThread(pi.hThread)

        deadline = time.monotonic() + timeout_s
        stdout = bytearray()
        stderr = bytearray()
        totals = [0, 0]
        reason = "exit"
        deadline_fired = False

        def drain(handle, sink: bytearray, total_idx: int) -> int:
            n = wintypes.DWORD()
            if not k32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(n), None):
                return -1  # broken/closed
            if n.value == 0:
                return 0
            chunk = min(n.value, 65536)
            buf = ctypes.create_string_buffer(chunk)
            got = wintypes.DWORD()
            if not k32.ReadFile(handle, buf, chunk, ctypes.byref(got), None):
                return -1
            data = buf.raw[: got.value]
            totals[total_idx] += got.value
            room = STREAM_CAP - len(sink)
            if room > 0:
                sink += data[:room]
            return got.value

        def drain_all() -> None:
            while drain(out_rd, stdout, 0) > 0:
                pass
            while drain(err_rd, stderr, 1) > 0:
                pass

        while True:
            drain_all()
            rc = k32.WaitForSingleObject(pi.hProcess, 25)
            if rc == 0:
                drain_all()
                break
            if time.monotonic() >= deadline:
                deadline_fired = True
                reason = "deadline"
                logger.info(
                    "sandbox[%s]: wall-clock watchdog fired after %.1fs — "
                    "TerminateJobObject",
                    run_id, timeout_s,
                )
                k32.TerminateJobObject(job, 1)
                k32.WaitForSingleObject(pi.hProcess, 5000)
                drain_all()
                break

        exit_code = wintypes.DWORD()
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        code = int(exit_code.value)
        duration_s = time.monotonic() - started

        stdout_txt = redact_text(_capped(stdout, totals[0]))
        stderr_txt = redact_text(_capped(stderr, totals[1]))
        timed_out = deadline_fired
        if not deadline_fired:
            if code in _MEM_EXIT_CODES or "MemoryError" in stderr_txt:
                reason = "memory"
                timed_out = True
            else:
                reason = "exit"
                timed_out = False
    finally:
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        k32.CloseHandle(job)
        k32.CloseHandle(out_rd)
        k32.CloseHandle(err_rd)

    try:
        out_path.write_text(stdout_txt, encoding="utf-8")
        err_path.write_text(stderr_txt, encoding="utf-8")
    except OSError:
        logger.exception("sandbox[%s]: scratch log persist failed", run_id)

    if reason != "exit":
        logger.info(
            "sandbox[%s]: finished reason=%s exit=%s duration=%.2fs",
            run_id, reason, code, duration_s,
        )
    return {
        "status": "executed",
        "exit_code": code,
        "timed_out": timed_out,
        "reason": reason,
        "duration_s": round(duration_s, 3),
        "stdout": stdout_txt,
        "stderr": stderr_txt,
        "scratch": str(run_dir),
    }


def _capped(data: bytearray, total: int) -> str:
    text = bytes(data).decode("utf-8", "replace")
    if total > STREAM_CAP:
        return text + TRUNC_MARKER
    return text
