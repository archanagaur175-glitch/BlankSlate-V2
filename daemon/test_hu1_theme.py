"""Phase 1 gate — theme toggle mechanics (dark <-> light switch).

T1  source: theme.ts carries the bs-theme key (localStorage read/write),
    applyTheme writes data-theme + color-scheme + background-color, and
    setTheme/initThemeSync emit+listen `theme_changed`; main.tsx calls
    initThemeSync before render; the toggle lives ONLY in the expanded
    StatusBar (data-testid=theme-toggle wired to setTheme) — CompactWidget
    untouched; index.html bootstrap still reads bs-theme (Phase 0
    invariant: dark default).
T2  npm run typecheck + npm run build + npm run tauri build --no-bundle,
    fresh exe present.
T3  cold launch with CDP (WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS): attach
    the main (.hud-root) and overlay (.overlay-pill) webviews; force a
    fresh dark state if the shared profile was left mid-state (removeItem
    + reload); assert initial dark; SHOW the overlay through the real Rust
    event plugin (plugin:event|emit daemon_message — infra only: ov1 owns
    daemon->overlay end-to-end); Win32 visibility + dark pill baseline
    (computed bg + screenshot stats archived).
T4  expand via the REAL global shortcut Ctrl+Alt+B (lib.rs) -> toggle
    present; CDP .click() -> main flips light (data-theme, localStorage,
    body background, button label) ; REQUIRED OVERLAY SYNC: poll the
    overlay webview on 100ms ticks for up to 1s until data-theme=light
    AND pill background changes; light screenshot mean must exceed the
    dark baseline by >50 (visual ground truth, archived).
T5  restart persistence: GRACEFUL close (WM_CLOSE both windows ->
    default Tauri exit -> WebView2 checkpoints localStorage; a hard kill
    drops a fresh unflushed write, probe-verified), relaunch -> BOTH
    windows report light with zero interaction (bootstrap reads the
    persisted bs-theme).
T6  restore: click back -> dark in main (attr+storage+bg) and overlay
    (100ms-tick poll again); graceful relaunch verification -> profile
    ends dark.
T7  hygiene: exe process tree (WebView2 children) killed, no leftover
    blankslate-hud, pre-edit backups present and restorable,
    GATE_RESULT + exit.

Ground truth: CDP Runtime.evaluate (DOM, localStorage, computed styles),
Win32 window enumeration, screenshot pixel statistics, disk reads, exit
codes — never assumptions.
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
ARTIFACTS = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud1_artifacts")
BACKUP_DIR = Path(r"C:\Users\archa\AppData\Local\Temp\opencode\hud_backup_p1")

OVERLAY_TITLE = "BlankSlate Overlay"
DARK_PILL_BG = "rgba(10, 10, 22, 0.88)"
LIGHT_PILL_BG = "rgba(248, 250, 252, 0.92)"
DARK_BODY_BG = "rgba(10, 10, 22, 0.9)"
LIGHT_BODY_BG = "rgba(241, 245, 249, 0.95)"
TOGGLE = '[data-testid="theme-toggle"]'

RESULTS: list[tuple[str, bool, str]] = []
PROC: subprocess.Popen | None = None
CONNS: dict[str, CDP] = {}


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


def _classify(cdp: CDP, budget_s: float = 8.0) -> str:
    deadline = time.time() + budget_s
    while time.time() < deadline:
        try:
            d = _eval_json(
                cdp,
                "JSON.stringify({o: !!document.querySelector('.overlay-pill'),"
                " h: !!document.querySelector('.hud-root')})",
                timeout=5,
            )
            if d.get("o"):
                return "overlay"
            if d.get("h"):
                return "main"
        except Exception:
            pass
        time.sleep(0.2)
    return "unknown"


def _attach_all(timeout_s: float = 14.0) -> dict[str, CDP]:
    deadline = time.time() + timeout_s
    found: dict[str, CDP] = {}
    seen: set[str] = set()
    while time.time() < deadline and not (
        "main" in found and "overlay" in found
    ):
        for tgt in _list_pages(CDP_PORT):
            if tgt["id"] in seen:
                continue
            seen.add(tgt["id"])
            try:
                c = _connect(CDP_PORT, tgt)
            except Exception:
                continue
            kind = _classify(c, budget_s=max(2.0, deadline - time.time()))
            if kind in ("main", "overlay") and kind not in found:
                found[kind] = c
            else:
                c.close()
        if "main" in found and "overlay" in found:
            break
        time.sleep(0.4)
    return found


def _close_conns() -> None:
    for c in list(CONNS.values()):
        try:
            c.close()
        except Exception:
            pass
    CONNS.clear()


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


def _launch() -> subprocess.Popen:
    env = dict(os.environ)
    env["BS_HUD_AUTOSTART"] = "0"
    env["BS_HUD_QUIT_PROMPT"] = "0"
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        f"--remote-debugging-port={CDP_PORT}"
    )
    return subprocess.Popen([str(EXE)], cwd=str(EXE.parent), env=env)


def _overlay_show(main_cdp: CDP) -> str:
    return str(
        _eval(
            main_cdp,
            "(async () => { try {"
            " await window.__TAURI_INTERNALS__.invoke('plugin:event|emit', {"
            " event: 'daemon_message',"
            " payload: JSON.stringify({type:'overlay', action:'show',"
            " x:400, y:400, w:240, h:56, text:'theme sync test'}) });"
            " return 'emitted'; } catch (e) { return 'err:' + String(e); } })()",
            timeout=10,
        )
    )


def _overlay_win32_visible(timeout_s: float = 4.0) -> bool:
    import win32gui

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        found: list[int] = []

        def cb(h: int, _ctx: Any) -> bool:
            if win32gui.GetWindowText(h) == OVERLAY_TITLE:
                found.append(h)
            return True

        win32gui.EnumWindows(cb, None)
        if found and win32gui.IsWindowVisible(found[0]):
            return True
        time.sleep(0.2)
    return False


def _graceful_close(proc: subprocess.Popen | None) -> bool:
    """Real-user shutdown: WM_CLOSE both windows -> default Tauri exit ->
    WebView2 checkpoints localStorage. Hard kill drops fresh unflushed
    writes (probe-verified), so persistence must survive THIS path."""
    if proc is None:
        return True
    import win32con
    import win32gui

    for title in ("BlankSlate", OVERLAY_TITLE):
        found: list[int] = []

        def cb(h: int, _ctx: Any) -> bool:
            if win32gui.GetWindowText(h) == title:
                found.append(h)
            return True

        win32gui.EnumWindows(cb, None)
        for h in found:
            try:
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
    deadline = time.time() + 6
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        _kill_tree(proc)
        return False
    return True


def _wait_overlay_pill(cdp: CDP, timeout_s: float = 6.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if _eval(cdp, "!!document.querySelector('.overlay-pill')", timeout=5):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _shot(cdp: CDP, name: str) -> tuple[float, float]:
    from PIL import Image, ImageStat

    r = cdp.cmd("Page.captureScreenshot", {"format": "png"})
    path = ARTIFACTS / name
    path.write_bytes(base64.b64decode(r["data"]))
    st = ImageStat.Stat(Image.open(path).convert("L"))
    return round(float(st.mean[0]), 1), round(float(st.stddev[0]), 1)


def _expand_and_get_toggle(main_cdp: CDP, budget_s: float = 6.0) -> str | None:
    import pyautogui

    pyautogui.hotkey("ctrl", "alt", "b")
    deadline = time.time() + budget_s
    while time.time() < deadline:
        try:
            txt = _eval(main_cdp, f"(() => {{ const b = document.querySelector('{TOGGLE}');"
                                  f" return b ? b.textContent : null; }})()")
            if txt:
                return str(txt)
        except Exception:
            pass
        time.sleep(0.3)
    return None


# ---- T1: source asserts -------------------------------------------------

def t1_source() -> None:
    print("\n--- T1: source (theme.ts / main.tsx / StatusBar / bootstrap) ---")
    theme = (HUD_DIR / "src" / "theme.ts").read_text(encoding="utf-8")
    main = (HUD_DIR / "src" / "main.tsx").read_text(encoding="utf-8")
    sb = (HUD_DIR / "src" / "components" / "StatusBar.tsx").read_text(encoding="utf-8")
    cw = (HUD_DIR / "src" / "components" / "CompactWidget.tsx").read_text(encoding="utf-8")
    html = (HUD_DIR / "index.html").read_text(encoding="utf-8")

    rec("T1 theme.ts bs-theme key + localStorage read/write",
        '"bs-theme"' in theme
        and "localStorage.getItem(KEY)" in theme
        and "localStorage.setItem(KEY, t)" in theme,
        "key + get/set present")
    rec("T1 applyTheme writes data-theme + color-scheme + background",
        'setAttribute("data-theme", t)' in theme
        and "r.style.colorScheme = t" in theme
        and "r.style.backgroundColor" in theme,
        "attribute + inline paint")
    rec("T1 setTheme emits theme_changed, initThemeSync listens",
        'emit("theme_changed", t)' in theme
        and 'listen<Theme>("theme_changed"' in theme,
        "cross-window event wired")
    rec("T1 main.tsx wires initThemeSync before render",
        'import { initThemeSync } from "./theme"' in main
        and main.find("initThemeSync();") < main.find("createRoot"),
        "called once at startup (both windows share main.tsx)")
    rec("T1 StatusBar toggle testid + setTheme + getStoredTheme",
        'data-testid="theme-toggle"' in sb
        and "setTheme(next)" in sb
        and "getStoredTheme()" in sb
        and "useState<Theme>" in sb,
        "button wired to theme module")
    rec("T1 CompactWidget untouched (no theme code)",
        "theme-toggle" not in cw
        and "setTheme" not in cw
        and "theme" not in cw.lower(),
        "compact pill has zero theme surface")
    rec("T1 index.html bootstrap still defaults dark (Phase 0 invariant)",
        'localStorage.getItem("bs-theme")' in html
        and 't = "dark"' in html,
        "bootstrap untouched")


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


# ---- T3: cold launch + overlay baseline ---------------------------------

def t3_launch() -> None:
    print("\n--- T3: cold launch, fresh dark state, overlay baseline ---")
    global PROC
    _kill_tree(PROC)
    PROC = _launch()

    CONNS.update(_attach_all())
    rec("T3 CDP attached to main + overlay webviews",
        "main" in CONNS and "overlay" in CONNS,
        f"attached={sorted(CONNS)}")
    if "main" not in CONNS:
        return

    main = CONNS["main"]
    st = _eval_json(
        main,
        "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
        " ls: localStorage.getItem('bs-theme'),"
        " bodyBg: getComputedStyle(document.body).backgroundColor})",
    )
    if st.get("ls") not in (None, "dark"):
        _eval(main, "localStorage.removeItem('bs-theme'); location.reload(); 'x'")
        if "overlay" in CONNS:
            try:
                _eval(CONNS["overlay"], "location.reload(); 'x'", timeout=5)
            except Exception:
                pass
        time.sleep(1.5)
        st = _eval_json(
            main,
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " ls: localStorage.getItem('bs-theme'),"
            " bodyBg: getComputedStyle(document.body).backgroundColor})",
        )
    if "overlay" in CONNS:
        _wait_overlay_pill(CONNS["overlay"])
    rec("T3 initial main window is dark (fresh state)",
        st.get("theme") == "dark" and st.get("ls") in (None, "dark")
        and st.get("bodyBg") == DARK_BODY_BG,
        f"state={st}")

    res = _overlay_show(main)
    rec("T3 overlay show emitted via Rust event plugin",
        res == "emitted", f"emit={res}")
    visible = _overlay_win32_visible()
    rec("T3 overlay window Win32 visible", visible, f"title={OVERLAY_TITLE!r}")

    if "overlay" in CONNS and visible:
        ov = CONNS["overlay"]
        base = _eval_json(
            ov,
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " pillBg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor})",
        )
        rec("T3 overlay pill dark baseline",
            base.get("theme") == "dark" and base.get("pillBg") == DARK_PILL_BG,
            f"state={base}")
        mean, std = _shot(ov, "overlay_dark.png")
        DARK_SHOTS.append((mean, std))
        rec("T3 dark overlay screenshot archived",
            mean < 60, f"mean={mean} std={std} dir={ARTIFACTS}")
    else:
        rec("T3 overlay baseline skipped (no overlay conn/visibility)", False,
            f"conn={'overlay' in CONNS} visible={visible}")


DARK_SHOTS: list[tuple[float, float]] = []


# ---- T4: expand + click + overlay sync poll -----------------------------

def t4_toggle() -> None:
    print("\n--- T4: expand -> toggle click -> main light -> overlay sync (<=1s) ---")
    if "main" not in CONNS:
        rec("T4 toggle flow skipped (no main conn)", False, "launch failed")
        return
    main = CONNS["main"]

    txt = _expand_and_get_toggle(main)
    rec("T4 Ctrl+Alt+B expands -> theme-toggle present",
        txt is not None, f"label={txt!r}")
    if txt is None:
        return
    rec("T4 toggle starts at 'Theme dark'", txt.strip().lower() == "Theme dark".lower(),
        f"label={txt!r}")

    _eval(main, f"document.querySelector('{TOGGLE}').click(); 'clicked'")

    post = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        post = _eval_json(
            main,
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " ls: localStorage.getItem('bs-theme'),"
            " bodyBg: getComputedStyle(document.body).backgroundColor,"
            " btn: document.querySelector('%s') ?"
            " document.querySelector('%s').textContent : null})" % (TOGGLE, TOGGLE),
        )
        if (post.get("theme") == "light" and post.get("ls") == "light"
                and post.get("bodyBg") == LIGHT_BODY_BG):
            break
        time.sleep(0.2)
    rec("T4 main flips light (data-theme + localStorage + body bg)",
        post is not None and post.get("theme") == "light"
        and post.get("ls") == "light" and post.get("bodyBg") == LIGHT_BODY_BG,
        f"state={post}")
    rec("T4 toggle label follows React state",
        post is not None and (post.get("btn") or "").strip().lower() == "Theme light".lower(),
        f"btn={post.get('btn') if post else None!r}")

    if "overlay" not in CONNS:
        rec("T4 overlay sync skipped (no overlay conn)", False, "")
        return
    ov = CONNS["overlay"]
    synced_after: float | None = None
    last: dict = {}
    t0 = time.time()
    for i in range(10):
        last = _eval_json(
            ov,
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " pillBg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor})",
        )
        if last.get("theme") == "light" and last.get("pillBg") == LIGHT_PILL_BG:
            synced_after = time.time() - t0
            break
        time.sleep(0.1)
    rec("T4 overlay syncs within 1s on 100ms ticks (event -> attr + pill repaint)",
        synced_after is not None and synced_after <= 1.0,
        f"synced_after={synced_after if synced_after is None else round(synced_after, 3)}s "
        f"ticks={i + 1} last={last}")

    mean, std = _shot(ov, "overlay_light.png")
    dark_mean = DARK_SHOTS[0][0] if DARK_SHOTS else 0.0
    rec("T4 light screenshot visibly brighter than dark baseline (+50)",
        mean > dark_mean + 50, f"light_mean={mean} dark_mean={dark_mean}")


# ---- T5: restart persistence --------------------------------------------

def t5_restart() -> None:
    print("\n--- T5: restart persistence (zero-interaction light) ---")
    global PROC
    _close_conns()
    graceful = _graceful_close(PROC)
    time.sleep(0.5)
    PROC = _launch()
    CONNS.update(_attach_all())
    if "main" not in CONNS:
        rec("T5 relaunched main attached", False, "no main target")
        return
    main = CONNS["main"]
    st = _eval_json(
        main,
        "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
        " ls: localStorage.getItem('bs-theme'),"
        " bodyBg: getComputedStyle(document.body).backgroundColor})",
    )
    rec("T5 relaunched main window is light with zero interaction",
        st.get("theme") == "light" and st.get("ls") == "light"
        and st.get("bodyBg") == LIGHT_BODY_BG,
        f"graceful={graceful} state={st}")
    if "overlay" in CONNS:
        ovst = _eval_json(
            CONNS["overlay"],
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " pillBg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor})",
        )
        rec("T5 relaunched overlay window also light (per-window bootstrap)",
            ovst.get("theme") == "light" and ovst.get("pillBg") == LIGHT_PILL_BG,
            f"state={ovst}")
    else:
        rec("T5 relaunched overlay attached", False, "no overlay target")


# ---- T6: restore to dark -------------------------------------------------

def t6_restore() -> None:
    print("\n--- T6: click back to dark + relaunch verification ---")
    global PROC
    if "main" not in CONNS:
        rec("T6 restore skipped (no main conn)", False, "")
        return
    main = CONNS["main"]

    txt = _expand_and_get_toggle(main)
    rec("T6 toggle reachable after restart (expand works)",
        txt is not None, f"label={txt!r}")
    if txt is None:
        return

    _eval(main, f"document.querySelector('{TOGGLE}').click(); 'clicked'")
    post = None
    deadline = time.time() + 2.0
    while time.time() < deadline:
        post = _eval_json(
            main,
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " ls: localStorage.getItem('bs-theme'),"
            " bodyBg: getComputedStyle(document.body).backgroundColor})",
        )
        if post.get("theme") == "dark" and post.get("ls") == "dark":
            break
        time.sleep(0.2)
    rec("T6 main back to dark (attr + storage + bg)",
        post is not None and post.get("theme") == "dark"
        and post.get("ls") == "dark" and post.get("bodyBg") == DARK_BODY_BG,
        f"state={post}")

    if "overlay" in CONNS:
        ov = CONNS["overlay"]
        synced: dict = {}
        t0 = time.time()
        for _ in range(10):
            synced = _eval_json(
                ov,
                "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
                " pillBg: getComputedStyle(document.querySelector('.overlay-pill')).backgroundColor})",
            )
            if synced.get("theme") == "dark" and synced.get("pillBg") == DARK_PILL_BG:
                break
            time.sleep(0.1)
        rec("T6 overlay back to dark within 1s (100ms ticks)",
            synced.get("theme") == "dark" and synced.get("pillBg") == DARK_PILL_BG,
            f"elapsed={time.time() - t0:.3f}s state={synced}")
        m2, s2 = _shot(ov, "overlay_dark_restored.png")
        rec("T6 restored dark screenshot archived",
            DARK_SHOTS and m2 < DARK_SHOTS[0][0] + 20,
            f"mean={m2} std={s2}")

    _close_conns()
    graceful = _graceful_close(PROC)
    time.sleep(0.5)
    PROC = _launch()
    conns2 = _attach_all()
    if "main" in conns2:
        final = _eval_json(
            conns2["main"],
            "JSON.stringify({theme: document.documentElement.getAttribute('data-theme'),"
            " ls: localStorage.getItem('bs-theme')})",
        )
        rec("T6 final relaunch verifies profile restored to dark",
            final.get("theme") == "dark" and final.get("ls") == "dark",
            f"graceful={graceful} state={final}")
        for c in conns2.values():
            try:
                c.close()
            except Exception:
                pass
    else:
        rec("T6 final relaunch main attached", False, "no main target")


# ---- T7: hygiene ---------------------------------------------------------

def t7_hygiene() -> None:
    print("\n--- T7: process tree + backups hygiene ---")
    _close_conns()
    _kill_tree(PROC)
    time.sleep(0.4)
    killed = _kill_leftovers()
    time.sleep(0.4)
    remaining = _hud_instances()
    rec("T7 no leftover blankslate-hud process", remaining == 0,
        f"killed={killed} remaining={remaining}")

    sb_bak = BACKUP_DIR / "StatusBar.tsx.bak"
    main_bak = BACKUP_DIR / "main.tsx.bak"
    sb_ok = sb_bak.exists() and "theme-toggle" not in sb_bak.read_text(encoding="utf-8")
    main_ok = main_bak.exists() and "initThemeSync" not in main_bak.read_text(encoding="utf-8")
    rec("T7 pre-edit backups present and restorable",
        sb_ok and main_ok,
        f"StatusBar={sb_bak.exists()} main={main_bak.exists()}")


def main() -> int:
    global CDP_PORT
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    CDP_PORT = free_port()
    try:
        t1_source()
        t2_build()
        t3_launch()
        t4_toggle()
        t5_restart()
        t6_restore()
    finally:
        t7_hygiene()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nGATE_RESULT: {passed}/{total} passed")
    if passed != total:
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("FAILED: " + "; ".join(failed))
        print("RESTORE (only if sources must revert):")
        print(f'  Copy-Item "{BACKUP_DIR}\\StatusBar.tsx.bak" '
              f'"{HUD_DIR}\\src\\components\\StatusBar.tsx" -Force')
        print(f'  Copy-Item "{BACKUP_DIR}\\main.tsx.bak" '
              f'"{HUD_DIR}\\src\\main.tsx" -Force')
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
