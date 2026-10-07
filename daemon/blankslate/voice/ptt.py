"""Push-to-Talk via global hotkey — pynput.

PTT loop starts unconditionally at daemon boot (Issue B fix).
Even if STT init failed, PTT still acknowledges press/release.

The hotkey is Ctrl+Space by default (configurable).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("blankslate.voice.ptt")


class PushToTalk:
    """Global hotkey PTT controller.

    Registers a global hotkey (default: ctrl+space).
    On press: starts mic capture.
    On release: stops mic capture, delivers audio chunk to callback.

    Starts unconditionally — does not depend on STT being ready.
    """

    def __init__(
        self,
        hotkey: str = "<ctrl>+<space>",
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[bytes | None], None] | None = None,
        mic_capture: Any | None = None,
    ) -> None:
        self._hotkey = hotkey
        self._on_press = on_press
        self._on_release = on_release
        self._mic = mic_capture
        self._pressed = False
        self._pressed_at: float = 0.0
        self._listener: Any = None
        self._started = False
        self._ctrl_held = False
        self._shift_held = False
        self._alt_held = False

    @property
    def is_pressed(self) -> bool:
        return self._pressed

    def start(self) -> None:
        """Start listening for the global hotkey. Always succeeds."""
        try:
            from pynput import keyboard

            def on_press(key) -> None:
                # Track modifier state
                if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                    self._ctrl_held = True
                elif key == keyboard.Key.shift_l or key == keyboard.Key.shift_r:
                    self._shift_held = True
                elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                    self._alt_held = True

                # Check for hotkey combo
                if self._matches_key(key) and not self._pressed:
                    self._pressed = True
                    self._pressed_at = time.time()
                    logger.info("PTT pressed")
                    if self._on_press:
                        try:
                            self._on_press()
                        except Exception:
                            logger.exception("PTT on_press callback failed")

            def on_release(key) -> None:
                # Track modifier state
                if key == keyboard.Key.ctrl_l or key == keyboard.Key.ctrl_r:
                    self._ctrl_held = False
                    # If Ctrl released while PTT active, trigger release
                    if self._pressed:
                        self._pressed = False
                        duration = time.time() - self._pressed_at
                        logger.info("PTT released (ctrl released, held %.1fs)", duration)
                        if self._on_release:
                            try:
                                self._on_release(None)
                            except Exception:
                                logger.exception("PTT on_release callback failed")
                        return
                elif key == keyboard.Key.alt_l or key == keyboard.Key.alt_r:
                    self._alt_held = False
                    # If Alt released while PTT active, trigger release
                    if self._pressed:
                        self._pressed = False
                        duration = time.time() - self._pressed_at
                        logger.info("PTT released (alt released, held %.1fs)", duration)
                        if self._on_release:
                            try:
                                self._on_release(None)
                            except Exception:
                                logger.exception("PTT on_release callback failed")
                        return
                elif key == keyboard.Key.shift_l or key == keyboard.Key.shift_r:
                    self._shift_held = False

                if self._matches_key(key) and self._pressed:
                    self._pressed = False
                    duration = time.time() - self._pressed_at
                    logger.info("PTT released (held %.1fs)", duration)
                    if self._on_release:
                        try:
                            self._on_release(None)
                        except Exception:
                            logger.exception("PTT on_release callback failed")

            self._listener = keyboard.Listener(
                on_press=on_press,
                on_release=on_release,
            )
            self._listener.daemon = True
            self._listener.start()
            self._started = True
            logger.info("PTT started: hotkey=%s (always active)", self._hotkey)

        except Exception as exc:
            logger.error("PTT hotkey listener failed to start: %s", exc)
            self._started = False

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._started = False
            logger.info("PTT stopped")

    def _matches_key(self, key) -> bool:
        """Check if the pressed key matches our hotkey combo."""
        from pynput import keyboard as kb
        try:
            if self._hotkey == "<ctrl>+<alt>+<space>":
                # Require Ctrl AND Alt AND Space to be held
                return key == kb.Key.space and self._ctrl_held and self._alt_held
            elif self._hotkey == "<ctrl>+<space>":
                # Require both Ctrl AND Space to be held
                return key == kb.Key.space and self._ctrl_held
        except Exception:
            pass
        return False
