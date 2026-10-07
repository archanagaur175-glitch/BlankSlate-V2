"""Floating overlay pill control (six-feature expansion, Phase 1).

Daemon-side positioner + broadcaster for the Tauri overlay window:
resolves a target window by title substring (win32), computes where the
pill sits INSIDE that window's rect (top-right, screen-clamped), and
broadcasts an ``overlay`` message that the HUD's overlay webview executes
(show/hide + coordinates). Trigger paths:

  * IPC client message ``overlay_show`` / ``overlay_hide`` (deterministic,
    used by gates and the HUD itself),
  * LLM tool ``show_context_pill`` (same code path).

The pill is display-only: the overlay window runs click-through
(set_ignore_cursor_events -> WS_EX_TRANSPARENT), so input always reaches
the window underneath — that property is what the Phase 1 gate proves.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ctypes
import logging
from typing import Any

import win32gui

logger = logging.getLogger("blankslate.overlay")

# Bound by main.py once IPCState exists; show/hide refuse to run unbound
# (a tool call before startup must error, not broadcast into the void).
_state: Any = None

PILL_W = 240
PILL_H = 56

_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_DPI_CTX_PER_MONITOR_V2 = -4


def _enable_thread_dpi_awareness() -> None:
    """Run this worker thread in per-monitor-v2 DPI awareness so every
    GetWindowRect/GetSystemMetrics call returns PHYSICAL pixels.

    The daemon (python.exe) is DPI-unaware by default, so its measurements
    are virtualized (this machine runs at 125% -> 0.8x scaling). Tauri
    positions windows with PHYSICAL coordinates (PhysicalPosition), so the
    overlay must broadcast physical too — otherwise the pill lands ~20%
    off. Thread-scoped (not process-scoped) on purpose: the rest of the
    daemon and its gates keep the virtualized coordinate space they were
    verified in.
    """
    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(
            ctypes.c_void_p(_DPI_CTX_PER_MONITOR_V2))
    except Exception:  # pragma: no cover - older Windows
        pass


# Dedicated single worker: awareness is set once per thread at creation,
# and this executor is used ONLY for overlay win32 measurement.
_win32_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="overlay-dpi",
    initializer=_enable_thread_dpi_awareness,
)


def bind(state: Any) -> None:
    """Attach the IPC state used for broadcasting overlay messages."""
    global _state
    _state = state


def _find_window_rect(title_substr: str) -> tuple[int, tuple[int, int, int, int], str] | None:
    """Blocking win32: shortest-title visible window whose title contains
    title_substr (case-insensitive). Returns (hwnd, GetWindowRect, title)."""
    needle = title_substr.lower()
    hits: list[tuple[int, int, tuple[int, int, int, int], str]] = []
    user32 = ctypes.windll.user32

    def _cb(hwnd: int, _ctx: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if needle and needle in title.lower():
            root = user32.GetAncestor(hwnd, 0) or hwnd
            root_title = win32gui.GetWindowText(root)
            hits.append((len(root_title), root, win32gui.GetWindowRect(root), root_title))
        return True

    win32gui.EnumWindows(_cb, None)
    if not hits:
        return None
    _, hwnd, rect, title = min(hits, key=lambda h: h[0])
    return hwnd, rect, title


def _pill_origin(rect: tuple[int, int, int, int]) -> tuple[int, int]:
    """Top-right of the target window (inside it), clamped to the virtual
    screen so the pill is never off-screen."""
    left, top, right, bottom = rect
    x = right - PILL_W - 16
    y = top + 16
    if x < left + 8:
        x = left + 8
    if y + PILL_H > bottom - 8:
        y = max(left, bottom - 8 - PILL_H)
    user32 = ctypes.windll.user32
    vx = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    x = max(vx, min(x, vx + vw - PILL_W))
    y = max(vy, min(y, vy + vh - PILL_H))
    return int(x), int(y)


def _resolve_pill_origin(title_substr: str) -> tuple[int, tuple[int, int, int, int], str, int, int] | None:
    """Worker-thread entry (DPI-aware): find the target window AND compute
    the pill origin in the same physical-pixel space. Returns
    (hwnd, rect, title, pill_x, pill_y) or None."""
    found = _find_window_rect(title_substr)
    if found is None:
        return None
    hwnd, rect, title = found
    x, y = _pill_origin(rect)
    return hwnd, rect, title, x, y


async def show_pill(target_title: str, text: str) -> dict[str, Any]:
    """Resolve target window, compute pill origin, broadcast show.

    Returns the broadcast payload (without the ``type`` field) on success,
    or ``{"error": ...}`` — never raises for expected failures.
    """
    if _state is None:
        return {"error": "overlay not bound to ipc state"}
    target_title = str(target_title or "").strip()
    text = str(text or "").strip()
    if not target_title:
        return {"error": "target_title required (window title substring)"}
    if not text:
        return {"error": "text required"}

    loop = asyncio.get_running_loop()
    resolved = await loop.run_in_executor(_win32_executor, _resolve_pill_origin,
                                           target_title)
    if resolved is None:
        return {"error": f"no visible window matches {target_title!r}"}
    hwnd, rect, resolved_title, x, y = resolved

    result: dict[str, Any] = {
        "action": "show",
        "x": x,
        "y": y,
        "w": PILL_W,
        "h": PILL_H,
        "text": text,
        "target_title": resolved_title,
        "target_hwnd": int(hwnd),
    }
    from blankslate.ipc.server import broadcast

    await broadcast(_state, {"type": "overlay", **result})
    logger.info("Overlay pill show near %r at (%d,%d)", resolved_title, x, y)
    return result


async def hide_pill() -> dict[str, Any]:
    """Broadcast overlay hide to every connected HUD."""
    if _state is None:
        return {"error": "overlay not bound to ipc state"}
    from blankslate.ipc.server import broadcast

    result: dict[str, Any] = {"action": "hide"}
    await broadcast(_state, {"type": "overlay", **result})
    logger.info("Overlay pill hide")
    return result
