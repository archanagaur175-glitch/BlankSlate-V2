"""Active-window capture + exclusion gate (design doc section 2).

Ordering is the control: foreground metadata is read first, the exclusion
lists are evaluated, and only a non-excluded window ever reaches mss.
Capture mirrors the proven screen_xray clamp (virtual-screen safe).

Nothing here writes files — frames live in RAM for the pHash/OCR pass and
are then discarded (D9: no thumbnails).
"""

from __future__ import annotations

import logging
from typing import Any

import psutil
import win32api
import win32con
import win32gui
import win32process
from PIL import Image

logger = logging.getLogger("blankslate.timeline.capture")


def foreground_meta() -> dict[str, Any]:
    """hwnd/title/process of the foreground window ('process' may be '')."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return {}
        title = win32gui.GetWindowText(hwnd) or "(untitled)"
        process = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            process = psutil.Process(pid).name()
        except Exception:
            process = ""
        return {"hwnd": hwnd, "title": title, "process": process}
    except Exception:
        logger.exception("foreground meta failed")
        return {}


def excluded(meta: dict[str, Any], cfg: Any) -> bool:
    """True when the window matches a configured exclusion (process/title)."""
    proc = str(meta.get("process", "")).lower()
    title = str(meta.get("title", "")).lower()
    for want in getattr(cfg, "timeline_exclude_processes", ()) or ():
        if want and want.lower() == proc:
            return True
    for want in getattr(cfg, "timeline_exclude_titles", ()) or ():
        if want and want.lower() in title:
            return True
    return False


def capture_foreground(hwnd: int) -> Image.Image | None:
    """Grab the foreground window rect clamped to the virtual screen.

    Returns None for minimized/off-screen/degenerate windows (structured
    skip — never a garbage capture).
    """
    try:
        import mss  # noqa: PLC0415 — local import: structured error if missing
    except ImportError:
        logger.warning("timeline capture unavailable: no module 'mss'")
        return None

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    vs_left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
    vs_top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
    vs_w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
    vs_h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
    x1 = max(int(left), vs_left)
    y1 = max(int(top), vs_top)
    x2 = min(int(right), vs_left + vs_w)
    y2 = min(int(bottom), vs_top + vs_h)
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None

    try:
        with mss.mss() as sct:
            shot = sct.grab({"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1})
        return Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception:
        logger.exception("timeline capture failed")
        return None
