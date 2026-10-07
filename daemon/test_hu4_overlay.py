"""Phase 4 gate — overlay pill restyle (`.overlay-pill` CSS only, neon frosted).

T1  source: index.css `.overlay-pill` carries the new frosted/neon design
    (background-color token kept + background-image glass highlight via
    color-mix white, emerald outline ring, cyan outer glow, weight 600 +
    letter-spacing) while every hud0 invariant survives (import order,
    @theme inline mappings, the 13 indexed var(--bs-*) substrings, zero
    hardcoded sentinels), structural pill props unchanged (flex/100%/16px
    padding/radius/ellipsis/13px), OverlayWindow.tsx untouched (testids +
    PhysicalSize 240/56), tauri.conf dims + P0 backgroundColor untouched.
    The pre-edit backup holds the OLD rule (no background-image/glow).
T2  npm run typecheck + npm run build + npm run tauri build,
    fresh exe present.
T3  cold launch, attach BOTH webviews (main .hud-root + overlay
    .overlay-pill): dark pill backgroundColor byte-identical to the hud0/hu1
    T0 value (token-driven), border/color tokens intact, NEW design live
    (gradient present, emerald outline, cyan glow in box-shadow, weight
    600, letter-spacing), dark pill screenshot archived.
T4  overlay show via Rust event plugin (daemon_message, w=240 h=56
    physical — daemon broadcast units): window visible, Win32 rect == broadcast
    +-4px (ov1 semantics), pill text rendered == sent text.
T5  live theme-sync: expand + real toggle click -> main light -> overlay
    pill repaints <=1.5s (backgroundColor -> light token, outline color
    changes), light pill brighter (+30 mean) -> toggle back -> dark values
    restored byte-identically (profile left dark).
T6  overlay hide via plugin event -> window not visible (ov1 T7 parity).
T7  hygiene: process tree killed, no leftover blankslate-hud, backup
    restorable, GATE_RESULT + exit.

Ground truth: disk reads, CDP Runtime.evaluate (both webviews), Win32
window rects/visibility, screenshot pixel statistics, exit codes.
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
ARTIFACTS = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud4_artifacts")
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_p4")

MAIN_TITLE = "BlankSlate"
OVERLAY_TITLE = "BlankSlate Overlay"
DARK_BODY_BG = "rgba(10, 10, 22, 0.9)"
DARK_PILL_BG = "rgba(10, 10, 22, 0.88)"
LIGHT_PILL_BG = "rgba(248, 250, 252, 0.92)"
TOGGLE = '[data-testid="theme-toggle"]'
SENTINELS_ABSENT = [
    "rgba(10, 10, 22",
    "#e0e0e0",
    "rgba(120, 180, 255",
    "#cfe8ff",
    "rgba(34, 211, 238",
]
INDEXED_VARS = [
    "var(--bs-bg-app)", "var(--bs-text-primary)",
    "var(--bs-glass-from)", "var(--bs-glass-to)",
    "var(--bs-border-subtle)", "var(--bs-radius-card)",
    "var(--bs-glow-inset)", "var(--bs-scrollbar-thumb)",
    "var(--bs-pill-bg)", "var(--bs-pill-border)",
    "var(--bs-pill-text)", "var(--bs-shadow-pill)",
    "var(--bs-radius-pill)",
]

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


def _classify(cdp: CDP, kind: str) -> bool:
    sel = ".hud-root" if kind == "main" else ".overlay-pill"
    try:
        return bool(_eval(cdp, f"!!document.querySelector('{sel}')", timeout=5))
    except Exception:
        return False


def _attach(kind: str, timeout_s: float = 12.0) -> CDP | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for tgt in _list_pages(CDP_PORT):
            try:
                c = _connect(CDP_PORT, tgt)
            except Exception:
                continue
            for _ in range(8):
                if _classify(c, kind):
                    return c
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


def _win32_rect(h: int) -> tuple[int, int]:
    import win32gui

    l, t, r, b = win32gui.GetWindowRect(h)
    return (r - l, b - t)


def _win32_visible(h: int) -> bool:
    import win32gui

    return bool(win32gui.IsWindowVisible(h))


def _shot(cdp: CDP, name: str) -> tuple[float, float]:
    from PIL import Image, ImageStat

    r = cdp.cmd("Page.captureScreenshot", {"format": "png"})
    path = ARTIFACTS / name
    path.write_bytes(base64.b64decode(r["data"]))
    st = ImageStat.Stat(Image.open(path).convert("L"))
    return round(float(st.mean[0]), 1), round(float(st.stddev[0]), 1)


def _emit_overlay(main: CDP, action: str, text: str = "hu4 pill") -> str:
    payload = (
        "{type:'overlay', action:'%s', x:400, y:400, w:240, h:56, "
        "text:'%s'}" % (action, text)
    )
    return str(
        _eval(
            main,
            "(async () => { try {"
            " await window.__TAURI_INTERNALS__.invoke('plugin:event|emit', {"
            " event: 'daemon_message',"
            f" payload: JSON.stringify({payload}) }});"
            " return 'emitted'; } catch (e) { return 'err:' + String(e); } })()",
            timeout=10,
        )
    )


# ---- T1: source asserts -------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: source (index.css pill design + hud0 invariants) ---")
    css = (HUD_DIR / "src" / "index.css").read_text(encoding="utf-8")
    ow = (HUD_DIR / "src" / "components" / "OverlayWindow.tsx").read_text(
        encoding="utf-8"
    )
    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    bak = BACKUP_DIR / "index.css.bak"
    bak_txt = bak.read_text(encoding="utf-8") if bak.exists() else ""

    rec("T1 pill keeps token background-color (backgroundColor baseline intact)",
        "background-color: var(--bs-pill-bg);" in css,
        "backgroundColor stays token-driven")
    rec("T1 pill new glass highlight gradient (background-image color-mix)",
        "background-image: linear-gradient(" in css
        and "color-mix(in oklab, var(--bs-white) 6%, transparent)" in css,
        "top glass gradient present")
    rec("T1 pill emerald neon outline ring",
        "outline: 1px solid color-mix(in oklab, var(--bs-emerald-400) 35%, transparent)" in css
        and "outline-offset: -1px" in css,
        "emerald ring inside border")
    rec("T1 pill cyan outer glow + inset highlight",
        "0 0 16px -6px var(--bs-cyan-400)" in css
        and "inset 0 1px 0 color-mix(in oklab, var(--bs-white) 8%, transparent)" in css,
        "box-shadow glow layers")
    rec("T1 pill typography upgrade (weight + letter-spacing)",
        "font-weight: 600" in css and "letter-spacing: 0.06em" in css,
        "600 / 0.06em")
    rec("T1 pill structural props unchanged (flex/100%/padding/ellipsis/13px)",
        all(s in css for s in [
            "display: flex;", "width: 100%;", "height: 100%;",
            "padding: 0 16px;", "text-overflow: ellipsis;",
            "white-space: nowrap;", "font-size: 13px;",
            "border-radius: var(--bs-radius-pill);",
        ]),
        "layout-critical declarations intact")

    tail_i = css.find('@import "tailwindcss"')
    tok_i = css.find('@import "./styles/tokens.css"')
    theme_i = css.find("@theme inline {")
    rec("T1 hud0 invariants: import order + @theme block present",
        0 <= tail_i < tok_i and theme_i > 0,
        f"tail@{tail_i} tok@{tok_i} theme@{theme_i}")
    rec("T1 hud0 invariants: representative @theme mappings intact",
        all(m in css for m in [
            "--color-white: var(--bs-white);",
            "--color-emerald-400: var(--bs-emerald-400);",
            "--color-cyan-400: var(--bs-cyan-400);",
            "--color-zinc-700: var(--bs-zinc-700);",
        ]),
        "spot-checked mappings")
    missing_vars = [v for v in INDEXED_VARS if v not in css]
    rec("T1 hud0 invariants: 13 indexed var(--bs-*) substrings survive",
        not missing_vars, f"missing={missing_vars}")
    found_sent = [s for s in SENTINELS_ABSENT if s in css]
    rec("T1 hud0 invariants: zero hardcoded sentinels in index.css",
        not found_sent, f"found={found_sent}")

    rec("T1 OverlayWindow.tsx untouched (testids + PhysicalSize 240/56)",
        'data-testid="overlay-pill"' in ow
        and 'data-testid="overlay-pill-text"' in ow
        and 'className="overlay-pill"' in ow
        and "PhysicalSize(Math.round(evt.w ?? 240), Math.round(evt.h ?? 56))" in ow,
        "structure + geometry code intact")
    win = conf["app"]["windows"][0]
    rec("T1 tauri.conf window dims unchanged (250x100) + P0 backgroundColor",
        win.get("width") == 250 and win.get("height") == 100
        and win.get("backgroundColor") == "#0a0a16",
        f"{win.get('width')}x{win.get('height')} bg={win.get('backgroundColor')}")
    rec("T1 pre-edit backup holds OLD pill rule (restyle proof)",
        bak.exists()
        and "background: var(--bs-pill-bg);" in bak_txt
        and "box-shadow: var(--bs-shadow-pill);" in bak_txt
        and "background-image" not in bak_txt
        and "background-image: linear-gradient(" in css,
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


# ---- T3: cold launch, both webviews, dark pill ----------------------------

def t3_cold_dark() -> tuple[CDP | None, CDP | None]:
    print("\n--- T3: launch, attach main + overlay, dark pill styles ---")
    global PROC
    _kill_tree(PROC)
    PROC = _launch()
    main = _attach("main")
    overlay = _attach("overlay")
    rec("T3 CDP attached to main (.hud-root)", main is not None,
        "classified via .hud-root")
    rec("T3 CDP attached to overlay (.overlay-pill)", overlay is not None,
        "classified via .overlay-pill")
    if overlay is None:
        return main, None

    st = _eval_json(
        overlay,
        "JSON.stringify({"
        " bg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor,"
        " border: getComputedStyle(document.querySelector('.overlay-pill')).borderColor,"
        " color: getComputedStyle(document.querySelector('.overlay-pill')).color,"
        " img: getComputedStyle(document.querySelector('.overlay-pill')).backgroundImage,"
        " outline: getComputedStyle(document.querySelector('.overlay-pill')).outline,"
        " shadow: getComputedStyle(document.querySelector('.overlay-pill')).boxShadow,"
        " weight: getComputedStyle(document.querySelector('.overlay-pill')).fontWeight,"
        " ls: getComputedStyle(document.querySelector('.overlay-pill')).letterSpacing,"
        " theme: document.documentElement.getAttribute('data-theme')})",
    )
    rec("T3 dark pill backgroundColor == hud0/hu1 T0 value (token-driven)",
        st.get("bg") == DARK_PILL_BG, f"bg={st.get('bg')}")
    rec("T3 dark pill border/color tokens intact",
        st.get("border") == "rgba(120, 180, 255, 0.28)"
        and st.get("color") == "rgb(207, 232, 255)",
        f"border={st.get('border')} color={st.get('color')}")
    rec("T3 new design live: gradient + emerald outline + cyan glow",
        st.get("img") not in (None, "none")
        and "solid" in str(st.get("outline"))
        and "inset" in str(st.get("shadow"))
        and st.get("shadow") != "none"
        and ("34, 211, 238" in str(st.get("shadow"))
             or "oklch" in str(st.get("shadow"))),
        f"outline={st.get('outline')}")
    rec("T3 new typography live (600 / letter-spacing)",
        st.get("weight") == "600" and st.get("ls") not in ("normal", "0px"),
        f"weight={st.get('weight')} ls={st.get('ls')}")
    rec("T3 overlay starts dark",
        st.get("theme") == "dark", f"theme={st.get('theme')}")

    dark_mean, dark_std = _shot(overlay, "pill_dark.png")
    rec("T3 dark pill screenshot archived",
        dark_mean < 80, f"mean={dark_mean} std={dark_std} dir={ARTIFACTS}")
    return main, overlay


# ---- T4: show pill + Win32 geometry ---------------------------------------

def t4_show(main: CDP | None, overlay: CDP | None) -> None:
    print("\n--- T4: overlay show (w=240 h=56 physical) + Win32 rect ---")
    if main is None or overlay is None:
        rec("T4 show skipped (no connection)", False, "launch failed")
        return
    res = _emit_overlay(main, "show", "hu4 pill live")
    rec("T4 overlay show emitted via Rust event plugin", res == "emitted",
        f"emit={res}")

    import win32gui

    h = _find_window(OVERLAY_TITLE)
    if h is None:
        rec("T4 overlay window found by Win32 title", False,
            f"title={OVERLAY_TITLE!r}")
        return
    vis = False
    deadline = time.time() + 4
    while time.time() < deadline:
        if _win32_visible(h):
            vis = True
            break
        time.sleep(0.2)
    rec("T4 overlay window visible after show", vis, f"visible={vis}")

    rect = _win32_rect(h)
    rect_ok = abs(rect[0] - 240) <= 4 and abs(rect[1] - 56) <= 4
    rec("T4 overlay rect == broadcast 240x56 (+-4px, ov1 semantics)",
        rect_ok, f"rect={rect[0]}x{rect[1]}")

    txt = _eval(
        overlay,
        "document.querySelector('[data-testid=\"overlay-pill-text\"]').textContent",
    )
    rec("T4 pill text rendered == sent text", str(txt) == "hu4 pill live",
        f"text={txt!r}")


# ---- T5: live theme-sync ---------------------------------------------------

def _toggle_click(main: CDP) -> bool:
    try:
        _eval(
            main,
            "(() => { const b = document.querySelector('%s');"
            " if (b) { b.click(); return 'clicked'; } return 'absent'; })()" % TOGGLE,
        )
        return True
    except Exception:
        return False


def _wait_overlay_pill_bg(overlay: CDP, want: str, budget_s: float = 1.5) -> str | None:
    deadline = time.time() + budget_s
    last: str | None = None
    while time.time() < deadline:
        try:
            last = str(
                _eval(
                    overlay,
                    "getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor",
                    timeout=5,
                )
            )
            if last == want:
                return last
        except Exception:
            pass
        time.sleep(0.1)
    return last if last == want else None


def t5_theme_sync(main: CDP | None, overlay: CDP | None) -> None:
    print("\n--- T5: live theme-sync (toggle -> overlay repaint <=1.5s) ---")
    if main is None or overlay is None:
        rec("T5 sync skipped (no connection)", False, "launch failed")
        return
    import pyautogui

    pyautogui.hotkey("ctrl", "alt", "b")
    deadline = time.time() + 6
    has_toggle = False
    while time.time() < deadline:
        has_toggle = bool(
            _eval(main, f"!!document.querySelector('{TOGGLE}')", timeout=5)
        )
        if has_toggle:
            break
        time.sleep(0.3)
    rec("T5 expand shows theme-toggle", has_toggle, f"toggle={has_toggle}")
    if not has_toggle:
        return

    base = _eval_json(
        overlay,
        "JSON.stringify({bg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor,"
        " outline: getComputedStyle(document.querySelector('.overlay-pill')).outlineColor})",
    )
    rec("T5 overlay dark baseline before flip",
        base.get("bg") == DARK_PILL_BG, f"base={base}")
    dark_mean, _ = _shot(overlay, "pill_dark_sync.png")

    _toggle_click(main)
    synced = _wait_overlay_pill_bg(overlay, LIGHT_PILL_BG, 1.5)
    rec("T5 overlay pill repaints to LIGHT within 1.5s of toggle",
        synced == LIGHT_PILL_BG, f"pillBg={synced}")
    after = _eval_json(
        overlay,
        "JSON.stringify({bg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor,"
        " outline: getComputedStyle(document.querySelector('.overlay-pill')).outlineColor,"
        " theme: document.documentElement.getAttribute('data-theme')})",
    )
    rec("T5 overlay outline color flips with theme (token-driven neon)",
        after.get("theme") == "light"
        and after.get("outline") != base.get("outline"),
        f"dark={base.get('outline')} light={after.get('outline')}")
    light_mean, _ = _shot(overlay, "pill_light.png")
    rec("T5 light pill screenshot brighter (+30 mean)",
        light_mean > dark_mean + 30,
        f"light={light_mean} dark={dark_mean}")

    _toggle_click(main)
    restored = _wait_overlay_pill_bg(overlay, DARK_PILL_BG, 1.5)
    rec("T5 overlay restored to DARK byte-identically (profile left dark)",
        restored == DARK_PILL_BG, f"pillBg={restored}")

    pyautogui.hotkey("ctrl", "alt", "b")
    deadline = time.time() + 6
    collapsed = False
    while time.time() < deadline:
        collapsed = not bool(
            _eval(main, f"!!document.querySelector('{TOGGLE}')", timeout=5)
        )
        if collapsed:
            break
        time.sleep(0.3)
    rec("T5 collapse hides theme-toggle again", collapsed, f"collapsed={collapsed}")


# ---- T6: hide ---------------------------------------------------------------

def t6_hide(main: CDP | None) -> None:
    print("\n--- T6: overlay hide via plugin event ---")
    if main is None:
        rec("T6 hide skipped (no main conn)", False, "launch failed")
        return
    res = _emit_overlay(main, "hide")
    rec("T6 overlay hide emitted", res == "emitted", f"emit={res}")

    import win32gui

    h = _find_window(OVERLAY_TITLE)
    if h is None:
        rec("T6 overlay window still exists after hide", False,
            f"title={OVERLAY_TITLE!r}")
        return
    still_visible = _win32_visible(h)
    deadline = time.time() + 4
    while time.time() < deadline and still_visible:
        time.sleep(0.2)
        still_visible = _win32_visible(h)
    rec("T6 overlay window not visible after hide", not still_visible,
        f"visible={still_visible}")


# ---- T7: hygiene -------------------------------------------------------------

def t7_hygiene() -> None:
    print("\n--- T7: process tree + backup hygiene ---")
    _kill_tree(PROC)
    time.sleep(0.4)
    killed = _kill_leftovers()
    time.sleep(0.4)
    remaining = _hud_instances()
    rec("T7 no leftover blankslate-hud process", remaining == 0,
        f"killed={killed} remaining={remaining}")
    bak = BACKUP_DIR / "index.css.bak"
    rec("T7 pre-edit backup present and restorable",
        bak.exists() and bak.stat().st_size > 2000,
        f"size={bak.stat().st_size if bak.exists() else 0}")


def main() -> int:
    global CDP_PORT
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    CDP_PORT = free_port()
    _win32_dpi_aware()
    main_cdp: CDP | None = None
    overlay_cdp: CDP | None = None
    try:
        t1_source()
        t2_build()
        main_cdp, overlay_cdp = t3_cold_dark()
        t4_show(main_cdp, overlay_cdp)
        t5_theme_sync(main_cdp, overlay_cdp)
        t6_hide(main_cdp)
    finally:
        for c in (main_cdp, overlay_cdp):
            if c is not None:
                try:
                    c.close()
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
        print(f'  Copy-Item "{BACKUP_DIR}\\index.css.bak" "{HUD_DIR}\\src\\index.css" -Force')
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
