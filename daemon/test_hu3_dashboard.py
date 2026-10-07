"""Phase 3 gate — expanded dashboard restyle (350x700 full view, visual overhaul).

T1  source: all eight expanded-view components carry the new frosted/neon
    design (StatusBar status pill border-emerald-400/30 + bg-emerald-500/10,
    Transcript bubble glow shadows + violet tool chips, Waveform
    from-emerald-400/80 to-cyan-400/90 + drop-shadow, ModeToggle
    border-emerald-400/40 + glow, SensitivityControls VAD emerald accent,
    Composer cyan focus ring + Send glow, DiagnosisCard amber band
    backdrop-blur + bordered action buttons, SandboxCard violet band +
    bordered viewer) while EVERY legacy assertion string of the original
    gates survives (dg2: App case strings + DiagnosisCard
    diagnose_confirm/dismiss/Confirm/Dismiss + diagnosis-card testids;
    fd5: App transcription_partial/tts_state cases + partial/tts testids;
    sb6: App sandbox_proposal/sandbox_result cases + SandboxCard
    sandbox_confirm/dismiss; gst4/tl3/tl5: StatusBar CameraIndicator /
    timeline / MIC strings; hu1: theme-toggle + setTheme(next) +
    getStoredTheme() + useState<Theme>). Section order in App.tsx
    (StatusBar -> Transcript -> cards -> Waveform -> ModeToggle ->
    SensitivityControls -> Composer), tauri.conf dims (250x100) and P0
    backgroundColor untouched, no fuchsia left in the dashboard files, and
    the pre-edit backups hold the OLD design (restyle proof).
T2  npm run typecheck + npm run build + npm run tauri build,
    fresh exe present.
T3  cold launch with CDP: compact view live (shell, no theme-toggle,
    Offline chip, no overflow at 250x100, dpr captured).
T4  expand via Ctrl+Alt+B: theme-toggle appears (StatusBar rendered),
    Win32 client rect == 350x700 physical (bridge.rs position_window uses
    PhysicalSize) + viewport == 350/dpr x 700/dpr, expanded DOM sections
    present (transcript empty state, Composer Send + input, Sensitivity,
    daemon chip), no document overflow, dark screenshot archived.
T5  visual at expanded size: fresh dark -> runtime data-theme flip ->
    light screenshot brighter (+30 mean) -> flipped back to dark.
T6  collapse via Ctrl+Alt+B: client rect back to 250x100 physical,
    viewport back to 250/dpr x 100/dpr, theme-toggle gone again.
T7  hygiene: process tree killed, no leftover blankslate-hud, all eight
    backups restorable, GATE_RESULT + exit.

Ground truth: disk reads, CDP Runtime.evaluate (DOM), Win32 window rects,
screenshot pixel statistics, exit codes.
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
ARTIFACTS = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud3_artifacts")
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_p3")

COMPONENTS = [
    "StatusBar.tsx",
    "Transcript.tsx",
    "Waveform.tsx",
    "ModeToggle.tsx",
    "SensitivityControls.tsx",
    "Composer.tsx",
    "DiagnosisCard.tsx",
    "SandboxCard.tsx",
]

MAIN_TITLE = "BlankSlate"
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


def _toggle_state(cdp: CDP) -> bool:
    try:
        return bool(_eval(cdp, f"!!document.querySelector('{TOGGLE}')", timeout=5))
    except Exception:
        return False


def _wait_toggle(cdp: CDP, want_present: bool, budget_s: float = 6.0) -> bool:
    deadline = time.time() + budget_s
    while time.time() < deadline:
        if _toggle_state(cdp) is want_present:
            return True
        time.sleep(0.3)
    return _toggle_state(cdp) is want_present


def _settle_rect(client: tuple[int, int], want: tuple[int, int],
                 budget_s: float = 6.0) -> tuple[int, int]:
    deadline = time.time() + budget_s
    while time.time() < deadline:
        if client == want:
            return client
        time.sleep(0.3)
        mh = _find_window(MAIN_TITLE)
        if mh is None:
            return (-1, -1)
        client = _client_rect(mh)
    return client


# ---- T1: source asserts -------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: source (8 dashboard components design + legacy survival) ---")
    texts = {
        name: (HUD_DIR / "src" / "components" / name).read_text(encoding="utf-8")
        for name in COMPONENTS
    }
    sb = texts["StatusBar.tsx"]
    tr = texts["Transcript.tsx"]
    wf = texts["Waveform.tsx"]
    mt = texts["ModeToggle.tsx"]
    sc = texts["SensitivityControls.tsx"]
    cp = texts["Composer.tsx"]
    dc = texts["DiagnosisCard.tsx"]
    sd = texts["SandboxCard.tsx"]
    app = (HUD_DIR / "src" / "App.tsx").read_text(encoding="utf-8")
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))

    rec("T1 StatusBar frosted status pill (emerald border + tint)",
        "rounded-full border" in sb and "border-emerald-400/30" in sb
        and "bg-emerald-500/10" in sb and "border-rose-400/30" in sb,
        "connected/disconnected pill classes")
    rec("T1 StatusBar gst4/tl3/tl5 strings survive",
        "import CameraIndicator" in sb and "<CameraIndicator" in sb
        and "cameraActive" in sb and "timeline?.active" in sb
        and "REC" in sb and "Timeline" in sb and "timeline?.audio" in sb
        and "timeline-audio-indicator" in sb and "MIC" in sb,
        "gst4 T5 + tl3 T4 + tl5 T4 StatusBar asserts")
    rec("T1 StatusBar hu1 theme strings survive",
        'data-testid="theme-toggle"' in sb and "setTheme(next)" in sb
        and "getStoredTheme()" in sb and "useState<Theme>" in sb,
        "hu1 T1 StatusBar asserts")
    rec("T1 Transcript emerald/cyan bubble glows + violet tool chips",
        "border-emerald-400/30" in tr and "border-cyan-400/25" in tr
        and "shadow-[0_0_14px_rgba(52,211,153,0.10)]" in tr
        and "bg-violet-500/10" in tr and "fuchsia" not in tr,
        "glow shadows + violet chips, fuchsia gone")
    rec("T1 Waveform neon emerald -> cyan gradient + glow",
        "from-emerald-400/80" in wf and "to-cyan-400/90" in wf
        and "drop-shadow-[0_0_3px_rgba(34,211,238,0.45)]" in wf
        and "fuchsia" not in wf,
        "P2-parity bars")
    rec("T1 ModeToggle frosted chips + emerald glow",
        "border-emerald-400/40" in mt and "border-amber-400/40" in mt
        and "shadow-[0_0_8px_rgba(52,211,153,0.12)]" in mt,
        "online/offline chip classes")
    rec("T1 SensitivityControls VAD emerald accent, cyan hover",
        "hover:border-cyan-400/30" in sc and "accent-emerald-400" in sc
        and "text-emerald-300" in sc and "fuchsia" not in sc,
        "collapsed hover + VAD slider")
    rec("T1 Composer cyan focus ring + Send glow",
        "focus:border-cyan-400/60" in cp
        and "shadow-[0_0_10px_rgba(34,211,238,0.35)]" in cp
        and "border-t border-white/10" in cp,
        "input focus + neon send")
    rec("T1 DiagnosisCard frosted amber band + bordered actions",
        "border-amber-400/30" in dc and "backdrop-blur-sm" in dc
        and "border-emerald-400/40" in dc and "border-rose-400/40" in dc,
        "band + confirm/dismiss buttons")
    rec("T1 dg2 DiagnosisCard strings survive",
        'data-testid="diagnosis-card"' in dc
        and 'data-testid="diagnosis-confirm"' in dc
        and 'data-testid="diagnosis-dismiss"' in dc
        and "diagnose_confirm" in dc and "diagnose_dismiss" in dc
        and "Confirm" in dc and "Dismiss" in dc,
        "dg2 T8 DiagnosisCard asserts")
    rec("T1 SandboxCard frosted violet band + bordered viewer",
        "border-violet-400/30" in sd and "backdrop-blur-sm" in sd
        and "border-white/10 bg-black/50" in sd
        and "border-emerald-400/40" in sd and "border-rose-400/40" in sd,
        "band + buttons + script viewer")
    rec("T1 sb6 SandboxCard strings survive",
        'data-testid="sandbox-card"' in sd
        and 'data-testid="sandbox-confirm"' in sd
        and 'data-testid="sandbox-dismiss"' in sd
        and 'data-testid="sandbox-script"' in sd
        and "sandbox_confirm" in sd and "sandbox_dismiss" in sd
        and "Confirm" in sd and "Dismiss" in sd,
        "sb6 T6b SandboxCard asserts")
    rec("T1 dg2 App.tsx case/testid strings survive",
        'case "monitor_alert"' in app and 'category === "diagnosis"' in app
        and 'data-testid="diagnosis-pill"' in app
        and "awaiting_confirmation" in app and "<DiagnosisCard" in app,
        "dg2 T8 App.tsx asserts")
    rec("T1 fd5 App.tsx case/testid strings survive",
        'case "transcription_partial"' in app and 'case "tts_state"' in app
        and 'data-testid="partial-pill"' in app and 'data-testid="tts-pill"' in app
        and 'data-testid="partial-line"' in app and 'data-testid="tts-state"' in app,
        "fd5 T5 App.tsx asserts")
    rec("T1 sb6 App.tsx case strings survive",
        'case "sandbox_proposal"' in app and 'case "sandbox_result"' in app,
        "sb6 T6b App.tsx asserts")
    rec("T1 gst4/tl3 App.tsx strings survive",
        'case "camera_state"' in app and "setCameraActive" in app
        and "cameraActive={cameraActive}" in app
        and "timeline: evt.timeline" in app,
        "gst4 T5 + tl3 T4 App.tsx asserts")

    order = [
        "<StatusBar ", "<Transcript ", "<Waveform ",
        "<ModeToggle ", "<SensitivityControls ", "<Composer",
    ]
    positions: list[int] = []
    for needle in order:
        i = app.find(needle)
        positions.append(i)
    rec("T1 App.tsx section order (StatusBar->Transcript->Waveform->"
        "ModeToggle->Sensitivity->Composer)",
        all(i >= 0 for i in positions)
        and positions == sorted(positions)
        and app.find("<DiagnosisCard ") > app.find("<Transcript ")
        and app.find("<SandboxCard ") > app.find("<DiagnosisCard "),
        f"positions={positions}")

    win = conf["app"]["windows"][0]
    rec("T1 tauri.conf window dims unchanged (250x100)",
        win.get("width") == 250 and win.get("height") == 100,
        f"width={win.get('width')} height={win.get('height')}")
    rec("T1 P0 backgroundColor invariant intact",
        win.get("backgroundColor") == "#0a0a16",
        f"backgroundColor={win.get('backgroundColor')}")

    backup_markers = {
        "StatusBar.tsx": "border-b border-white/5",
        "Transcript.tsx": "border-fuchsia-400/20",
        "Waveform.tsx": "from-cyan-500/70 to-fuchsia-400/80",
        "ModeToggle.tsx": "text-emerald-400 border-emerald-500/30",
        "SensitivityControls.tsx": "accent-fuchsia-400",
        "Composer.tsx": "bg-cyan-500/90",
        "DiagnosisCard.tsx": "bg-amber-500/5 px-4 py-2 text-xs",
        "SandboxCard.tsx": "bg-violet-500/5 px-4 py-2 text-xs",
    }
    missing = [
        f for f, marker in backup_markers.items()
        if not (BACKUP_DIR / f"{f}.bak").exists()
        or marker not in (BACKUP_DIR / f"{f}.bak").read_text(encoding="utf-8")
    ]
    stale = [
        f for f, marker in backup_markers.items()
        if marker in texts[f] and f in ("Transcript.tsx", "Waveform.tsx",
                                        "SensitivityControls.tsx")
    ]
    rec("T1 pre-edit backups hold OLD design (restyle proof, 8 files)",
        not missing, f"missing/old-marker-mismatch={missing}")
    rec("T1 old gradient/fuchsia design gone from live sources",
        not stale, f"stale={stale}")


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


# ---- T3: compact regression ----------------------------------------------

def t3_compact_dom() -> CDP | None:
    print("\n--- T3: cold launch, compact view regression ---")
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
        " overflowY: document.body.scrollHeight - window.innerHeight})" % TOGGLE,
    )
    rec("T3 compact shell rendered (no expand yet)",
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


# ---- T4: expand ----------------------------------------------------------

def t4_expand(main: CDP | None) -> None:
    print("\n--- T4: expand (Ctrl+Alt+B) -> 350x700 footprint + DOM ---")
    if main is None:
        rec("T4 expand skipped (no main conn)", False, "launch failed")
        return
    import pyautogui

    pyautogui.hotkey("ctrl", "alt", "b")
    opened = _wait_toggle(main, True)
    rec("T4 expand shows StatusBar theme-toggle", opened,
        f"toggle_present={_toggle_state(main)}")

    mh = _find_window(MAIN_TITLE)
    if mh is None:
        rec("T4 main window found by Win32 title", False, f"title={MAIN_TITLE!r}")
        return
    client = _settle_rect(_client_rect(mh), (350, 700))
    rec("T4 client rect == 350x700 physical (position_window PhysicalSize)",
        client == (350, 700), f"client={client[0]}x{client[1]}")

    dpr = float(_eval_json(main, "String(window.devicePixelRatio)") or 1)
    vp = _eval_json(
        main,
        "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
    )
    exp_vp = (round(350 / dpr), round(700 / dpr))
    rec("T4 viewport == 350/dpr x 700/dpr",
        (int(vp.get("w") or 0), int(vp.get("h") or 0)) == exp_vp,
        f"viewport={vp.get('w')}x{vp.get('h')} expected={exp_vp[0]}x{exp_vp[1]} dpr={dpr}")

    st = _eval_json(
        main,
        "JSON.stringify({bodyText: document.body.innerText,"
        " input: !!document.querySelector('input[placeholder]'),"
        " send: Array.from(document.querySelectorAll('button'))"
        ".some(b => b.textContent.trim() === 'Send'),"
        " overflowX: document.body.scrollWidth - window.innerWidth,"
        " overflowY: document.body.scrollHeight - window.innerHeight})",
    )
    body = str(st.get("bodyText", "")).lower()
    rec("T4 expanded transcript empty state present",
        "awaiting input" in body, f"body[:80]={str(st.get('bodyText'))[:80]!r}")
    rec("T4 expanded Composer + Sensitivity rendered",
        bool(st.get("input")) and bool(st.get("send"))
        and "sensitivity" in body,
        f"input={st.get('input')} send={st.get('send')}")
    rec("T4 expanded StatusBar daemon chip rendered",
        "daemon" in body and "offline" in body,
        "status pill text present")
    rec("T4 no document overflow at 350x700",
        int(st.get("overflowX") or 0) <= 0 and int(st.get("overflowY") or 0) <= 0,
        f"dx={st.get('overflowX')} dy={st.get('overflowY')}")

    dark_mean, dark_std = _shot(main, "expanded_dark.png")
    rec("T4 expanded dark screenshot archived",
        dark_mean < 80, f"mean={dark_mean} std={dark_std} dir={ARTIFACTS}")


# ---- T5: visual dark/light ----------------------------------------------

def t5_visual(main: CDP | None) -> None:
    print("\n--- T5: visual at expanded size (dark -> light -> dark) ---")
    if main is None:
        rec("T5 visual skipped (no main conn)", False, "launch failed")
        return

    st = _eval_json(
        main,
        "JSON.stringify({ls: localStorage.getItem('bs-theme'),"
        " theme: document.documentElement.getAttribute('data-theme')})",
    )
    rec("T5 dark state at expanded size",
        st.get("theme") == "dark" and st.get("ls") in (None, "dark"),
        f"state={st}")

    dark_mean, _ = _shot(main, "expanded_dark_check.png")
    rec("T5 dark screenshot baseline archived",
        dark_mean < 80, f"dark_mean={dark_mean}")

    _eval(main, "document.documentElement.setAttribute('data-theme', 'light'); 'x'")
    time.sleep(0.4)
    light_state = _eval_json(
        main,
        "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
        " bodyBg: getComputedStyle(document.body).backgroundColor})",
    )
    light_mean, light_std = _shot(main, "expanded_light.png")
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


# ---- T6: collapse ---------------------------------------------------------

def t6_collapse(main: CDP | None) -> None:
    print("\n--- T6: collapse (Ctrl+Alt+B) -> 250x100 restored ---")
    if main is None:
        rec("T6 collapse skipped (no main conn)", False, "launch failed")
        return
    import pyautogui

    pyautogui.hotkey("ctrl", "alt", "b")
    closed = _wait_toggle(main, False)
    rec("T6 collapse hides theme-toggle again", closed,
        f"toggle_present={_toggle_state(main)}")

    mh = _find_window(MAIN_TITLE)
    if mh is None:
        rec("T6 main window found by Win32 title", False, f"title={MAIN_TITLE!r}")
        return
    client = _settle_rect(_client_rect(mh), (250, 100))
    rec("T6 client rect restored to 250x100 physical",
        client == (250, 100), f"client={client[0]}x{client[1]}")
    dpr = float(_eval_json(main, "String(window.devicePixelRatio)") or 1)
    vp = _eval_json(
        main,
        "JSON.stringify({w: window.innerWidth, h: window.innerHeight})",
    )
    exp_vp = (round(250 / dpr), round(100 / dpr))
    rec("T6 viewport restored to 250/dpr x 100/dpr",
        (int(vp.get("w") or 0), int(vp.get("h") or 0)) == exp_vp,
        f"viewport={vp.get('w')}x{vp.get('h')} expected={exp_vp[0]}x{exp_vp[1]}")


# ---- T7: hygiene ----------------------------------------------------------

def t7_hygiene() -> None:
    print("\n--- T7: process tree + backup hygiene ---")
    _kill_tree(PROC)
    time.sleep(0.4)
    killed = _kill_leftovers()
    time.sleep(0.4)
    remaining = _hud_instances()
    rec("T7 no leftover blankslate-hud process", remaining == 0,
        f"killed={killed} remaining={remaining}")

    not_ok = [
        f for f in COMPONENTS
        if not (BACKUP_DIR / f"{f}.bak").exists()
        or (BACKUP_DIR / f"{f}.bak").stat().st_size < 500
    ]
    rec("T7 all eight pre-edit backups present and restorable",
        not not_ok, f"bad={not_ok}")


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
        t4_expand(conn)
        t5_visual(conn)
        t6_collapse(conn)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        t7_hygiene()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
        print("RESTORE (only if sources must revert):")
        for f in COMPONENTS:
            print(f'  Copy-Item "{BACKUP_DIR}\\{f}.bak" '
                  f'"{HUD_DIR}\\src\\components\\{f}" -Force')
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
