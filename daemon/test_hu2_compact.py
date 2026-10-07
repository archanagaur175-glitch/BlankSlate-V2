"""Phase 2 gate — CompactWidget restyle (250x100 pill, visual overhaul).

T1  source: CompactWidget carries the new frosted/neon design (status
    chip border-emerald-400/30 + bg-emerald-500/10, waveform gradient
    from-emerald-400/80 to-cyan-400/90, command accent
    border-cyan-400/60) while EVERY legacy assertion string survives
    (gst4: import CameraIndicator / <CameraIndicator / cameraActive;
    tl3: timeline?.active / REC / Timeline; tl5: timeline?.audio /
    timeline-audio-indicator / MIC; lastCommand), theme code stays out
    of the compact view (no theme-toggle / setTheme), App.tsx compact
    pills (diagnosis/sandbox/partial/tts testids) untouched, and
    tauri.conf window dims (250x100) + P0 backgroundColor untouched.
    The pre-edit backup must contain the OLD design (fuchsia gradient)
    and must not match the current file (restyle proof).
T2  npm run typecheck + npm run build + npm run tauri build,
    fresh exe present.
T3  cold launch with CDP: compact view is the live state — .hud-root
    shell present, CompactWidget rendered ("Offline" chip; no daemon),
    NO theme-toggle in compact (toggle lives in expanded StatusBar
    only), no layout overflow (scroll size == viewport), devicePixelRatio
    captured for physical-pixel math.
T4  Win32 footprint (per-monitor-v2 aware): main window "BlankSlate"
    CLIENT rect == 250x100 physical pixels (probe-verified ground
    truth: config window is created before WebView2's DPI bootstrap,
    so it is NOT dpr-scaled; GetWindowRect adds the thickframe's
    invisible 9px/5px borders) + webview viewport == round(250/dpr) x
    round(100/dpr); overlay window (built later, dpr-scaled) ==
    round(240*dpr) x round(56*dpr) — window dimensions must not change
    (phase constraint).
T5  visual: fresh dark state -> dark screenshot; runtime data-theme
    flip -> light screenshot must be brighter (+30 mean); flip back to
    dark verified (profile left dark for downstream gates).
T6  hygiene: process tree killed, no leftover blankslate-hud, backup
    restorable, GATE_RESULT + exit.

Ground truth: disk reads, CDP Runtime.evaluate (DOM + computed styles),
Win32 window rects, screenshot pixel statistics, exit codes.
"""

from __future__ import annotations

import base64
import json
import os
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
TAURI_CONF = HUD_DIR / "src-tauri" / "tauri.conf.json"
ARTIFACTS = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud2_artifacts")
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_p2")

MAIN_TITLE = "BlankSlate"
OVERLAY_TITLE = "BlankSlate Overlay"
DARK_BODY_BG = "rgba(10, 10, 22, 0.9)"
LIGHT_BODY_BG = "rgba(241, 245, 249, 0.95)"
TOGGLE = '[data-testid="theme-toggle"]'

RESULTS: list[tuple[str, bool, str]] = []
PROC: subprocess.Popen | None = None
CDP_PORT = 0


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


def _hud_instances() -> int:
    try:
        import psutil

        n = 0
        for p in psutil.process_iter(["name"]):
            if "blankslate-hud" in (p.info.get("name") or "").lower():
                n += 1
        return n
    except Exception:
        return 0


def _kill_leftovers() -> int:
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


def _eval_json(cdp: CDP, expr: str, timeout: float = 10.0) -> dict:
    return json.loads(_eval(cdp, expr, timeout=timeout))


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


def _attach_main(timeout_s: float = 10.0) -> CDP | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for tgt in _list_pages(CDP_PORT):
            try:
                c = _connect(CDP_PORT, tgt)
            except Exception:
                continue
            for _ in range(8):
                try:
                    if _eval(c, "!!document.querySelector('.hud-root')", timeout=5):
                        return c
                except Exception:
                    pass
                time.sleep(0.3)
            c.close()
        time.sleep(0.4)
    return None


def _launch() -> subprocess.Popen:
    env = dict(os.environ)
    env["BS_HUD_AUTOSTART"] = "0"
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        f"--remote-debugging-port={CDP_PORT}"
    )
    return subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env)


def _kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    kids: list[int] = []
    try:
        import psutil

        kids = [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=6)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for pid in kids:
        try:
            import psutil

            psutil.Process(pid).kill()
        except Exception:
            pass


def _win32_dpi_aware() -> None:
    import ctypes

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def _find_window(title: str) -> int | None:
    import win32gui

    found: list[int] = []

    def cb(h: int, _ctx: Any) -> bool:
        if win32gui.GetWindowText(h) == title:
            found.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def _rect(h: int) -> tuple[int, int]:
    import win32gui

    l, t, r, b = win32gui.GetWindowRect(h)
    return (r - l, b - t)


def _client_rect(h: int) -> tuple[int, int]:
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rc = RECT()
    ctypes.windll.user32.GetClientRect(ctypes.c_void_p(h), ctypes.byref(rc))
    return (rc.right - rc.left, rc.bottom - rc.top)


def _shot(cdp: CDP, name: str) -> tuple[float, float]:
    from PIL import Image, ImageStat

    r = cdp.cmd("Page.captureScreenshot", {"format": "png"})
    path = ARTIFACTS / name
    path.write_bytes(base64.b64decode(r["data"]))
    st = ImageStat.Stat(Image.open(path).convert("L"))
    return round(float(st.mean[0]), 1), round(float(st.stddev[0]), 1)


# ---- T1: source asserts -------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: source (CompactWidget design + legacy survival + invariants) ---")
    cw = (HUD_DIR / "src" / "components" / "CompactWidget.tsx").read_text(encoding="utf-8")
    app = (HUD_DIR / "src" / "App.tsx").read_text(encoding="utf-8")
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    bak = BACKUP_DIR / "CompactWidget.tsx.bak"
    bak_txt = bak.read_text(encoding="utf-8") if bak.exists() else ""

    rec("T1 new design: frosted status chip (emerald border + tint)",
        "border-emerald-400/30" in cw and "bg-emerald-500/10" in cw
        and "rounded-full border" in cw,
        "status chip classes present")
    rec("T1 new design: waveform neon emerald -> cyan gradient + glow",
        "from-emerald-400/80" in cw and "to-cyan-400/90" in cw
        and "drop-shadow-[0_0_3px_rgba(34,211,238,0.45)]" in cw,
        "bar gradient + drop-shadow")
    rec("T1 new design: command line cyan hairline accent (frosted bg)",
        "border-cyan-400/60" in cw and "bg-white/5" in cw
        and "rounded-r" in cw,
        "accent border classes")
    rec("T1 gst4 strings survive (CameraIndicator import/usage/cameraActive)",
        "import CameraIndicator" in cw and "<CameraIndicator" in cw
        and "cameraActive" in cw,
        "gst4 T5 CompactWidget asserts")
    rec("T1 tl3 strings survive (timeline?.active / REC / Timeline)",
        "timeline?.active" in cw and "REC" in cw and "Timeline" in cw,
        "tl3 T4 CompactWidget asserts")
    rec("T1 tl5 strings survive (timeline?.audio / audio-indicator / MIC)",
        "timeline?.audio" in cw and "timeline-audio-indicator" in cw
        and "MIC" in cw,
        "tl5 T4 CompactWidget asserts")
    rec("T1 command rendering survives (lastCommand + truncate)",
        "lastCommand" in cw and "truncate" in cw,
        "compact command line intact")
    rec("T1 no theme code in compact view",
        "theme-toggle" not in cw and "setTheme" not in cw
        and "getStoredTheme" not in cw,
        "toggle lives in expanded StatusBar only")
    rec("T1 App.tsx compact pills untouched (fd5/dg2 testids)",
        'data-testid="diagnosis-pill"' in app and 'data-testid="sandbox-pill"' in app
        and 'data-testid="partial-pill"' in app and 'data-testid="tts-pill"' in app,
        "all four compact pills present")
    win = conf["app"]["windows"][0]
    rec("T1 tauri.conf window dims unchanged (250x100)",
        win.get("width") == 250 and win.get("height") == 100,
        f"width={win.get('width')} height={win.get('height')}")
    rec("T1 P0 backgroundColor invariant intact",
        win.get("backgroundColor") == "#0a0a16",
        f"backgroundColor={win.get('backgroundColor')}")
    rec("T1 pre-edit backup holds OLD design (restyle proof)",
        bak.exists() and "from-cyan-500/70 to-fuchsia-400/80" in bak_txt
        and "from-cyan-500/70 to-fuchsia-400/80" not in cw,
        f"backup={bak} ({len(bak_txt)}B)")


# ---- T2: builds ---------------------------------------------------------

def t2_build() -> None:
    print("\n--- T2: typecheck + vite build + fresh exe ---")
    killed = _kill_leftovers()
    if killed:
        time.sleep(0.5)
    rc, out = _run("npm run typecheck", HUD_DIR, 300)
    rec("T2 npm run typecheck exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-2:]}")
    rc, out = _run("npm run build", HUD_DIR, 600)
    rec("T2 npm run build exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-2:]}")
    rc, out = _run("npm run tauri build -- --no-bundle", HUD_DIR, 900)
    rec("T2 npm run tauri build -- --no-bundle exit 0", rc == 0,
        f"rc={rc} tail={out.strip().splitlines()[-2:]}")
    rec("T2 fresh exe present", EXE.exists(), f"path={EXE}")


# ---- T3: compact view DOM -----------------------------------------------

def t3_compact_dom() -> CDP | None:
    print("\n--- T3: cold launch, compact view DOM ---")
    global PROC
    _kill_tree(PROC)
    PROC = _launch()
    main = _attach_main()
    if main is None:
        rec("T3 CDP attached to main webview", False, "no .hud-root target")
        return None
    rec("T3 CDP attached to main webview", True, "target classified via .hud-root")

    st = _eval_json(
        main,
        "JSON.stringify({shell: !!document.querySelector('.hud-root'),"
        " toggle: !!document.querySelector('%s'),"
        " bodyText: document.body.innerText,"
        " dpr: window.devicePixelRatio,"
        " overflowX: document.body.scrollWidth - window.innerWidth,"
        " overflowY: document.body.scrollHeight - window.innerHeight,"
        " bodyBg: getComputedStyle(document.body).backgroundColor})" % TOGGLE,
    )
    rec("T3 compact shell rendered (.hud-root, no expand)",
        bool(st.get("shell")) and st.get("toggle") is False,
        f"shell={st.get('shell')} toggle={st.get('toggle')}")
    rec("T3 CompactWidget chip rendered (daemon absent -> Offline)",
        "offline" in str(st.get("bodyText", "")).lower(),
        f"text={str(st.get('bodyText'))[:60]!r}")
    rec("T3 no layout overflow at 250x100",
        int(st.get("overflowX") or 0) <= 0 and int(st.get("overflowY") or 0) <= 0,
        f"dx={st.get('overflowX')} dy={st.get('overflowY')}")
    rec("T3 devicePixelRatio captured for physical math",
        isinstance(st.get("dpr"), (int, float)) and float(st.get("dpr")) > 0,
        f"dpr={st.get('dpr')}")
    return main


# ---- T4: Win32 footprint -------------------------------------------------

def t4_footprint(main: CDP | None) -> None:
    print("\n--- T4: Win32 footprint (250x100 + overlay dimensions guard) ---")
    if main is None:
        rec("T4 footprint skipped (no main conn)", False, "launch failed")
        return
    dpr = float(_eval_json(main, "String(window.devicePixelRatio)") or 1)
    exp_main = (round(250 * dpr), round(100 * dpr))
    exp_ov = (round(240 * dpr), round(56 * dpr))

    mh = _find_window(MAIN_TITLE)
    if mh is None:
        rec("T4 main window found by Win32 title", False, f"title={MAIN_TITLE!r}")
        return
    client = _client_rect(mh)
    rec("T4 main window client rect == 250x100 physical (probe truth)",
        client == (250, 100),
        f"client={client[0]}x{client[1]} dpr={dpr} "
        f"winrect={_rect(mh)[0]}x{_rect(mh)[1]}")
    vp = _eval_json(
        main,
        "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
    )
    exp_vp = (round(250 / dpr), round(100 / dpr))
    rec("T4 webview viewport == 250/dpr x 100/dpr (content fits footprint)",
        (int(vp.get("w") or 0), int(vp.get("h") or 0)) == exp_vp,
        f"viewport={vp.get('w')}x{vp.get('h')} expected={exp_vp[0]}x{exp_vp[1]}")

    oh = _find_window(OVERLAY_TITLE)
    if oh is None:
        rec("T4 overlay window exists (created at startup)", False,
            f"title={OVERLAY_TITLE!r}")
        return
    orect = _rect(oh)
    rec("T4 overlay window rect == 240x56 (dpr-scaled, unchanged)",
        orect == exp_ov,
        f"rect={orect[0]}x{orect[1]} expected={exp_ov[0]}x{exp_ov[1]} dpr={dpr}")


# ---- T5: visual dark/light ----------------------------------------------

def t5_visual(main: CDP | None) -> None:
    print("\n--- T5: visual (fresh dark -> runtime light flip -> back) ---")
    if main is None:
        rec("T5 visual skipped (no main conn)", False, "launch failed")
        return

    st = _eval_json(
        main,
        "JSON.stringify({ls: localStorage.getItem('bs-theme'),"
        " theme: document.documentElement.getAttribute('data-theme')})",
    )
    if st.get("ls") not in (None, "dark"):
        _eval(main, "localStorage.removeItem('bs-theme'); location.reload(); 'x'")
        time.sleep(1.5)
        st = _eval_json(
            main,
            "JSON.stringify({ls: localStorage.getItem('bs-theme'),"
            " theme: document.documentElement.getAttribute('data-theme')})",
        )
    rec("T5 fresh dark state (theme + storage)",
        st.get("theme") == "dark" and st.get("ls") in (None, "dark"),
        f"state={st}")

    dark_mean, dark_std = _shot(main, "compact_dark.png")
    rec("T5 dark compact screenshot archived",
        dark_mean < 80, f"mean={dark_mean} std={dark_std} dir={ARTIFACTS}")

    _eval(main, "document.documentElement.setAttribute('data-theme', 'light'); 'x'")
    time.sleep(0.4)
    light_state = _eval_json(
        main,
        "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
        " bodyBg: getComputedStyle(document.body).backgroundColor})",
    )
    light_mean, light_std = _shot(main, "compact_light.png")
    rec("T5 runtime flip -> light repaint (body bg token swapped)",
        light_state.get("theme") == "light"
        and light_state.get("bodyBg") == LIGHT_BODY_BG,
        f"state={light_state}")
    rec("T5 light screenshot visibly brighter (+30 mean)",
        light_mean > dark_mean + 30,
        f"light={light_mean} dark={dark_mean}")

    _eval(main, "document.documentElement.setAttribute('data-theme', 'dark'); 'x'")
    time.sleep(0.3)
    back = _eval_json(
        main,
        "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
        " bodyBg: getComputedStyle(document.body).backgroundColor})",
    )
    rec("T5 flipped back to dark (profile left dark for downstream gates)",
        back.get("theme") == "dark" and back.get("bodyBg") == DARK_BODY_BG,
        f"state={back}")


# ---- T6: hygiene ---------------------------------------------------------

def t6_hygiene() -> None:
    print("\n--- T6: process tree + backup hygiene ---")
    _kill_tree(PROC)
    time.sleep(0.4)
    killed = _kill_leftovers()
    time.sleep(0.4)
    remaining = _hud_instances()
    rec("T6 no leftover blankslate-hud process", remaining == 0,
        f"killed={killed} remaining={remaining}")

    bak = BACKUP_DIR / "CompactWidget.tsx.bak"
    rec("T6 pre-edit backup present and restorable",
        bak.exists() and bak.stat().st_size > 1000,
        f"size={bak.stat().st_size if bak.exists() else 0}")


def main() -> int:
    global CDP_PORT
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    CDP_PORT = free_port()
    _win32_dpi_aware()
    conn: CDP | None = None
    try:
        t1_source()
        t2_build()
        conn = t3_compact_dom()
        t4_footprint(conn)
        t5_visual(conn)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        t6_hygiene()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
        print("RESTORE (only if sources must revert):")
        print(f'  Copy-Item "{BACKUP_DIR}\\CompactWidget.tsx.bak" '
              f'"{HUD_DIR}\\src\\components\\CompactWidget.tsx" -Force')
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
