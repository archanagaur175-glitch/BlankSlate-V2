"""Gesture detectors (six-feature expansion, Phase 4) — OpenCV only.

Deterministic, stateless-per-call API over in-memory frames: HSV skin
mask localizes the hand, Farneback optical flow confirms lateral motion,
a Schmitt-trigger on the blob centroid counts oscillation reversals.

  wave : lateral oscillation >= 6 reversals (>= 3 cycles) within
         WAVE_WINDOW_S (~1.5 s), each reversal flow-validated.
  palm : large centered skin region sustained PALM_SUSTAIN_S.

Frames never touch disk — feed() takes numpy arrays and returns gesture
names; there is no file I/O in this module (gate source-asserted).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import cv2
import numpy as np

# HSV skin envelope (OpenCV hue 0-179).
SKIN_LOWER = (0, 40, 60)
SKIN_UPPER = (25, 170, 255)

MIN_BLOB_PX = 900           # below this the "hand" is noise
WAVE_WINDOW_S = 1.5         # >= 3 cycles in ~1.5 s (spec)
WAVE_HALF_SWINGS = 6        # 6 reversals = 3 full cycles
MIN_SWING_PX = 30           # Schmitt-trigger band around the rolling center
FLOW_MIN_PX = 0.75          # mean |flow_x| over skin at a reversal
PALM_MIN_RATIO = 0.18       # skin area as a fraction of the frame
PALM_CENTER = (0.25, 0.75)  # centroid must sit in the central box
PALM_SUSTAIN_S = 0.5
COOLDOWN_S = 2.0            # per-gesture re-fire suppression

KNOWN_GESTURES = ("wave", "palm")


class GestureDetector:
    """Feed frames with explicit timestamps for deterministic unit tests."""

    def __init__(self) -> None:
        self._prev_gray: np.ndarray | None = None
        self._samples: deque[tuple[float, float]] = deque()   # (ts, cx)
        self._reversals: deque[float] = deque()
        self._state = 0                 # -1 | 0 | +1 (Schmitt)
        self._palm_since: float | None = None
        self._palm_fired = False
        self._last_fired: dict[str, float] = {}
        self.frames = 0

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def feed(self, frame: np.ndarray, ts: float | None = None) -> list[str]:
        """Process one BGR frame; returns gesture names fired on this call."""
        now = time.monotonic() if ts is None else float(ts)
        self.frames += 1
        fired: list[str] = []

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array(SKIN_LOWER), np.array(SKIN_UPPER))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                np.ones((5, 5), np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        largest = max(contours, key=cv2.contourArea) if contours else None
        blob_ok = largest is not None and cv2.contourArea(largest) >= MIN_BLOB_PX

        # Farneback flow on the full pair (masked read afterwards); needs
        # the previous frame even when this frame has no blob.
        mean_dx = 0.0
        if self._prev_gray is not None:
            flow = cv2.calcOpticalFlowFarneback(
                self._prev_gray, gray, None,
                0.5, 3, 15, 3, 5, 1.2, 0,
            )
            skin = mask > 0
            if skin.any():
                mean_dx = float(flow[..., 0][skin].mean())
        self._prev_gray = gray

        if not blob_ok:
            # No hand: keep the reversal history (the 1.5 s window prunes
            # staleness) but the palm sustain resets.
            self._palm_since = None
            self._palm_fired = False
            self._prune(now)
            return fired

        moments = cv2.moments(largest)
        if moments["m00"] == 0:
            return fired
        cx = moments["m10"] / moments["m00"]
        cy = moments["m01"] / moments["m00"]
        h, w = frame.shape[:2]
        area_ratio = float(cv2.contourArea(largest)) / float(w * h)

        self._samples.append((now, cx))
        self._prune(now)

        # ── wave: Schmitt trigger on the rolling center + flow gate ──────
        center = float(np.mean([x for _t, x in self._samples]))
        new_state = 0
        if cx >= center + MIN_SWING_PX:
            new_state = 1
        elif cx <= center - MIN_SWING_PX:
            new_state = -1
        if new_state != 0 and new_state != self._state:
            if self._state == 0:
                self._state = new_state          # establishment: no count
            else:
                flow_dir = 1 if mean_dx > 0 else -1
                if abs(mean_dx) >= FLOW_MIN_PX and flow_dir == new_state:
                    self._reversals.append(now)
                self._state = new_state
                if (len(self._reversals) >= WAVE_HALF_SWINGS
                        and self._ready("wave", now)):
                    fired.append("wave")
                    self._last_fired["wave"] = now
                    self._reversals.clear()

        # ── palm: large centered skin region, sustained ──────────────────
        cx_lo, cx_hi = PALM_CENTER[0] * w, PALM_CENTER[1] * w
        cy_lo, cy_hi = PALM_CENTER[0] * h, PALM_CENTER[1] * h
        centered = cx_lo <= cx <= cx_hi and cy_lo <= cy <= cy_hi
        if area_ratio >= PALM_MIN_RATIO and centered:
            if self._palm_since is None:
                self._palm_since = now
            if (not self._palm_fired
                    and now - self._palm_since >= PALM_SUSTAIN_S
                    and self._ready("palm", now)):
                fired.append("palm")
                self._last_fired["palm"] = now
                self._palm_fired = True
        else:
            self._palm_since = None
            self._palm_fired = False

        return fired

    def _prune(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > WAVE_WINDOW_S:
            self._samples.popleft()
        while self._reversals and now - self._reversals[0] > WAVE_WINDOW_S:
            self._reversals.popleft()

    def _ready(self, name: str, now: float) -> bool:
        last = self._last_fired.get(name)
        return last is None or (now - last) >= COOLDOWN_S

    def stats(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "reversals": len(self._reversals),
            "state": self._state,
            "last_fired": dict(self._last_fired),
        }
