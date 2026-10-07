"""Focus Memory — snapshot and restore foreground window context.

Stores the most recent foreground windows as a LIFO stack (max 5) so the
assistant can remember where it was acting and return there afterwards.

Design:
- FocusFrame is a frozen snapshot (hwnd/pid/process/title)
- Validation before restore: window must still exist with the same title
  (guards against HWND reuse by an unrelated window)
- Restore delegates to window_manager._switch_to_window_blocking (verified)
- All blocking Win32 calls run via loop.run_in_executor()
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

import psutil
import win32gui
import win32process

logger = logging.getLogger("blankslate.focus.memory")

MAX_FRAMES = 5


@dataclass(frozen=True)
class FocusFrame:
    """One remembered foreground window state."""

    hwnd: int
    pid: int
    process_name: str
    title: str


@dataclass
class FocusStack:
    """Thread-safe LIFO stack of focus frames (most recent first)."""

    _frames: list[FocusFrame] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def push(self, frame: FocusFrame) -> None:
        with self._lock:
            self._frames.insert(0, frame)
            del self._frames[MAX_FRAMES:]

    def pop(self) -> FocusFrame | None:
        with self._lock:
            return self._frames.pop(0) if self._frames else None

    def depth(self) -> int:
        with self._lock:
            return len(self._frames)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()


# Daemon-wide stack (module-level singleton).
STACK = FocusStack()


# ── Blocking helpers (run in executor) ──────────────────────────────────────

def _capture_blocking() -> FocusFrame | None:
    """Capture the current foreground window; None when there is none."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or not win32gui.IsWindow(hwnd):
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            process_name = psutil.Process(pid).name()
        except Exception:
            process_name = "unknown"
        return FocusFrame(hwnd=hwnd, pid=pid, process_name=process_name, title=title)
    except Exception:
        logger.exception("focus capture failed")
        return None


def _validate_blocking(frame: FocusFrame) -> str | None:
    """Return None if the frame is restorable, else the reason it is not."""
    try:
        if not win32gui.IsWindow(frame.hwnd):
            return f"saved window no longer exists: {frame.title!r}"
        current = win32gui.GetWindowText(frame.hwnd)
        if current != frame.title:
            return (
                f"window changed since snapshot "
                f"({frame.title!r} -> {current!r}); re-snapshot required"
            )
        return None
    except Exception as exc:
        logger.exception("focus validation failed")
        return f"validation failed: {exc}"


def _restore_blocking(frame: FocusFrame) -> dict[str, Any]:
    """Validate, then switch back via the verified window switcher."""
    from blankslate.tools.window_manager import _switch_to_window_blocking

    problem = _validate_blocking(frame)
    if problem:
        return {"error": problem}
    return _switch_to_window_blocking(frame.hwnd)


# ── Async API ───────────────────────────────────────────────────────────────

async def snapshot_current() -> FocusFrame | None:
    """Capture and push the current foreground window (best-effort).

    Used by the switch_window auto-snapshot hook; never raises.
    """
    try:
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(None, _capture_blocking)
        if frame is not None:
            STACK.push(frame)
            logger.info(
                "Focus snapshot: hwnd=%d %r (depth=%d)",
                frame.hwnd, frame.title, STACK.depth(),
            )
        return frame
    except Exception:
        logger.exception("focus auto-snapshot failed")
        return None


async def focus_snapshot(_args: dict[str, Any]) -> dict[str, Any]:
    """Save the current foreground window for later restoration."""
    try:
        loop = asyncio.get_running_loop()
        frame = await loop.run_in_executor(None, _capture_blocking)
        if frame is None:
            return {
                "tool": "focus_snapshot",
                "error": "no foreground window to snapshot",
                "result": None,
            }
        STACK.push(frame)
        depth = STACK.depth()
        return {
            "tool": "focus_snapshot",
            "title": frame.title,
            "process": frame.process_name,
            "hwnd": frame.hwnd,
            "depth": depth,
            "result": f"Saved focus: '{frame.title}' ({depth}/{MAX_FRAMES}).",
            "reply": f"Saved focus on {frame.title}.",
            "deterministic": True,
        }
    except Exception as exc:
        logger.exception("focus_snapshot failed")
        return {"tool": "focus_snapshot", "error": f"snapshot failed: {exc}", "result": None}


async def focus_restore(_args: dict[str, Any]) -> dict[str, Any]:
    """Restore the most recent focus snapshot (pops the stack)."""
    try:
        frame = STACK.pop()
        if frame is None:
            return {
                "tool": "focus_restore",
                "error": "no saved focus to restore",
                "result": None,
            }
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _restore_blocking, frame)
        if "error" in info:
            return {"tool": "focus_restore", "error": info["error"], "result": None}
        if not info.get("success"):
            return {
                "tool": "focus_restore",
                "error": (
                    f"could not switch back to {frame.title!r} "
                    f"(foreground: {info.get('foreground_title')!r})"
                ),
                "result": None,
            }
        depth = STACK.depth()
        return {
            "tool": "focus_restore",
            "title": frame.title,
            "restored": True,
            "strategy": info.get("strategy"),
            "depth": depth,
            "result": info.get("result", f"Restored focus to '{frame.title}'."),
            "reply": f"Restored focus to {frame.title}.",
            "deterministic": True,
        }
    except Exception as exc:
        logger.exception("focus_restore failed")
        return {"tool": "focus_restore", "error": f"restore failed: {exc}", "result": None}
