"""PHASE 5 GATE — network isolation verification.

T1  isolation rules present, exactly one per target, enabled, Outbound BLOCK,
    covering THIS python + the frozen daemon exe (proves stale sweep + create)
T2  outbound probe: TCP connect 1.1.1.1:443 from this python must be DROPPED,
    recorded in daemon/logs/isolation.log
T3  loopback intact under the block rule: Ollama /api/tags answers on 127.0.0.1
T4  live daemon netstat audit: spawned-or-existing daemon holds no non-loopback
    connection and no public listener; IPC loopback reachable
T5  dropped-connection logging: system firewall log shows the DROP for our
    probe (fallback: own isolation.log record, clearly noted)

Prereq: run elevated first -
  powershell -ExecutionPolicy Bypass -File scripts\\net_isolation.ps1 -Mode enable

Ground truth: firewall rule objects (Get-NetFirewallRule), raw socket probe
results, netstat rows for concrete PIDs, firewall log lines — never assumptions.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

DAEMON_DIR = Path(__file__).resolve().parent
LOG_DIR = DAEMON_DIR / "logs"
ISO_LOG = LOG_DIR / "isolation.log"
FW_LOG = Path(os.environ.get("windir", r"C:\Windows")) / "System32" / "LogFiles" / "Firewall" / "pfirewall.log"
RULE_PREFIX = "BlankSlate-Isolation"
FROZEN_EXE = DAEMON_DIR / "dist" / "blankslate-daemon" / "blankslate-daemon.exe"
IPC_HOST, IPC_PORT = "127.0.0.1", 8765
OLLAMA_HOST, OLLAMA_PORT = "127.0.0.1", 11434
PROBE_HOST, PROBE_PORT = "1.1.1.1", 443
PROBE_TIMEOUT = 4.0

results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'OK' if ok else 'FAIL'}   {name}" + (f" — {note}" if note else ""))


def iso_log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    with open(ISO_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"  log: {line}")


def ps_json(command: str):
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", full],
        capture_output=True, timeout=60,
    )
    text = out.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def probe_tcp(host: str, port: int, timeout: float) -> tuple[bool, str]:
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, f"connected in {(time.perf_counter() - t0) * 1000:.0f}ms"
    except Exception as exc:
        return False, f"{type(exc).__name__} after {(time.perf_counter() - t0) * 1000:.0f}ms ({exc})"


def http_get(host: str, port: int, path: str, timeout: float = 5.0) -> str:
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        data = b""
        while len(data) < 4096:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", errors="replace")
    finally:
        s.close()


def netstat_rows() -> list[tuple[str, str, str, str, int]]:
    out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=30)
    rows = []
    for line in out.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 5 and parts[0].upper() == "TCP":
            try:
                rows.append((parts[1], parts[2], parts[3], parts[4], int(parts[4])))
            except ValueError:
                continue
    return rows


def is_loopback_addr(addr: str) -> bool:
    host = addr.rsplit(":", 1)[0].strip("[]")
    if host.startswith("127."):
        return True
    if host in ("::1",):
        return True
    if host in ("0.0.0.0", "::", "[::]", "*", "") or host == "":
        return True  # wildcard: not a remote peer (LISTENING rows checked via local)
    return False


def audit_pids(pids: set[int]) -> list[str]:
    violations = []
    for local, foreign, state, _raw, pid in netstat_rows():
        if pid not in pids:
            continue
        if state.upper() == "ESTABLISHED" and not is_loopback_addr(foreign):
            violations.append(f"pid {pid}: ESTABLISHED to {foreign}")
        if state.upper() == "LISTENING":
            lhost = local.rsplit(":", 1)[0].strip("[]")
            if lhost in ("0.0.0.0", "::", "[::]"):
                violations.append(f"pid {pid}: PUBLIC LISTEN on {local}")
    return violations


def find_ipc_listener_pid() -> int | None:
    for local, foreign, state, _raw, pid in netstat_rows():
        if state.upper() == "LISTENING" and local.endswith(f":{IPC_PORT}"):
            lhost = local.rsplit(":", 1)[0].strip("[]")
            if lhost in ("127.0.0.1", "::1", "0.0.0.0", "::", "[::]"):
                return pid
    return None


async def main() -> int:
    print("=" * 60)
    print("PHASE 5 GATE TESTS — Network Isolation")
    print("=" * 60)

    # ── T1: rule inventory ──
    print("\n--- T1: isolation firewall rules ---")
    targets = [norm(sys.executable)]
    if FROZEN_EXE.exists():
        targets.append(norm(FROZEN_EXE))
    q = (
        "(Get-NetFirewallRule -DisplayName '" + RULE_PREFIX + "-*' | ForEach-Object { "
        "$p = (Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $_).Program; "
        "[PSCustomObject]@{ DisplayName = [string]$_.DisplayName; "
        "Enabled = [string]$_.Enabled; Direction = [string]$_.Direction; "
        "Action = [string]$_.Action; Program = [string]$p } } | ConvertTo-Json)"
    )
    raw = ps_json(q)
    rules = [] if raw is None else (raw if isinstance(raw, list) else [raw])
    rule_progs = {norm(r["Program"]) for r in rules if r.get("Program")}
    flags_ok = all(
        r.get("Enabled") == "True"
        and r.get("Direction") == "Outbound"
        and r.get("Action") == "Block"
        for r in rules
    ) if rules else False
    coverage = all(t in rule_progs for t in targets)
    exact = len(rules) == len(targets)  # stale sweep left nothing extra
    print(f"  expected targets: {len(targets)}, rules found: {len(rules)}")
    for r in rules:
        print(f"    - {r.get('DisplayName')} Enabled={r.get('Enabled')} "
              f"{r.get('Direction')}/{r.get('Action')} Program={r.get('Program')}")
    if not rules:
        print("  -> no rules. Run elevated: powershell -ExecutionPolicy Bypass "
              "-File scripts\\net_isolation.ps1 -Mode enable")
    rec("T1 rules exact/enable/outbound-block/coverage",
        bool(exact and flags_ok and coverage),
        f"exact={exact} flags={flags_ok} coverage={coverage}")

    # ── T2: outbound probe must be dropped ──
    print(f"\n--- T2: outbound probe {PROBE_HOST}:{PROBE_PORT} (handshake only) ---")
    reachable, detail = probe_tcp(PROBE_HOST, PROBE_PORT, PROBE_TIMEOUT)
    outcome = "ALLOWED" if reachable else "DROPPED"
    iso_log(f"probe {PROBE_HOST}:{PROBE_PORT} -> {outcome} ({detail})")
    print(f"  {detail}")
    rec("T2 outbound probe dropped by isolation rule", not reachable, detail)

    # ── T3: loopback intact under block ──
    print("\n--- T3: loopback intact (Ollama /api/tags under block rule) ---")
    try:
        resp = http_get(OLLAMA_HOST, OLLAMA_PORT, "/api/tags")
        ollama_ok = resp.startswith("HTTP/1.1 200") or resp.startswith("HTTP/2 200")
        note = resp.splitlines()[0] if resp else "no response"
    except Exception as exc:
        ollama_ok, note = False, f"{type(exc).__name__}: {exc}"
    print(f"  {note}")
    rec("T3 loopback exemption works (daemon usable offline)", ollama_ok, note)

    # ── T4: live daemon netstat audit ──
    print("\n--- T4: daemon netstat audit ---")
    daemon_proc: asyncio.subprocess.Process | None = None
    out_file = None
    existing_pid = find_ipc_listener_pid()
    try:
        if existing_pid:
            daemon_pid = existing_pid
            src = "existing daemon"
        else:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            out_file = open(LOG_DIR / "daemon_gate.out.log", "w", encoding="utf-8")
            daemon_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "blankslate",
                cwd=str(DAEMON_DIR),
                stdout=out_file, stderr=subprocess.STDOUT,
            )
            daemon_pid = daemon_proc.pid
            src = "gate-spawned daemon"
            for _ in range(20):
                if find_ipc_listener_pid() == daemon_pid:
                    break
                await asyncio.sleep(1.0)
            else:
                raise RuntimeError(
                    f"daemon did not open IPC port {IPC_PORT} within 20s "
                    f"(see logs/daemon_gate.out.log)"
                )
            await asyncio.sleep(1.5)  # settle
        print(f"  auditing pid {daemon_pid} ({src})")
        audit = {daemon_pid, os.getpid()}
        violations = audit_pids(audit)
        ipc_ok, ipc_detail = probe_tcp(IPC_HOST, IPC_PORT, 3.0)
        print(f"  IPC loopback {'open' if ipc_ok else 'closed'}; "
              f"violations: {violations or 'none'}")
        rec("T4 daemon: no non-loopback connections + IPC loopback works",
            bool(not violations and ipc_ok),
            f"v={len(violations)} ipc={ipc_ok} ({src})")
    except Exception as exc:
        rec("T4 daemon: no non-loopback connections + IPC loopback works",
            False, f"{type(exc).__name__}: {exc}")
    finally:
        if daemon_proc is not None:
            try:
                daemon_proc.terminate()
                await asyncio.wait_for(daemon_proc.wait(), timeout=8)
            except Exception:
                try:
                    daemon_proc.kill()
                    await daemon_proc.wait()
                except Exception:
                    pass
        if out_file is not None:
            out_file.close()

    # ── T5: dropped-connection log evidence ──
    print("\n--- T5: dropped-connection logging ---")
    own_log_ok = False
    if ISO_LOG.exists():
        tail = ISO_LOG.read_text(encoding="utf-8", errors="replace")[-4000:]
        own_log_ok = bool(
            re.search(rf"probe {re.escape(PROBE_HOST)}:{PROBE_PORT} -> DROPPED", tail)
        )
    sys_drop = None
    sys_readable = False
    if FW_LOG.exists():
        try:
            with open(FW_LOG, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 512 * 1024))
                fw_tail = f.read()
            sys_readable = True
            probe_epoch = time.time() - (PROBE_TIMEOUT + 60)
            for line in fw_tail.splitlines():
                if "DROP" not in line or PROBE_HOST not in line:
                    continue
                m = re.match(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(f"{m.group(1)} {m.group(2)}",
                                            "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
                if ts >= probe_epoch:
                    sys_drop = line.strip()
                    break
        except OSError:
            sys_readable = False
    print(f"  own isolation.log DROPPED record: {own_log_ok}")
    if sys_readable:
        print(f"  system firewall DROP line: {sys_drop or 'NOT FOUND'}")
    else:
        print(f"  system firewall log not readable (elevated?): {FW_LOG}")
    if sys_readable and sys_drop:
        rec("T5 dropped-connection logged (system firewall log)", True, sys_drop[:90])
    elif not sys_readable:
        rec("T5 dropped-connection logged (own log; system log not readable)",
            own_log_ok, "elevation-independent evidence only")
    else:
        rec("T5 dropped-connection logged (system firewall log)",
            False, "probe dropped but no DROP line in firewall log — "
                   "cannot attribute to isolation rule (logging off / network down?)")

    # ── Summary ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"PHASE 5 GATE: {passed}/{len(results)} tests passed")
    for name, ok, _ in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
