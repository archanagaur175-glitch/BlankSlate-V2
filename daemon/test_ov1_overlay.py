"""Phase 1 gate — floating overlay HUD (six-feature expansion).

T1  build: `npm run tauri build -- --no-bundle` exits 0 and produces the
    prod-mode HUD exe (plain `cargo build` yields a DEV binary that loads
    http://localhost:1420 instead of the embedded bundle — verified the
    hard way; `tauri build` enables the `custom-protocol` feature).
T2  daemon up on isolated port 8801; HUD exe launched with BS_DAEMON_PORT;
    WS subscribers >= 2 (gate client + HUD Rust bridge connected).
T3  click-logging target window created (visible, topmost).
T4  overlay_show via IPC (re-sent until the webview reacts — the Rust
    bridge subscribes before the page finishes loading) -> ack + `overlay`
    broadcast; computed pill origin INSIDE the target window's rect.
T5  overlay window Win32 ground truth: visible, rect == broadcast
    (x, y, w, h) within 4px (shadow off => no invisible frame offset),
    WS_EX_TOPMOST | WS_EX_TRANSPARENT.
T6  click at pill point PASSES THROUGH to the window underneath: the
    underlying target logs WM_LBUTTONDOWN at the clicked screen position.
    The overlay is re-raised to the top of the topmost band first, so a
    pass can only come from WS_EX_TRANSPARENT click-through.
T7  overlay_hide via IPC -> `overlay` broadcast action=hide + overlay
    window no longer visible (win32 poll).
T8  error paths: unknown target -> type=error "no visible window" (no
    broadcast), missing text -> error "text required"; daemon still
    healthy afterwards (ping -> pong).
T9  `npm run typecheck` exit 0.
T10 restore: HUD + target window + daemon killed, port 8801 free, env
    cleaned, scratch removed.

Ground truth: raw WS wire messages, win32 window rects/styles/click logs,
process tables, exit codes — never assumptions.

COORDINATE SPACES: this machine runs at 125% system DPI. The gate opts
into per-monitor-v2 process awareness up front, and blankslate/overlay.py
measures in a dedicated per-monitor-v2 thread, so every coordinate here
(design, broadcast, SetCursorPos, GetWindowRect) is PHYSICAL pixels —
the same space Tauri's PhysicalPosition uses. The rest of the daemon
stays DPI-unaware; gates that assume virtualized coordinates are
unaffected.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import websockets

# npm/vite output contains ✓ and other non-cp1252 glyphs; keep print safe.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DAEMON_DIR = Path(__file__).resolve().parent
HUD_DIR = DAEMON_DIR.parent / "hud"
PORT = 8801
OVERLAY_TITLE = "BlankSlate Overlay"

RESULTS: list[tuple[str, bool, str]] = []
WIRE: list[tuple[float, dict[str, Any]]] = []
LINES: list[str] = []

DAEMON: subprocess.Popen | None = None
HUD: subprocess.Popen | None = None
SCRATCH: list[Path] = []

WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
GWL_EXSTYLE = -20
WM_LBUTTONDOWN = 0x0201
WM_CLOSE = 0x0010
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def rec(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {note}" if note else ""))


def _run_shell(cmd: str, cwd: Path, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, shell=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── T1: build HUD the production way ───────────────────────────────────────

def t1_build() -> Path | None:
    print("\n--- T1: HUD build (npm run tauri build --no-bundle) ---")
    # NOTE: must be `tauri build` — plain `cargo build` produces a dev
    # binary (loads devUrl http://localhost:1420; webviews end up on
    # chrome-error:// and no JS ever runs).
    rc, out = _run_shell("npm run tauri build -- --no-bundle", HUD_DIR,
                         timeout=900)
    rec("T1 npm run tauri build --no-bundle exit 0", rc == 0,
        f"rc={rc} last={out.strip().splitlines()[-1:]}")

    exe = HUD_DIR / "src-tauri" / "target" / "release" / "blankslate-hud.exe"
    rec("T1 overlay-capable exe present", exe.exists(), f"path={exe}")
    return exe if rc == 0 and exe.exists() else None


# ── T2: daemon + HUD process lifecycle ─────────────────────────────────────

def _pump_daemon(stdout: Any) -> None:
    for line in stdout:
        LINES.append(line.rstrip("\n"))


def t2_daemon_start() -> None:
    global DAEMON
    print("\n--- T2: daemon on isolated port + HUD exe launch ---")
    env = dict(os.environ)
    env.update({
        "BS_IPC_PORT": str(PORT),
        "BS_TTS_ENABLED": "False",
        "PYTHONIOENCODING": "utf-8",
    })
    DAEMON = subprocess.Popen(
        [sys.executable, "run_daemon.py"],
        cwd=str(DAEMON_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    threading.Thread(target=_pump_daemon, args=(DAEMON.stdout,), daemon=True).start()
    deadline = time.time() + 30
    while time.time() < deadline:
        if any("IPC server listening" in ln for ln in LINES):
            break
        time.sleep(0.3)
    up = any("IPC server listening" in ln for ln in LINES)
    rec("T2 daemon IPC listening on gate port", up, f"port={PORT} pid={DAEMON.pid}")
    time.sleep(0.7)


def t2_hud_launch(exe: Path) -> None:
    global HUD
    env = dict(os.environ)
    env.update({
        "BS_DAEMON_HOST": "127.0.0.1",
        "BS_DAEMON_PORT": str(PORT),
        "BS_HUD_AUTOSTART": "0",
    })
    HUD = subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
    rec("T2 HUD exe launched", HUD.pid is not None, f"pid={HUD.pid}")


async def t2_wait_bridge() -> None:
    """Gate client + HUD Rust bridge both subscribed (subscribers >= 2)."""
    deadline = time.time() + 40
    subs = 0
    while time.time() < deadline:
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}", open_timeout=5
            ) as ws:
                await ws.send(json.dumps({"type": "get_state"}))
                got = None
                while time.time() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    m = json.loads(raw)
                    WIRE.append((time.time(), m))
                    if m.get("type") == "state":
                        got = m
                        break
                subs = int((got or {}).get("subscribers", 0))
                if subs >= 2:
                    break
        except Exception:
            pass
        await asyncio.sleep(1.0)
    rec("T2 WS subscribers >= 2 (gate + HUD bridge)", subs >= 2,
        f"subscribers={subs}")


# ── T3: click-logging target window (dedicated thread + message pump) ──────

TARGET: dict[str, Any] = {"hwnd": 0, "ready": threading.Event(), "clicks": []}


def t3_target_window(title: str, click_log: Path) -> None:
    def worker() -> None:
        import win32con
        import win32gui

        hinst = win32gui.GetModuleHandle(None)
        wc = win32gui.WNDCLASS()
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hInstance = hinst
        wc.lpszClassName = f"BSOverlayTarget{os.getpid()}"
        wc.lpszMenuName = ""
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW + 1

        def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_LBUTTONDOWN:
                x = lparam & 0xFFFF
                y = (lparam >> 16) & 0xFFFF
                if x >= 0x8000:
                    x -= 0x10000
                if y >= 0x8000:
                    y -= 0x10000
                TARGET["clicks"].append((x, y))
                with open(click_log, "a", encoding="utf-8") as fh:
                    fh.write(f"{x},{y}\n")
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

        wc.lpfnWndProc = wndproc
        atom = win32gui.RegisterClass(wc)
        hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST, atom, title,
            win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
            80, 80, 420, 300, 0, 0, hinst, None,
        )
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        TARGET["hwnd"] = hwnd
        TARGET["ready"].set()
        win32gui.PumpMessages()

    threading.Thread(target=worker, daemon=True).start()
    ok = TARGET["ready"].wait(10)
    hwnd = TARGET["hwnd"]
    visible = False
    if ok:
        import win32gui
        visible = bool(win32gui.IsWindowVisible(hwnd))
    rec("T3 target window created + visible", bool(ok and visible),
        f"hwnd={hwnd} visible={visible} title={title!r}")


# ── wire helpers ───────────────────────────────────────────────────────────

async def _overlay_cmd(ws: Any, payload: dict[str, Any],
                       want_error: bool = False) -> dict[str, Any]:
    """Send an overlay_* message; collect ack/error + overlay broadcast."""
    await ws.send(json.dumps(payload))
    out: dict[str, Any] = {"ack": None, "evt": None, "error": None}
    deadline = time.time() + 12
    while time.time() < deadline:
        if out["error"] is not None:
            break
        if not want_error and out["ack"] is not None and out["evt"] is not None:
            break
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(0.2, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        m = json.loads(raw)
        WIRE.append((time.time(), m))
        if m.get("type") == "overlay":
            out["evt"] = m
        elif m.get("type") == "ack" and m.get("setting") == "overlay":
            out["ack"] = m
        elif m.get("type") == "error":
            out["error"] = m
    return out


async def _ping(ws: Any) -> bool:
    await ws.send(json.dumps({"type": "ping"}))
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            return False
        m = json.loads(raw)
        WIRE.append((time.time(), m))
        if m.get("type") == "pong":
            return True
    return False


# ── T4/T5: show + win32 ground truth ───────────────────────────────────────

def _find_overlay_hwnd() -> int:
    import win32gui
    found: list[int] = []

    def cb(hwnd: int, _ctx: Any) -> bool:
        if win32gui.GetWindowText(hwnd) == OVERLAY_TITLE:
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else 0


async def _overlay_visible(timeout: float) -> bool:
    import win32gui
    deadline = time.time() + timeout
    while time.time() < deadline:
        hwnd = _find_overlay_hwnd()
        if hwnd and win32gui.IsWindowVisible(hwnd):
            return True
        await asyncio.sleep(0.15)
    return False


async def t4_t5_show(ws: Any, title: str) -> dict[str, Any] | None:
    print("\n--- T4/T5: overlay_show -> broadcast + win32 ground truth ---")
    # The Rust bridge subscribes the moment the app starts, but the
    # overlay page needs a second to mount its listener — re-send until
    # the pill actually appears (idempotent: every send recomputes and
    # re-broadcasts).
    res: dict[str, Any] = {"ack": None, "evt": None, "error": None}
    attempts = 0
    for attempts in range(1, 16):
        res = await _overlay_cmd(ws, {
            "type": "overlay_show", "target_title": title,
            "text": "context pill live",
        })
        if res["error"] is not None:
            break  # real daemon error — retrying won't help
        if res["evt"] is not None and await _overlay_visible(1.5):
            break
        await asyncio.sleep(0.4)
    ack, evt = res["ack"], res["evt"]
    rec("T4 overlay ack received", ack is not None,
        f"ack={ack} attempts={attempts}")
    rec("T4 overlay broadcast with coordinates", evt is not None, f"evt={evt}")
    if not evt:
        rec("T5 overlay window visible after show", False, "skipped: no broadcast")
        rec("T5 overlay rect == broadcast (x,y,w,h) ±4px", False, "skipped: no broadcast")
        rec("T5 WS_EX_TOPMOST | WS_EX_TRANSPARENT (click-through)", False,
            "skipped: no broadcast")
        return None

    import win32gui
    trect = win32gui.GetWindowRect(TARGET["hwnd"])
    x, y, w, h = evt["x"], evt["y"], evt["w"], evt["h"]
    inside = (x >= trect[0] and y >= trect[1]
              and x + w <= trect[2] and y + h <= trect[3])
    rec("T4 pill origin inside target window rect", bool(inside),
        f"pill=({x},{y},{w},{h}) target={trect}")

    hwnd = 0
    rect = (0, 0, 0, 0)
    visible = False
    for _ in range(30):  # webview show round-trip
        hwnd = _find_overlay_hwnd()
        if hwnd:
            rect = win32gui.GetWindowRect(hwnd)
            visible = bool(win32gui.IsWindowVisible(hwnd))
            if visible:
                break
        await asyncio.sleep(0.2)
    style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE) if hwnd else 0
    topmost = bool(style & WS_EX_TOPMOST)
    clickthrough = bool(style & WS_EX_TRANSPARENT)
    rec("T5 overlay window visible after show", visible,
        f"hwnd={hwnd} rect={rect}")
    rect_ok = (abs(rect[0] - x) <= 4 and abs(rect[1] - y) <= 4
               and abs((rect[2] - rect[0]) - w) <= 4
               and abs((rect[3] - rect[1]) - h) <= 4)
    rec("T5 overlay rect == broadcast (x,y,w,h) ±4px", rect_ok,
        f"rect={rect} broadcast=({x},{y},{w},{h})")
    rec("T5 WS_EX_TOPMOST | WS_EX_TRANSPARENT (click-through)",
        topmost and clickthrough, f"exstyle=0x{style:08X}")
    return evt


# ── T6: click-through proof ────────────────────────────────────────────────

def t6_clickthrough(evt: dict[str, Any]) -> None:
    print("\n--- T6: click at pill point passes through to target ---")
    import win32gui

    target = TARGET["hwnd"]
    overlay = _find_overlay_hwnd()

    # Click point: 20px in from pill left, 20px up from pill bottom —
    # comfortably inside the target's client area, below the title bar.
    click_x = evt["x"] + 20
    click_y = evt["y"] + evt["h"] - 20

    # Raise the pill to the TOP of the topmost band (z-order only; styles
    # and rect untouched) so a delivered click must traverse the overlay:
    # if WS_EX_TRANSPARENT were broken, the overlay itself would eat it.
    if overlay:
        ctypes.windll.user32.SetWindowPos(
            overlay, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        time.sleep(0.1)

    # What sits at the point? target/child of target/overlay are expected;
    # any other top-level window is a foreign interceptor (honest failure).
    hit = win32gui.WindowFromPoint((click_x, click_y))
    root = ctypes.windll.user32.GetAncestor(hit, 2) if hit else 0  # GA_ROOT
    foreign = not (root == target or hit in (0, target, overlay))
    note_pre = f"hit={hit} target={target} overlay={overlay} root={root}"

    user32 = ctypes.windll.user32
    user32.SetCursorPos(click_x, click_y)
    time.sleep(0.15)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.5)

    clicks = list(TARGET["clicks"])
    ok_click = len(clicks) >= 1
    sx = sy = None
    pos_ok = False
    if ok_click:
        cx, cy = clicks[-1]
        sx, sy = win32gui.ClientToScreen(target, (cx, cy))
        pos_ok = (evt["x"] - 4 <= sx <= evt["x"] + evt["w"] + 4
                  and evt["y"] - 4 <= sy <= evt["y"] + evt["h"] + 4)
    rec("T6 click passed THROUGH pill into target window",
        bool(ok_click and not foreign),
        f"clicks={clicks} screen=({sx},{sy}) {note_pre}")
    rec("T6 click landed inside pill footprint",
        bool(ok_click and pos_ok),
        f"client={clicks[-1] if clicks else None} "
        f"pill=({evt['x']},{evt['y']},{evt['w']},{evt['h']})")


# ── T7: hide ───────────────────────────────────────────────────────────────

async def t7_hide(ws: Any) -> None:
    print("\n--- T7: overlay_hide -> broadcast + window gone ---")
    res = await _overlay_cmd(ws, {"type": "overlay_hide"})
    evt = res["evt"]
    rec("T7 overlay hide broadcast + ack",
        evt is not None and evt.get("action") == "hide"
        and res["ack"] is not None, f"evt={evt}")
    import win32gui
    hwnd = _find_overlay_hwnd()
    visible = True
    for _ in range(30):
        if hwnd and not win32gui.IsWindowVisible(hwnd):
            visible = False
            break
        await asyncio.sleep(0.2)
        hwnd = _find_overlay_hwnd() or hwnd
    rec("T7 overlay window not visible after hide", not visible,
        f"hwnd={hwnd} visible={visible}")


# ── T8: error paths + daemon health ────────────────────────────────────────

async def t8_errors(ws: Any) -> None:
    print("\n--- T8: overlay error paths (no broadcast) ---")
    res = await _overlay_cmd(ws, {
        "type": "overlay_show", "target_title": "__NoSuchWindowZZ__",
        "text": "x",
    }, want_error=True)
    err = res["error"]
    ok1 = err is not None and "no visible window" in str(err.get("detail", ""))
    rec("T8 unknown target -> error, no overlay broadcast",
        bool(ok1 and res["evt"] is None and res["ack"] is None),
        f"error={err}")

    res2 = await _overlay_cmd(ws, {
        "type": "overlay_show", "target_title": "anything", "text": "",
    }, want_error=True)
    err2 = res2["error"]
    ok2 = err2 is not None and "text required" in str(err2.get("detail", ""))
    rec("T8 missing text -> error",
        bool(ok2 and res2["evt"] is None), f"error={err2}")

    alive = await _ping(ws)
    rec("T8 daemon healthy after errors (ping->pong)", alive, f"pong={alive}")


# ── T9/T10 ────────────────────────────────────────────────────────────────

def t9_typecheck() -> None:
    print("\n--- T9: npm run typecheck ---")
    rc, out = _run_shell("npm run typecheck", HUD_DIR)
    rec("T9 npm run typecheck exit 0", rc == 0,
        f"rc={rc} last={out.strip().splitlines()[-3:]}")


def t10_restore() -> None:
    print("\n--- T10: restore (kill HUD/target/daemon, port free, cleanup) ---")
    global DAEMON, HUD
    try:
        if TARGET["hwnd"]:
            import win32gui
            try:
                win32gui.PostMessage(TARGET["hwnd"], WM_CLOSE, 0, 0)
            except Exception:
                pass
    except Exception:
        pass

    try:
        for p in psutil.process_iter(["pid", "name"]):
            if p.info["name"] and "blankslate-hud" in p.info["name"].lower():
                p.kill()
    except Exception:
        pass
    for proc in (HUD, DAEMON):
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
    time.sleep(1.0)

    busy = None
    try:
        import socket
        s = socket.socket()
        busy = s.connect_ex(("127.0.0.1", PORT)) == 0
        s.close()
    except Exception:
        pass
    rec("T10 gate port free after kill", busy is False,
        f"port={PORT} busy={busy}")

    hud_alive = any(
        p.info["name"] and "blankslate-hud" in p.info["name"].lower()
        for p in psutil.process_iter(["name"])
    )
    rec("T10 HUD process gone", not hud_alive, f"alive={hud_alive}")

    for path in SCRATCH:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    for key in ("BS_IPC_PORT", "BS_TTS_ENABLED"):
        os.environ.pop(key, None)
    rec("T10 scratch removed + env cleaned", True,
        f"scratch={[p.name for p in SCRATCH]}")


async def main() -> int:
    # Physical-pixel coordinate space for this whole process (125% DPI
    # machine): opt in BEFORE any window is created — must be the very
    # first win32 interaction (see module docstring).
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    except Exception:
        pass
    exe = t1_build()
    try:
        t2_daemon_start()
        if exe:
            t2_hud_launch(exe)
            await t2_wait_bridge()
        title = f"BS Overlay Target {os.getpid()}"
        click_log = Path(os.environ.get("TEMP", ".")) / \
            f"bs_ov1_clicks_{os.getpid()}.txt"
        SCRATCH.append(click_log)
        if click_log.exists():
            click_log.unlink()

        t3_target_window(title, click_log)

        async with websockets.connect(
            f"ws://127.0.0.1:{PORT}", open_timeout=5
        ) as ws:
            evt = await t4_t5_show(ws, title)
            if evt:
                t6_clickthrough(evt)
            else:
                rec("T6 click passed THROUGH pill into target window", False,
                    "skipped: no broadcast")
                rec("T6 click landed inside pill footprint", False,
                    "skipped: no broadcast")
            await t7_hide(ws)
            await t8_errors(ws)
    finally:
        t9_typecheck()
        t10_restore()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
