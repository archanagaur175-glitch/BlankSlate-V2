"""Phase A gate — HUD autostart (daemon + Ollama) on HUD launch.

T1  source: autostart.rs exists and carries the full design (BS_HUD_AUTOSTART
    opt-out, frozen-exe-first + run_daemon.py fallback, ollama.exe serve with
    the start_prod env recipe, CREATE_NO_WINDOW, port probes with default
    8765); lib.rs wires mod autostart + autostart::run + AutostartState +
    app.manage; all 6 HUD-launching gates (hud0/hu1/hu2/hu3/hu4/ov1) and
    hud_fouc.py carry BS_HUD_AUTOSTART=0 (opt-out so the suite never spawns
    daemons); pre-edit backups in hud_backup_a exist and do NOT contain the
    new key (restorable proof).
T2  npm run typecheck + npm run build + npm run tauri build --no-bundle,
    fresh exe present.
T3  live autostart on an ISOLATED port: launch HUD WITHOUT BS_HUD_AUTOSTART
    (default ON) with BS_DAEMON_PORT/BS_IPC_PORT=<free>; the HUD must spawn
    the daemon itself (frozen exe first) -> port listening <=90s, exactly one
    new daemon process, gate WS get_state subscribers >=2 (gate + HUD bridge),
    CDP compact view shows the daemonState chip ("turns") so the bridge is
    really pumping state. Prod port 8765 owner (if any) must be untouched.
T4  idempotency: kill the HUD, relaunch against the SAME daemon port ->
    same port-owner PID and process count (no double spawn) + bridge
    reconnects (subscribers >=2 again).
T5  opt-out: launch with BS_HUD_AUTOSTART=0 and a second free port ->
    nothing spawns (port closed, daemon process count unchanged).
T6  ollama branch (pre-state recorded): if 11434 was LISTENING pre-gate the
    owner PID must be unchanged (skip, no second process); if it was closed
    the HUD must spawn it (port opens) and the gate kills it back to closed.
T7  restore/hygiene: HUD trees killed, gate-port daemon tree killed (port
    free), prod-port owner list byte-equal to pre-gate, ollama state matches
    pre-gate, no stray blankslate-hud, backups restorable.

Ground truth: TCP port probes, netstat/psutil owning PIDs, process counts,
gate WebSocket subscriber counts, CDP Runtime.evaluate on the live compact
view, disk reads — never assumptions.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hud_p0_probe import CDP, free_port

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
EXE = HUD_DIR / "src-tauri" / "target" / "release" / "blankslate-hud.exe"
AUTOSTART_RS = HUD_DIR / "src-tauri" / "src" / "autostart.rs"
LIB_RS = HUD_DIR / "src-tauri" / "src" / "lib.rs"
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_a")
PROD_PORT = 8765
OLLAMA_PORT = 11434

ALL_EDITED = [
    "test_hud0_tokens.py",
    "test_hu1_theme.py",
    "test_hu2_compact.py",
    "test_hu3_dashboard.py",
    "test_hu4_overlay.py",
    "test_ov1_overlay.py",
    "hud_fouc.py",
]

DIRECT_OPTOUT = [
    "test_hu1_theme.py",
    "test_hu2_compact.py",
    "test_hu3_dashboard.py",
    "test_hu4_overlay.py",
    "test_ov1_overlay.py",
    "hud_fouc.py",
]

RESULTS: list[tuple[str, bool, str]] = []
PROC: subprocess.Popen | None = None
CDP_PORT: int = 0
DAEMON_PORT_A: int = 0
DAEMON_PORT_B: int = 0
PRE: dict[str, Any] = {}


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def _run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), shell=True,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


def _listeners(port: int) -> list[int]:
    pids: list[int] = []
    try:
        import psutil

        for c in psutil.net_connections(kind="tcp"):
            if (
                c.laddr
                and getattr(c.laddr, "port", None) == port
                and c.status == psutil.CONN_LISTEN
                and c.pid
            ):
                pids.append(c.pid)
        if pids:
            return sorted(set(pids))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        ).stdout
        for ln in out.splitlines():
            parts = ln.split()
            if (
                len(parts) >= 5
                and parts[0].upper() == "TCP"
                and parts[3] == "LISTENING"
                and parts[1].endswith(f":{port}")
            ):
                pids.append(int(parts[4]))
    except Exception:
        pass
    return sorted(set(pids))


def _count_procs(substr: str) -> int:
    try:
        import psutil

        n = 0
        for p in psutil.process_iter(["name"]):
            if substr in (p.info.get("name") or "").lower():
                n += 1
        return n
    except Exception:
        return -1


def _kill_pid_tree(pid: int) -> None:
    try:
        import psutil

        proc = psutil.Process(pid)
        kids = [c.pid for c in proc.children(recursive=True)]
        proc.kill()
        for k in kids:
            try:
                psutil.Process(k).kill()
            except Exception:
                pass
    except Exception:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=15,
        )


def _hud_instances() -> int:
    return _count_procs("blankslate-hud")


def _kill_hud_leftovers() -> int:
    found = _hud_instances()
    try:
        import psutil

        for p in psutil.process_iter(["name"]):
            if "blankslate-hud" in (p.info.get("name") or "").lower():
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass
    return found


def _kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        _kill_pid_tree(proc.pid)
    except Exception:
        pass


def _kill_hud_root(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        import psutil

        p = psutil.Process(proc.pid)
        kids = p.children(recursive=True)
        for c in kids:
            n = (c.name() or "").lower()
            if "blankslate-daemon" in n or n.startswith("python"):
                continue
            try:
                c.kill()
            except Exception:
                pass
        p.kill()
    except Exception:
        pass


def _list_pages(port: int) -> list[dict]:
    import requests

    try:
        return [
            t
            for t in requests.get(f"http://127.0.0.1:{port}/json", timeout=5).json()
            if t.get("type") == "page" and "tauri.localhost" in t.get("url", "")
        ]
    except Exception:
        return []


def _connect(port: int, target: dict) -> CDP:
    from websockets.sync.client import connect as ws_connect

    c = CDP(port)
    c.ws = ws_connect(target["webSocketDebuggerUrl"], open_timeout=10)
    c.cmd("Page.enable")
    return c


def _eval(cdp: CDP, expr: str, timeout: float = 10.0) -> Any:
    last: Exception | None = None
    for _ in range(6):
        try:
            return cdp.eval_js(expr, timeout=timeout)
        except RuntimeError as e:
            if "Execution context was destroyed" in str(e):
                time.sleep(0.4)
                last = e
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise RuntimeError(f"eval never succeeded: {last}")


def _attach_main(timeout_s: float = 14.0) -> CDP | None:
    deadline = time.time() + timeout_s
    seen: set[str] = set()
    while time.time() < deadline:
        for tgt in _list_pages(CDP_PORT):
            if tgt["id"] in seen:
                continue
            seen.add(tgt["id"])
            try:
                c = _connect(CDP_PORT, tgt)
            except Exception:
                continue
            try:
                is_main = bool(
                    _eval(c, "!!document.querySelector('.hud-root')", timeout=5)
                )
            except Exception:
                is_main = False
            if is_main:
                return c
            try:
                c.close()
            except Exception:
                pass
        time.sleep(0.4)
    return None


def _wait(pred, seconds: float, step: float = 0.5) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _launch(daemon_port: int, autostart: str | None, cdp_port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("BS_HUD_AUTOSTART", None)
    if autostart is not None:
        env["BS_HUD_AUTOSTART"] = autostart
    env.update({
        "BS_DAEMON_HOST": "127.0.0.1",
        "BS_DAEMON_PORT": str(daemon_port),
        "BS_IPC_PORT": str(daemon_port),
        "BS_TTS_ENABLED": "False",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            f"--remote-debugging-port={cdp_port}",
    })
    return subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env)


def _daemon_token() -> str:
    """Token of the running daemon: frozen exe writes
    dist/blankslate-daemon/_internal/data/ipc_token (auth ON for frozen);
    dev run_daemon.py writes data/ipc_token (tokenless when not frozen).
    Presenting it is the production handshake contract (server.py guard)."""
    for p in (
        DAEMON_DIR / "dist" / "blankslate-daemon" / "_internal" / "data" / "ipc_token",
        DAEMON_DIR / "data" / "ipc_token",
    ):
        try:
            tok = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if tok:
            return tok
    return ""


def _ws_subscribers(port: int) -> int:
    from websockets.sync.client import connect as ws_connect

    token = _daemon_token()
    url = f"ws://127.0.0.1:{port}" + (f"?token={token}" if token else "")
    try:
        with ws_connect(url, open_timeout=5) as ws:
            ws.send(json.dumps({"type": "get_state"}))
            deadline = time.time() + 6
            while time.time() < deadline:
                m = json.loads(ws.recv(timeout=6))
                if m.get("type") == "state":
                    return int(m.get("subscribers", 0))
    except Exception:
        return -1
    return -1


# ---- T1: source + backups -------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: autostart source wiring + gate opt-out coverage ---")
    src = AUTOSTART_RS.read_text(encoding="utf-8") if AUTOSTART_RS.exists() else ""
    for needle in (
        "BS_HUD_AUTOSTART",
        "blankslate-daemon.exe",
        "run_daemon.py",
        "ollama.exe",
        "OLLAMA_FLASH_ATTENTION",
        "0x0800_0000",
        "8765",
        "spawn_daemon",
        "spawn_ollama",
        "wait_port",
    ):
        rec(f"T1 autostart.rs carries {needle}", needle in src,
            f"path={AUTOSTART_RS.name}")

    lib = LIB_RS.read_text(encoding="utf-8") if LIB_RS.exists() else ""
    for needle in ("mod autostart;", "autostart::run", "AutostartState", "app.manage"):
        rec(f"T1 lib.rs wires {needle}", needle in lib, "setup spawn + state")

    for gf in DIRECT_OPTOUT:
        p = DAEMON_DIR / gf
        has = p.exists() and "BS_HUD_AUTOSTART" in p.read_text(encoding="utf-8")
        rec(f"T1 {gf} carries BS_HUD_AUTOSTART opt-out", has, "suite never spawns")

    hud0 = DAEMON_DIR / "test_hud0_tokens.py"
    hud0_src = hud0.read_text(encoding="utf-8") if hud0.exists() else ""
    fouc = DAEMON_DIR / "hud_fouc.py"
    fouc_has = fouc.exists() and "BS_HUD_AUTOSTART" in fouc.read_text(encoding="utf-8")
    rec("T1 hud0 exe launches go through hud_fouc.py opt-out",
        "hud_fouc" in hud0_src and fouc_has,
        "hud0 spawns the exe only via hud_fouc.py")

    bak_ok = True
    for gf in ALL_EDITED:
        b = BACKUP_DIR / f"{gf}.bak"
        if not (b.exists() and "BS_HUD_AUTOSTART" not in b.read_text(encoding="utf-8")):
            bak_ok = False
            rec(f"T1 backup pre-edit proof {gf}", False, f"path={b}")
    rec("T1 all 7 pre-edit backups present and pre-edit", bak_ok,
        f"dir={BACKUP_DIR}")


# ---- T2: build ------------------------------------------------------------

def t2_build() -> None:
    print("\n--- T2: typecheck + vite build + tauri build ---")
    rc, out = _run("npm run typecheck", HUD_DIR, timeout=300)
    rec("T2 npm run typecheck exit 0", rc == 0, f"rc={rc}")
    rc, out = _run("npm run build", HUD_DIR, timeout=600)
    rec("T2 npm run build exit 0", rc == 0, f"rc={rc} last={out.strip().splitlines()[-1:]}")
    rc, out = _run("npm run tauri build -- --no-bundle", HUD_DIR, timeout=900)
    rec("T2 npm run tauri build --no-bundle exit 0", rc == 0,
        f"rc={rc} last={out.strip().splitlines()[-1:]}")
    rec("T2 fresh exe present", EXE.exists(), f"path={EXE}")


# ---- T3: live autostart ---------------------------------------------------

def t3_autostart_live() -> None:
    global PROC, CDP_PORT
    print("\n--- T3: HUD spawns daemon itself (isolated port, default ON) ---")
    CDP_PORT = free_port()
    PROC = _launch(DAEMON_PORT_A, None, CDP_PORT)
    rec("T3 HUD launched (autostart default ON)", PROC.pid is not None,
        f"pid={PROC.pid} daemon_port={DAEMON_PORT_A}")

    open_up = _wait(lambda: _port_open(DAEMON_PORT_A), 90, 0.5)
    owners = _listeners(DAEMON_PORT_A)
    rec("T3 daemon listening on isolated port <=90s (spawned by HUD)", open_up,
        f"port={DAEMON_PORT_A} owners={owners}")

    new_daemons = _count_procs("blankslate-daemon") - PRE["daemon_count"]
    rec("T3 exactly one new daemon process", new_daemons == 1,
        f"pre={PRE['daemon_count']} post={_count_procs('blankslate-daemon')}")

    subs = -1
    if open_up:
        deadline = time.time() + 20
        while time.time() < deadline:
            subs = _ws_subscribers(DAEMON_PORT_A)
            if subs >= 2:
                break
            time.sleep(1.0)
    rec("T3 HUD bridge subscribed (gate+HUD >= 2)", subs >= 2,
        f"subscribers={subs}")

    text = ""
    cdp = _attach_main()
    if cdp is not None:
        try:
            text = str(_eval(
                cdp,
                "(document.querySelector('.hud-root')||{innerText:''}).innerText",
                timeout=6,
            ))
        except Exception:
            text = ""
        try:
            cdp.close()
        except Exception:
            pass
    rec("T3 compact view renders daemonState chip (CDP innerText)",
        "turns" in text.lower(), f"found={'turns' in text.lower()}")

    prod_now = _listeners(PROD_PORT)
    rec("T3 prod port 8765 owner untouched", prod_now == PRE["prod_owners"],
        f"pre={PRE['prod_owners']} post={prod_now}")


# ---- T4: idempotency ------------------------------------------------------

def t4_idempotent() -> None:
    global PROC, CDP_PORT
    print("\n--- T4: relaunch against the same daemon -> no second spawn ---")
    owner_before = _listeners(DAEMON_PORT_A)
    count_before = _count_procs("blankslate-daemon")
    _kill_hud_root(PROC)
    _kill_hud_leftovers()
    time.sleep(0.5)

    CDP_PORT = free_port()
    PROC = _launch(DAEMON_PORT_A, None, CDP_PORT)
    ok = _wait(lambda: PROC.poll() is None, 3, 0.2)
    time.sleep(4)

    owner_after = _listeners(DAEMON_PORT_A)
    count_after = _count_procs("blankslate-daemon")
    rec("T4 daemon not respawned (same port-owner PID)",
        ok and owner_after == owner_before and len(owner_after) >= 1,
        f"before={owner_before} after={owner_after}")
    rec("T4 daemon process count unchanged",
        count_after == count_before, f"before={count_before} after={count_after}")

    subs = -1
    deadline = time.time() + 20
    while time.time() < deadline:
        subs = _ws_subscribers(DAEMON_PORT_A)
        if subs >= 2:
            break
        time.sleep(1.0)
    rec("T4 HUD bridge reconnected to pre-existing daemon", subs >= 2,
        f"subscribers={subs}")

    oll_now = _listeners(OLLAMA_PORT)
    if PRE["ollama_open"]:
        rec("T4 no second ollama (owner unchanged)",
            oll_now == PRE["ollama_owners"], f"pre={PRE['ollama_owners']} post={oll_now}")


# ---- T5: opt-out ----------------------------------------------------------

def t5_opt_out() -> None:
    global PROC, CDP_PORT
    print("\n--- T5: BS_HUD_AUTOSTART=0 spawns nothing ---")
    _kill_hud_root(PROC)
    _kill_hud_leftovers()
    time.sleep(0.5)

    count_before = _count_procs("blankslate-daemon")
    CDP_PORT = free_port()
    PROC = _launch(DAEMON_PORT_B, "0", CDP_PORT)
    time.sleep(6)
    port_up = _port_open(DAEMON_PORT_B)
    count_after = _count_procs("blankslate-daemon")
    rec("T5 opt-out: second isolated port stayed closed", not port_up,
        f"port={DAEMON_PORT_B}")
    rec("T5 opt-out: no new daemon process", count_after == count_before,
        f"before={count_before} after={count_after}")

    oll_count = _count_procs("ollama")
    rec("T5 opt-out: ollama process count unchanged", oll_count == PRE["ollama_count"],
        f"count={oll_count} pre={PRE['ollama_count']}")


# ---- T6: ollama branch ----------------------------------------------------

def t6_ollama() -> None:
    print("\n--- T6: ollama autostart branch (pre-state honored) ---")
    if PRE["ollama_open"]:
        now = _listeners(OLLAMA_PORT)
        rec("T6 ollama pre-running -> owner unchanged (skip spawn)",
            now == PRE["ollama_owners"] and len(now) >= 1,
            f"pre={PRE['ollama_owners']} post={now}")
    else:
        up = _wait(lambda: _port_open(OLLAMA_PORT), 60, 1.0)
        owners = _listeners(OLLAMA_PORT)
        rec("T6 ollama pre-closed -> HUD spawned it (port opens)", up,
            f"owners={owners}")
        for pid in owners:
            _kill_pid_tree(pid)
        time.sleep(1.0)
        rec("T6 ollama restored to closed after kill", not _port_open(OLLAMA_PORT),
            "pre-state was closed")


# ---- T7: restore + hygiene ------------------------------------------------

def t7_hygiene() -> None:
    print("\n--- T7: restore + hygiene ---")
    _kill_hud_root(PROC)
    _kill_hud_leftovers()
    time.sleep(0.6)

    for pid in _listeners(DAEMON_PORT_A):
        _kill_pid_tree(pid)
    time.sleep(0.6)
    if not PRE["prod_had_daemon"]:
        for pid in _listeners(PROD_PORT):
            if pid not in PRE["prod_owners"]:
                _kill_pid_tree(pid)
    if PRE["ollama_open"]:
        if not _port_open(OLLAMA_PORT):
            subprocess.Popen(
                [
                    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
                    "serve",
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _wait(lambda: _port_open(OLLAMA_PORT), 30, 1.0)
    else:
        for pid in _listeners(OLLAMA_PORT):
            _kill_pid_tree(pid)
        time.sleep(1.0)

    time.sleep(0.5)
    hud_left = _hud_instances()
    rec("T7 no leftover blankslate-hud process", hud_left == 0,
        f"remaining={hud_left}")
    rec("T7 gate daemon port free", not _port_open(DAEMON_PORT_A),
        f"port={DAEMON_PORT_A}")
    rec("T7 opt-out port free", not _port_open(DAEMON_PORT_B),
        f"port={DAEMON_PORT_B}")
    rec("T7 prod port owner list restored",
        _listeners(PROD_PORT) == PRE["prod_owners"],
        f"pre={PRE['prod_owners']} post={_listeners(PROD_PORT)}")
    oll_ok = _port_open(OLLAMA_PORT) == PRE["ollama_open"]
    rec("T7 ollama pre-state restored", oll_ok,
        f"pre_open={PRE['ollama_open']} now_open={_port_open(OLLAMA_PORT)}")

    backups_ok = True
    for gf in ALL_EDITED:
        b = BACKUP_DIR / f"{gf}.bak"
        if not (b.exists() and "BS_HUD_AUTOSTART" not in b.read_text(encoding="utf-8")):
            backups_ok = False
    rec("T7 pre-edit backups present and restorable", backups_ok,
        f"dir={BACKUP_DIR}")


def main() -> int:
    global DAEMON_PORT_A, DAEMON_PORT_B, CDP_PORT
    os.environ.pop("BS_HUD_AUTOSTART", None)
    DAEMON_PORT_A = free_port()
    DAEMON_PORT_B = free_port()
    CDP_PORT = free_port()

    PRE["prod_owners"] = _listeners(PROD_PORT)
    PRE["prod_had_daemon"] = len(PRE["prod_owners"]) > 0
    PRE["ollama_open"] = _port_open(OLLAMA_PORT)
    PRE["ollama_owners"] = _listeners(OLLAMA_PORT)
    PRE["daemon_count"] = _count_procs("blankslate-daemon")
    PRE["ollama_count"] = _count_procs("ollama")
    print(f"PRE: prod_owners={PRE['prod_owners']} ollama_open={PRE['ollama_open']} "
          f"daemon_count={PRE['daemon_count']} ollama_count={PRE['ollama_count']}")

    try:
        t1_source()
        t2_build()
        t3_autostart_live()
        t4_idempotent()
        t5_opt_out()
        t6_ollama()
    finally:
        t7_hygiene()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
        print("RESTORE (only if sources must revert):")
        for gf in ALL_EDITED:
            print(f'  Copy-Item "{BACKUP_DIR}\\{gf}.bak" "{DAEMON_DIR}\\{gf}" -Force')
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
