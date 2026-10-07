"""Desktop automation — window enumeration and focus switching.

Uses win32gui/AttachThreadInput pattern to reliably switch focus
even from background processes (bypasses Windows foreground-lock).

All blocking Win32 calls run via loop.run_in_executor().
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
from typing import Any

import psutil
import win32gui
import win32con
import win32process

logger = logging.getLogger("blankslate.tools.window_manager")

# Keep a reference to the input-thread handle so we can release it
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


# ── Blocking helpers (run in executor) ──────────────────────────────────────

def _get_foreground_pid() -> int:
    return win32gui.GetWindowThreadProcessId(win32gui.GetForegroundWindow())


GA_ROOT = 0x0000
GA_ROOTOWNER = 0x0003


def _enum_windows_blocking() -> list[dict[str, Any]]:
    """Return list of visible top-level windows with title + process info.

    Each hwnd is resolved to its GA_ROOT ancestor so the handle we store
    is always the true top-level window (not a child or helper).
    """
    windows: list[dict[str, Any]] = []

    def callback(hwnd: int, _ctx: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return True
        # Resolve to root ancestor — critical for Chromium/Electron
        root_hwnd = _user32.GetAncestor(hwnd, GA_ROOT)
        if root_hwnd:
            hwnd = root_hwnd
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = psutil.Process(pid).name() if pid else "unknown"
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
            proc_name = "unknown"
            pid = 0
        cls = win32gui.GetClassName(hwnd) if hwnd else "unknown"
        windows.append({
            "hwnd": hwnd,
            "title": title,
            "class": cls,
            "process": proc_name,
            "pid": pid,
        })
        return True

    win32gui.EnumWindows(callback, None)
    # Deduplicate by hwnd (root resolution may produce duplicates)
    seen: set[int] = set()
    unique: list[dict[str, Any]] = []
    for w in windows:
        if w["hwnd"] not in seen:
            seen.add(w["hwnd"])
            unique.append(w)
    return unique


def _switch_to_window_blocking(target_hwnd: int) -> dict[str, Any]:
    """Switch foreground to target_hwnd with multiple fallback strategies.

    Key techniques:
    - Resolve to GA_ROOT ancestor (Chromium helper windows aren't the real target)
    - Synthetic Alt key input before SetForegroundWindow (unlocks foreground permission)
    - SwitchToThisWindow as fallback (bypasses foreground-lock directly)
    - SetWindowPos HWND_TOP for Z-order boost
    """
    if not win32gui.IsWindow(target_hwnd):
        return {"error": f"Window handle {target_hwnd} is not a valid window."}

    # Fix #1: Resolve to true root ancestor
    root_hwnd = _user32.GetAncestor(target_hwnd, GA_ROOT)
    if root_hwnd:
        target_hwnd = root_hwnd

    title = win32gui.GetWindowText(target_hwnd)
    cls = win32gui.GetClassName(target_hwnd)
    _, pid = win32process.GetWindowThreadProcessId(target_hwnd)
    proc_name = "unknown"
    try:
        proc_name = psutil.Process(pid).name()
    except Exception:
        pass
    logger.info(
        "Switch: hwnd=%d root=%d class=%r title=%r process=%r",
        target_hwnd, root_hwnd, cls, title, proc_name,
    )

    # Restore if minimized
    if win32gui.IsIconic(target_hwnd):
        win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)

    # Show window if hidden
    if not win32gui.IsWindowVisible(target_hwnd):
        win32gui.ShowWindow(target_hwnd, win32con.SW_SHOW)

    import time
    success = False
    strategy_used = "none"

    # Strategy 1: Alt nudge + SetForegroundWindow
    try:
        _send_alt_nudge()
        time.sleep(0.1)
        win32gui.SetForegroundWindow(target_hwnd)
        time.sleep(0.1)
        success = win32gui.GetForegroundWindow() == target_hwnd
        if success:
            strategy_used = "alt+SetForegroundWindow"
    except Exception as e:
        logger.warning("Strategy 1 failed: %s", e)

    # Strategy 2: mouse_event click at window center
    if not success:
        try:
            rect = win32gui.GetWindowRect(target_hwnd)
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
            _user32.SetCursorPos(cx, cy)
            time.sleep(0.05)
            _user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
            _user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
            time.sleep(0.1)
            success = win32gui.GetForegroundWindow() == target_hwnd
            if success:
                strategy_used = "mouse_click"
        except Exception as e:
            logger.warning("Strategy 2 failed: %s", e)

    # Strategy 3: SwitchToThisWindow
    if not success:
        try:
            _user32.SwitchToThisWindow(target_hwnd, True)
            time.sleep(0.1)
            success = win32gui.GetForegroundWindow() == target_hwnd
            if success:
                strategy_used = "SwitchToThisWindow"
        except Exception as e:
            logger.warning("Strategy 3 failed: %s", e)

    # Strategy 4: SetWindowPos HWND_TOP
    if not success:
        try:
            HWND_TOP = 0
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            win32gui.SetWindowPos(target_hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            time.sleep(0.1)
            _send_alt_nudge()
            time.sleep(0.1)
            win32gui.SetForegroundWindow(target_hwnd)
            time.sleep(0.1)
            success = win32gui.GetForegroundWindow() == target_hwnd
            if success:
                strategy_used = "SetWindowPos+HWND_TOP+alt"
        except Exception as e:
            logger.warning("Strategy 4 failed: %s", e)

    # Strategy 5: ShowWindow + BringWindowToTop + Alt + SetForegroundWindow
    if not success:
        try:
            win32gui.ShowWindow(target_hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(target_hwnd)
            _send_alt_nudge()
            time.sleep(0.1)
            win32gui.SetForegroundWindow(target_hwnd)
            time.sleep(0.1)
            success = win32gui.GetForegroundWindow() == target_hwnd
            if success:
                strategy_used = "ShowWindow+BringWindowToTop+alt"
        except Exception as e:
            logger.warning("Strategy 5 failed: %s", e)

    result_title = win32gui.GetWindowText(win32gui.GetForegroundWindow())
    return {
        "success": success,
        "hwnd": target_hwnd,
        "title": title,
        "class": cls,
        "foreground_title": result_title,
        "strategy": strategy_used,
        "result": f"Switched to '{title}'." if success else f"Attempted switch to '{title}' (foreground: '{result_title}', strategy: {strategy_used}).",
    }


def _send_alt_nudge() -> None:
    """Send a synthetic Alt key down+up via keybd_event.

    This is the standard technique for bypassing Windows' foreground-lock:
    the brief Alt input makes the OS think the calling process just received
    user input, which relaxes the SetForegroundWindow restriction.
    """
    VK_MENU = 0x12
    _user32.keybd_event(VK_MENU, 0, 0, 0)
    _user32.keybd_event(VK_MENU, 0, 0x0002, 0)


def _get_foreground_window_blocking() -> dict[str, Any]:
    """Get info about the current foreground window."""
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name() if pid else "unknown"
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
        proc_name = "unknown"
        pid = 0
    return {"hwnd": hwnd, "title": title, "process": proc_name, "pid": pid}


# ── Async tool handlers ─────────────────────────────────────────────────────

async def list_windows(_args: dict[str, Any]) -> dict[str, Any]:
    """List all visible top-level windows."""
    loop = asyncio.get_running_loop()
    windows = await loop.run_in_executor(None, _enum_windows_blocking)
    return {
        "tool": "list_windows",
        "windows": windows,
        "count": len(windows),
        "result": f"Found {len(windows)} visible windows.",
        "reply": f"Found {len(windows)} windows: {', '.join(w['title'] for w in windows[:10])}.",
        "deterministic": True,
    }


# ── F7: natural-language target extraction for query-sourced switches ──

_SWITCH_PREFIXES = (
    "switch over to", "switch to", "switch window to", "switch",
    "bring me to", "bring up", "bring", "go to", "jump to",
    "focus on", "focus", "activate", "open", "pull up",
    "show me", "show",
)
_SWITCH_FILLER = (
    "the", "my", "a", "an", "that", "this", "please",
    "window", "app", "application",
)
_SWITCH_TRAILING = ("please", "window", "app", "application")


def _extract_window_target(query: str) -> str:
    """Pull the window target out of a natural-language command (F7).

    'switch to my Brave' -> 'Brave'; 'bring up the calculator' ->
    'calculator'. A prefix only counts when it ends at a word boundary
    ('switched to X' is left alone). Callers keep the raw query as a
    fallback candidate, and exact args['title'] never reaches here.
    """
    q = query.strip()
    if not q:
        return ""
    lowered = q.lower()
    for prefix in sorted(_SWITCH_PREFIXES, key=len, reverse=True):
        if lowered.startswith(prefix):
            rest = lowered[len(prefix):]
            if rest and not rest.startswith(" ") and prefix != lowered:
                continue  # prefix is part of a longer word ('switched ...')
            q = q[len(prefix):].strip()
            break
    # leading filler: 'switch to my X' -> 'X'
    while q:
        first = q.split(" ", 1)[0].lower().strip(",.!?")
        if first in _SWITCH_FILLER:
            q = q.split(" ", 1)[1] if " " in q else ""
        else:
            break
    # trailing filler + punctuation: 'X window please.' -> 'X'
    while q:
        before = q
        q = q.strip(" \t\"'.,!?")
        last = q.rsplit(" ", 1)[-1].lower()
        if last in _SWITCH_TRAILING:
            q = q.rsplit(" ", 1)[0] if " " in q else ""
        if q == before:
            break
    return q


async def switch_window(args: dict[str, Any]) -> dict[str, Any]:
    """Switch foreground to a window by title substring or handle."""
    title_query = str(args.get("title", args.get("query", ""))).strip()
    hwnd_arg = args.get("hwnd")

    loop = asyncio.get_running_loop()

    # Direct handle lookup
    if hwnd_arg is not None:
        target_hwnd = int(hwnd_arg)
        try:
            from blankslate.focus.memory import snapshot_current
            await snapshot_current()
        except Exception:
            logger.exception("focus auto-snapshot failed — switching anyway")
        info = await loop.run_in_executor(None, _switch_to_window_blocking, target_hwnd)
        if "error" in info:
            return {"tool": "switch_window", **info, "result": None}
        return {
            "tool": "switch_window",
            "hwnd": info["hwnd"],
            "title": info["title"],
            "class": info.get("class"),
            "success": info.get("success", False),
            "strategy": info.get("strategy", "none"),
            "foreground_title": info.get("foreground_title"),
            "result": info["result"],
            "reply": info["result"],
            "deterministic": True,
        }

    # Title substring search
    if not title_query:
        return {
            "tool": "switch_window",
            "error": "provide a title substring or hwnd to switch to",
            "result": None,
        }

    windows = await loop.run_in_executor(None, _enum_windows_blocking)
    # Exact substring match, prefer shortest title (most specific)
    # Exclude dialog-like windows (titles starting with common dialog prefixes)
    _DIALOG_PREFIXES = ("restore pages", "save as", "open file", "confirm", "alert")

    # F7: NL extraction for query-sourced input; explicit args['title']
    # stays verbatim (raw is then the only candidate).
    candidates = [title_query]
    explicit_title = str(args.get("title") or "").strip()
    if not explicit_title:
        extracted = _extract_window_target(title_query)
        if extracted and extracted.lower() != title_query.lower():
            candidates = [extracted, title_query]

    def _find(cands: list[str], allow_dialogs: bool) -> tuple[list[dict], str]:
        for cand in cands:
            lq = cand.lower()
            hits = [
                w for w in windows
                if lq in w["title"].lower()
                and (
                    allow_dialogs
                    or not any(
                        w["title"].lower().startswith(p)
                        for p in _DIALOG_PREFIXES
                    )
                )
            ]
            if hits:
                return hits, cand
        return [], ""

    matches, _matched = _find(candidates, allow_dialogs=False)
    if not matches:
        matches, _matched = _find(candidates, allow_dialogs=True)
    if not matches:
        return {
            "tool": "switch_window",
            "error": f"no window found matching {title_query!r}",
            "available": [w["title"] for w in windows[:20]],
            "result": None,
        }
    target = min(matches, key=lambda w: len(w["title"]))
    try:
        from blankslate.focus.memory import snapshot_current
        await snapshot_current()
    except Exception:
        logger.exception("focus auto-snapshot failed — switching anyway")
    info = await loop.run_in_executor(None, _switch_to_window_blocking, target["hwnd"])
    if "error" in info:
        return {"tool": "switch_window", **info, "result": None}
    return {
        "tool": "switch_window",
        "hwnd": info["hwnd"],
        "title": info["title"],
        "class": info.get("class"),
        "success": info.get("success", False),
        "strategy": info.get("strategy", "none"),
        "foreground_title": info.get("foreground_title"),
        "result": info["result"],
        "reply": info["result"],
        "deterministic": True,
    }


async def get_foreground_window(_args: dict[str, Any]) -> dict[str, Any]:
    """Get info about the current foreground window."""
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _get_foreground_window_blocking)
    return {
        "tool": "get_foreground_window",
        **info,
        "result": f"Foreground: '{info['title']}' ({info['process']})",
        "deterministic": True,
    }
