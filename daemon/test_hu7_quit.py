"""Phase B gate — quit prompt (Alt+F4/WM_CLOSE) + quit_app services kill.

T1  source: lib.rs carries on_window_event + CloseRequested + prevent_close
    + close_requested emit (targeted at "main", overlay window always
    suppressed, BS_HUD_QUIT_PROMPT=0 opt-out for the hu1 graceful-close
    persistence path) + quit_app(kill_all) command registered; autostart.rs
    carries pub fn listeners (netstat LISTENING parse) + kill_pid_tree
    (taskkill /T); QuitModal.tsx carries all four testids + killAll
    true/false invokes + services line; App.tsx listens close_requested and
    renders QuitModal in BOTH branches; pre-edit backups (hud_backup_b)
    predate the feature.
T2  npm run typecheck + npm run build + npm run tauri build --no-bundle.
T3  live CANCEL path: isolated-port autostart daemon up, WM_CLOSE to the
    'BlankSlate' window -> quit-modal appears <=5s showing the running
    services; click quit-cancel -> modal gone, HUD alive, daemon untouched
    (same owner PID).
T4  live QUIT HUD ONLY: WM_CLOSE -> modal -> click quit-hud-only -> HUD
    process exits, daemon on the isolated port KEEPS RUNNING with the same
    owner PID (quit without services kill).
T5  live QUIT ALL: relaunch HUD (autostart skips - daemon already up),
    WM_CLOSE -> modal -> click quit-all -> HUD exits AND the isolated-port
    daemon dies (taskkill tree, port free); the real daemon port env makes
    quit_all target ONLY the gate port (prod 8765 untouched). Ollama: if
    pre-running it must be stopped by quit-all (ground truth of kill_all)
    and then RELAUNCHED to restore pre-state; if pre-closed it stays closed.
T6  hygiene/restore: no stray blankslate-hud, gate port free, prod port
    owner list == pre-gate, ollama listening == pre-gate, backups restorable.

Ground truth: Win32 WM_CLOSE + window enumeration, CDP Runtime.evaluate
(DOM presence/text/clicks), process liveness + owning PIDs (netstat/psutil),
TCP port probes, disk reads — never assumptions.
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
LIB_RS = HUD_DIR / "src-tauri" / "src" / "lib.rs"
AUTOSTART_RS = HUD_DIR / "src-tauri" / "src" / "autostart.rs"
QUIT_TSX = HUD_DIR / "src" / "components" / "QuitModal.tsx"
APP_TSX = HUD_DIR / "src" / "App.tsx"
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_b")
PROD_PORT = 8765
OLLAMA_PORT = 11434
MAIN_TITLE = "BlankSlate"

RESULTS: list[tuple[str, bool, str]] = []
PROC: subprocess.Popen | None = None
CDP_PORT: int = 0
GATE_PORT: int = 0
MAIN_CDP: CDP | None = None
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


def _wait(pred, seconds: float, step: float = 0.3) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _launch() -> subprocess.Popen:
    global CDP_PORT
    CDP_PORT = free_port()
    env = dict(os.environ)
    env.pop("BS_HUD_AUTOSTART", None)
    env.update({
        "BS_DAEMON_HOST": "127.0.0.1",
        "BS_DAEMON_PORT": str(GATE_PORT),
        "BS_IPC_PORT": str(GATE_PORT),
        "BS_TTS_ENABLED": "False",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            f"--remote-debugging-port={CDP_PORT}",
    })
    return subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env)


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


def _close_cdp(cdp: CDP | None) -> None:
    if cdp is not None:
        try:
            cdp.close()
        except Exception:
            pass


def _main_hwnd(timeout_s: float = 8.0) -> int | None:
    import win32gui

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found: list[int] = []

        def cb(h, _):
            try:
                if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == MAIN_TITLE:
                    found.append(h)
            except Exception:
                pass

        win32gui.EnumWindows(cb, None)
        if found:
            return found[0]
        time.sleep(0.3)
    return None


def _wm_close(hwnd: int) -> bool:
    import win32con
    import win32gui

    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


def _has_modal(cdp: CDP) -> bool:
    try:
        return bool(_eval(cdp, "!!document.querySelector('[data-testid=\"quit-modal\"]')", timeout=5))
    except Exception:
        return False


def _modal_text(cdp: CDP) -> str:
    try:
        return str(_eval(
            cdp,
            "(document.querySelector('[data-testid=\"quit-modal\"]')||{innerText:''}).innerText",
            timeout=5,
        ))
    except Exception:
        return ""


def _click(cdp: CDP, testid: str) -> str:
    try:
        return str(_eval(
            cdp,
            "(()=>{const el=document.querySelector('[data-testid=\"" + testid + "\"]');"
            "if(!el)return 'missing';el.click();return 'clicked';})()",
            timeout=5,
        ))
    except Exception as e:
        return f"err:{e}"


# ---- T1: source -----------------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: quit prompt source wiring ---")
    lib = LIB_RS.read_text(encoding="utf-8") if LIB_RS.exists() else ""
    for needle in (
        "on_window_event",
        "CloseRequested",
        "prevent_close",
        "close_requested",
        '"overlay"',
        "quit_app",
        "kill_all",
        "listeners",
        "BS_HUD_QUIT_PROMPT",
    ):
        rec(f"T1 lib.rs carries {needle}", needle in lib, "window event + command")

    hu1 = DAEMON_DIR / "test_hu1_theme.py"
    hu1_src = hu1.read_text(encoding="utf-8") if hu1.exists() else ""
    rec("T1 hu1 graceful-close path opts out of the prompt (BS_HUD_QUIT_PROMPT)",
        "BS_HUD_QUIT_PROMPT" in hu1_src, "persistence restart test unaffected")
    rec("T1 hu1 backup pre-edit proof",
        (BACKUP_DIR / "test_hu1_theme.py.bak").exists()
        and "BS_HUD_QUIT_PROMPT"
        not in (BACKUP_DIR / "test_hu1_theme.py.bak").read_text(encoding="utf-8"),
        f"path={BACKUP_DIR / 'test_hu1_theme.py.bak'}")

    auto = AUTOSTART_RS.read_text(encoding="utf-8") if AUTOSTART_RS.exists() else ""
    for needle in ("pub fn listeners", "netstat", "LISTENING", "kill_pid_tree", "taskkill"):
        rec(f"T1 autostart.rs carries {needle}", needle in auto, "pid resolution + tree kill")

    quit_src = QUIT_TSX.read_text(encoding="utf-8") if QUIT_TSX.exists() else ""
    for needle in (
        'data-testid="quit-modal"',
        'data-testid="quit-all"',
        'data-testid="quit-hud-only"',
        'data-testid="quit-cancel"',
        "killAll: true",
        "killAll: false",
        "Also stop",
    ):
        rec(f"T1 QuitModal.tsx carries {needle}", needle in quit_src, "modal testids + invokes")

    app = APP_TSX.read_text(encoding="utf-8") if APP_TSX.exists() else ""
    rec("T1 App.tsx listens close_requested", '"close_requested"' in app, "event -> state")
    rec("T1 App.tsx renders QuitModal in BOTH branches",
        app.count("<QuitModal") == 2, f"renders={app.count('<QuitModal')}")
    rec("T1 App.tsx imports QuitModal", "import QuitModal" in app, "component import")

    proofs = [
        ("App.tsx.bak", "close_requested"),
        ("lib.rs.bak", "quit_app"),
        ("autostart.rs.bak", "pub fn listeners"),
    ]
    for bak, needle in proofs:
        b = BACKUP_DIR / bak
        ok = b.exists() and needle not in b.read_text(encoding="utf-8")
        rec(f"T1 backup pre-edit proof {bak}", ok, f"path={b}")


# ---- T2: build ------------------------------------------------------------

def t2_build() -> None:
    print("\n--- T2: typecheck + vite build + tauri build ---")
    rc, out = _run("npm run typecheck", HUD_DIR, timeout=300)
    rec("T2 npm run typecheck exit 0", rc == 0, f"rc={rc}")
    rc, out = _run("npm run build", HUD_DIR, timeout=600)
    rec("T2 npm run build exit 0", rc == 0,
        f"rc={rc} last={out.strip().splitlines()[-1:]}")
    rc, out = _run("npm run tauri build -- --no-bundle", HUD_DIR, timeout=900)
    rec("T2 npm run tauri build --no-bundle exit 0", rc == 0,
        f"rc={rc} last={out.strip().splitlines()[-1:]}")
    rec("T2 fresh exe present", EXE.exists(), f"path={EXE}")


# ---- T3: cancel path ------------------------------------------------------

def t3_cancel() -> None:
    global PROC, MAIN_CDP
    print("\n--- T3: WM_CLOSE -> modal -> Cancel keeps HUD + daemon ---")
    PROC = _launch()
    up = _wait(lambda: _port_open(GATE_PORT), 90, 0.5)
    owners = _listeners(GATE_PORT)
    rec("T3 autostart daemon up on gate port", up,
        f"port={GATE_PORT} owners={owners}")

    MAIN_CDP = _attach_main()
    rec("T3 CDP attached to main webview", MAIN_CDP is not None,
        f"cdp_port={CDP_PORT}")

    hwnd = _main_hwnd()
    rec("T3 main 'BlankSlate' window found", hwnd is not None, f"hwnd={hwnd}")

    sent = _wm_close(hwnd) if hwnd else False
    rec("T3 WM_CLOSE posted", sent, f"hwnd={hwnd}")

    modal = False
    if MAIN_CDP is not None:
        modal = _wait(lambda: _has_modal(MAIN_CDP), 5, 0.3)
    text = _modal_text(MAIN_CDP) if MAIN_CDP else ""
    rec("T3 quit-modal appears <=5s on WM_CLOSE", modal, f"visible={modal}")
    rec("T3 modal lists running services (payload ground truth)",
        modal and "daemon" in text.lower(),
        f"text={text.replace(chr(10), ' | ')}")

    clicked = _click(MAIN_CDP, "quit-cancel") if MAIN_CDP else "no-cdp"
    rec("T3 quit-cancel clicked", clicked == "clicked", f"result={clicked}")
    gone = _wait(lambda: not _has_modal(MAIN_CDP), 3, 0.3) if MAIN_CDP else False
    rec("T3 modal dismissed on cancel", gone, f"gone={gone}")
    alive = PROC.poll() is None
    rec("T3 HUD still alive after cancel", alive, f"exit={PROC.poll()}")
    still = _listeners(GATE_PORT)
    rec("T3 daemon untouched (same owner PID)",
        still == owners and len(still) >= 1, f"before={owners} after={still}")


# ---- T4: quit HUD only ----------------------------------------------------

def t4_quit_hud_only() -> None:
    global MAIN_CDP
    print("\n--- T4: quit-hud-only exits HUD, daemon keeps running ---")
    hwnd = _main_hwnd()
    sent = _wm_close(hwnd) if hwnd else False
    rec("T4 WM_CLOSE posted", sent, f"hwnd={hwnd}")

    modal = False
    if MAIN_CDP is not None:
        modal = _wait(lambda: _has_modal(MAIN_CDP), 5, 0.3)
    rec("T4 quit-modal reappears", modal, f"visible={modal}")

    owners_before = _listeners(GATE_PORT)
    clicked = _click(MAIN_CDP, "quit-hud-only") if MAIN_CDP else "no-cdp"
    rec("T4 quit-hud-only clicked", clicked == "clicked", f"result={clicked}")

    exited = _wait(lambda: PROC.poll() is not None, 8, 0.3)
    rec("T4 HUD process exited <=8s", exited, f"exit={PROC.poll()}")
    _close_cdp(MAIN_CDP)
    MAIN_CDP = None

    survived = _wait(
        lambda: _listeners(GATE_PORT) == owners_before and len(owners_before) >= 1,
        3, 0.3,
    )
    rec("T4 daemon STILL running (same owner PID, not killed)",
        survived, f"before={owners_before} after={_listeners(GATE_PORT)}")


# ---- T5: quit all ---------------------------------------------------------

def t5_quit_all() -> None:
    global PROC, MAIN_CDP
    print("\n--- T5: quit-all stops HUD AND gate-port daemon ---")
    PROC = _launch()
    connected = _wait(lambda: _port_open(GATE_PORT), 5, 0.3)
    rec("T5 gate daemon still up at relaunch (autostart skips)", connected,
        f"port={GATE_PORT}")

    MAIN_CDP = _attach_main()
    rec("T5 CDP attached after relaunch", MAIN_CDP is not None,
        f"cdp_port={CDP_PORT}")

    hwnd = _main_hwnd()
    sent = _wm_close(hwnd) if hwnd else False
    rec("T5 WM_CLOSE posted", sent, f"hwnd={hwnd}")
    modal = False
    if MAIN_CDP is not None:
        modal = _wait(lambda: _has_modal(MAIN_CDP), 5, 0.3)
    rec("T5 quit-modal appears", modal, f"visible={modal}")

    clicked = _click(MAIN_CDP, "quit-all") if MAIN_CDP else "no-cdp"
    rec("T5 quit-all clicked", clicked == "clicked", f"result={clicked}")

    hud_gone = _wait(lambda: PROC.poll() is not None, 8, 0.3)
    rec("T5 HUD process exited <=8s", hud_gone, f"exit={PROC.poll()}")
    daemon_gone = _wait(lambda: not _port_open(GATE_PORT), 10, 0.5)
    rec("T5 gate-port daemon stopped by quit-all", daemon_gone,
        f"port={GATE_PORT} owners={_listeners(GATE_PORT)}")

    if PRE["ollama_open"]:
        stopped = _wait(lambda: not _port_open(OLLAMA_PORT), 8, 0.5)
        rec("T5 quit-all stopped the pre-running ollama (kill_all ground truth)",
            stopped, f"pre_owners={PRE['ollama_owners']}")
        subprocess.Popen(
            [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"), "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        back = _wait(lambda: _port_open(OLLAMA_PORT), 30, 1.0)
        rec("T5 ollama relaunched to restore pre-state", back,
            "pre-state was running")
    else:
        rec("T5 ollama stays closed (was closed pre-gate)",
            not _port_open(OLLAMA_PORT), "pre-state was closed")


# ---- T6: hygiene ----------------------------------------------------------

def t6_hygiene() -> None:
    print("\n--- T6: hygiene + pre-state restore proof ---")
    _close_cdp(MAIN_CDP)
    _kill_hud_leftovers()
    time.sleep(0.6)

    hud_left = _hud_instances()
    rec("T6 no leftover blankslate-hud process", hud_left == 0,
        f"remaining={hud_left}")
    rec("T6 gate daemon port free", not _port_open(GATE_PORT),
        f"port={GATE_PORT}")
    prod_now = _listeners(PROD_PORT)
    rec("T6 prod port 8765 owner list == pre-gate", prod_now == PRE["prod_owners"],
        f"pre={PRE['prod_owners']} post={prod_now}")
    oll_ok = _port_open(OLLAMA_PORT) == PRE["ollama_open"]
    rec("T6 ollama pre-state restored (listening == pre-gate)", oll_ok,
        f"pre_open={PRE['ollama_open']} now_open={_port_open(OLLAMA_PORT)}")

    backups_ok = True
    for bak, needle in (
        ("App.tsx.bak", "close_requested"),
        ("lib.rs.bak", "quit_app"),
        ("autostart.rs.bak", "pub fn listeners"),
    ):
        b = BACKUP_DIR / bak
        if not (b.exists() and needle not in b.read_text(encoding="utf-8")):
            backups_ok = False
    rec("T6 pre-edit backups present and restorable", backups_ok,
        f"dir={BACKUP_DIR}")


def main() -> int:
    global GATE_PORT, CDP_PORT
    os.environ.pop("BS_HUD_AUTOSTART", None)
    GATE_PORT = free_port()

    PRE["prod_owners"] = _listeners(PROD_PORT)
    PRE["ollama_open"] = _port_open(OLLAMA_PORT)
    PRE["ollama_owners"] = _listeners(OLLAMA_PORT)
    PRE["daemon_count"] = _count_procs("blankslate-daemon")
    print(f"PRE: prod_owners={PRE['prod_owners']} ollama_open={PRE['ollama_open']} "
          f"daemon_count={PRE['daemon_count']}")

    try:
        t1_source()
        t2_build()
        t3_cancel()
        t4_quit_hud_only()
        t5_quit_all()
    finally:
        t6_hygiene()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
        print("RESTORE (only if sources must revert):")
        print(f'  Copy-Item "{BACKUP_DIR}\\App.tsx.bak" "{HUD_DIR}\\src\\App.tsx" -Force')
        print(f'  Copy-Item "{BACKUP_DIR}\\lib.rs.bak" "{HUD_DIR}\\src-tauri\\src\\lib.rs" -Force')
        print(f'  Copy-Item "{BACKUP_DIR}\\autostart.rs.bak" "{HUD_DIR}\\src-tauri\\src\\autostart.rs" -Force')
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
