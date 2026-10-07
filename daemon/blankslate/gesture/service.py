"""Gesture service lifecycle (six-feature expansion, Phase 4).

Off by default (config gesture_enabled=False); explicit opt-in via config
or the set_gesture_mode IPC message. Owns the camera thread + detector
and translates detected gestures through the fixed action map:

    GESTURE_ACTIONS = {"wave": "hide_overlay"}   # reuses the Phase 1 pill

Wire messages broadcast on the shared channel:
    {"type": "camera_state", "active": bool}     # start/stop/enable/disable
    {"type": "gesture_event", "gesture", "action"}  # action may be null

Frames live in RAM only; this module performs no file I/O (gate
source-asserted alongside camera.py/gestures.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("blankslate.gesture")

GESTURE_ACTIONS: dict[str, str] = {"wave": "hide_overlay"}
KNOWN_GESTURES = ("wave", "palm")

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


class GestureService:
    """Camera + detector lifecycle bound to the IPC broadcast channel."""

    def __init__(self, cfg: Any, emit: EmitFn) -> None:
        self._cfg = cfg
        self._emit = emit
        self._enabled = bool(getattr(cfg, "gesture_enabled", False))
        self._camera: Any = None
        self._detector: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.active = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def set_enabled(self, enabled: bool) -> dict[str, Any]:
        """Enable/disable the camera. Broadcasts camera_state either way."""
        if enabled:
            return await self.enable()
        return await self.disable()

    async def enable(self) -> dict[str, Any]:
        if self.active and self._camera is not None:
            return {"enabled": True, "active": True}
        from blankslate.gesture.camera import CameraCapture
        from blankslate.gesture.gestures import GestureDetector

        self._loop = asyncio.get_running_loop()
        self._detector = GestureDetector()
        camera = CameraCapture(self._on_frame, on_lost=self._on_camera_lost)
        ok = await self._loop.run_in_executor(None, camera.start)
        self._camera = camera if ok else None
        self.active = bool(ok)
        self._enabled = True
        await self._broadcast_state()
        if not ok:
            logger.error("gesture enable failed: no camera available")
        return {"enabled": True, "active": bool(ok)}

    async def disable(self) -> dict[str, Any]:
        camera, self._camera = self._camera, None
        was_active = self.active or camera is not None
        self.active = False
        self._enabled = False
        self._detector = None
        if camera is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, camera.stop)
        if was_active:
            await self._broadcast_state()
        return {"enabled": False, "active": False}

    async def shutdown(self) -> None:
        """Daemon-stop path: release the device without extra broadcasts."""
        camera, self._camera = self._camera, None
        self.active = False
        if camera is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, camera.stop)

    # ── detection path (camera worker thread -> event loop) ──────────────

    def _on_frame(self, frame: Any) -> None:
        if self._detector is None or self._loop is None:
            return
        for name in self._detector.feed(frame):
            logger.info("gesture detected: %s", name)
            asyncio.run_coroutine_threadsafe(
                self.handle_gesture(name), self._loop)

    def _on_camera_lost(self) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._camera_lost(), self._loop)

    async def _camera_lost(self) -> None:
        logger.error("gesture camera lost — device stopped")
        camera, self._camera = self._camera, None
        self.active = False
        if camera is not None:
            await asyncio.get_running_loop().run_in_executor(None, camera.stop)
        await self._broadcast_state()

    # ── action mapping (detector path AND gate-inject path share this) ───

    async def handle_gesture(self, name: str) -> dict[str, Any]:
        if name not in KNOWN_GESTURES:
            return {"error": f"unknown gesture: {name}"}
        action: str | None = GESTURE_ACTIONS.get(name)
        await self._emit({
            "type": "gesture_event",
            "gesture": name,
            "action": action,
        })
        if action == "hide_overlay":
            from blankslate import overlay as overlay_mod

            result = await overlay_mod.hide_pill()
            if "error" in result:
                logger.warning("wave->hide_overlay failed: %s", result["error"])
        logger.info("gesture_event: %s -> %s", name, action)
        return {"gesture": name, "action": action}

    async def _broadcast_state(self) -> None:
        await self._emit({"type": "camera_state", "active": self.active})
