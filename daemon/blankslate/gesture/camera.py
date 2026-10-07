"""Camera capture thread (six-feature expansion, Phase 4).

cv2.VideoCapture in a dedicated capture thread feeding a single-slot
latest-frame queue (one worker thread consumes; if the detector is slower
than the camera, intermediate frames drop — latest always wins). Frames
are numpy arrays in RAM only: nothing here writes files (gate
source-asserted). release() runs on stop() — re-enable re-opens cleanly.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import cv2
import numpy as np

logger = logging.getLogger("blankslate.gesture.camera")

CAMERA_DEVICE = 0
FRAME_W, FRAME_H = 640, 480
MAX_READ_FAILURES = 15     # consecutive failed reads => device lost


class CameraCapture:
    """Single-slot camera pipeline: capture thread -> slot -> worker."""

    def __init__(
        self,
        on_frame: Callable[[np.ndarray], None],
        device: int = CAMERA_DEVICE,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        self._on_frame = on_frame
        self._on_lost = on_lost
        self._device = device
        self._slot: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cap: cv2.VideoCapture | None = None
        self._capture_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self.dropped = 0
        self.reads = 0
        self.active = False

    def start(self) -> bool:
        """Open the device and launch both threads. False if no camera."""
        if self.active:
            return True
        cap = cv2.VideoCapture(self._device, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self._device)
        if not cap.isOpened():
            logger.error("gesture camera: device %d failed to open", self._device)
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
        self._cap = cap
        self._stop.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="gesture-capture", daemon=True)
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="gesture-worker", daemon=True)
        self.active = True
        self._capture_thread.start()
        self._worker_thread.start()
        logger.info("gesture camera opened (device=%d)", self._device)
        return True

    def stop(self) -> None:
        """Signal both threads, join, release the device. Idempotent."""
        if self._cap is None and not self.active:
            return
        self._stop.set()
        for t in (self._capture_thread, self._worker_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                logger.exception("gesture camera release failed")
            self._cap = None
        self._capture_thread = None
        self._worker_thread = None
        self.active = False
        logger.info("gesture camera released (reads=%d dropped=%d)",
                    self.reads, self.dropped)

    def _capture_loop(self) -> None:
        failures = 0
        cap = self._cap
        assert cap is not None
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= MAX_READ_FAILURES:
                    logger.error("gesture camera: device lost after %d reads",
                                 self.reads)
                    break
                time.sleep(0.05)
                continue
            failures = 0
            self.reads += 1
            with self._lock:
                if self._slot is not None:
                    self.dropped += 1
                self._slot = frame
            time.sleep(0.001)
        # Capture ended (stop() or device loss) — notify only on loss.
        if not self._stop.is_set() and self._on_lost is not None:
            try:
                self._on_lost()
            except Exception:
                logger.exception("gesture camera on_lost callback failed")

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                frame = self._slot
                self._slot = None
            if frame is None:
                time.sleep(0.005)
                continue
            try:
                self._on_frame(frame)
            except Exception:
                logger.exception("gesture frame handling failed")

    def stats(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reads": self.reads,
            "dropped": self.dropped,
            "device": self._device,
        }
