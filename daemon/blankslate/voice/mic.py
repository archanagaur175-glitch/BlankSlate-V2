"""Microphone capture with WebRTC VAD — runs in its own thread.

Isolated from the main IPC loop with its own try/except boundary.
An audio failure must never take down the daemon.

Exports MicCapture which provides:
  - start(on_speech_chunk) — begins recording in background thread
  - stop() — stops recording
  - is_active — property
"""

from __future__ import annotations

import collections
import io
import logging
import math
import struct
import threading
import time
import wave
from typing import Callable

import numpy as np

logger = logging.getLogger("blankslate.voice.mic")

# Voice Activity Detection defaults (overridden at runtime)
FRAME_DURATION_MS = 30     # 30ms frames (WebRTC requirement: 10, 20, or 30)
SAMPLE_RATE = 16000
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 480 samples
SILENCE_THRESHOLD_FRAMES = 30   # ~900ms of silence → end of utterance
MIN_SPEECH_FRAMES = 5           # ~150ms minimum speech to count
PRE_SPEECH_BUFFER_FRAMES = 15   # ~450ms of audio before speech onset
AUDIO_LEVEL_LOG_INTERVAL = 20   # log audio level every N frames (~600ms)
AUDIO_LEVEL_EMIT_INTERVAL = 3   # emit audio_level every N frames (~90ms)


class MicCapture:
    """Microphone capture with VAD-triggered chunking.

    Usage:
        capture = MicCapture()
        capture.start(on_chunk=lambda audio_bytes: ...)
        ...
        capture.stop()
    """

    def __init__(
        self,
        device: int | None = None,
        on_speech_chunk: Callable[[bytes], None] | None = None,
        gain: float = 1.0,
        vad_aggressiveness: int = 1,
        on_audio_level: Callable[[float], None] | None = None,
        on_frame: Callable[[bytes, bool], None] | None = None,
    ) -> None:
        self._device = device
        self._on_chunk = on_speech_chunk
        self._gain = gain
        self._vad_aggressiveness = max(0, min(3, vad_aggressiveness))
        self._on_audio_level = on_audio_level
        # Phase 5 seam: optional per-frame (bytes, is_speech) hook for the
        # streaming pipeline (partial transcription + barge-in onset). None
        # (the PTT baseline path) means zero behavioral change.
        self._on_frame = on_frame
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active = False
        self._error: str | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def last_error(self) -> str | None:
        return self._error

    def update_gain(self, gain: float) -> None:
        self._gain = max(0.1, min(10.0, gain))

    def update_vad(self, aggressiveness: int) -> None:
        self._vad_aggressiveness = max(0, min(3, aggressiveness))

    def start(self, on_speech_chunk: Callable[[bytes], None] | None = None) -> None:
        """Start mic capture in a background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("MicCapture already running")
            return
        if on_speech_chunk is not None:
            self._on_chunk = on_speech_chunk
        self._stop_event.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="mic-capture")
        self._thread.start()
        logger.info("MicCapture thread started")

    def stop(self) -> None:
        """Stop mic capture."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._active = False
        logger.info("MicCapture stopped")

    def _run(self) -> None:
        """Main capture loop — runs in its own thread with own error boundary."""
        try:
            import sounddevice as sd
            import webrtcvad

            vad = webrtcvad.Vad(self._vad_aggressiveness)

            # Ring buffer for pre-speech audio
            pre_speech_buffer: collections.deque[bytes] = collections.deque(
                maxlen=PRE_SPEECH_BUFFER_FRAMES
            )
            speech_frames: list[bytes] = []
            silence_count = 0
            in_speech = False
            frame_count = 0

            self._active = True
            dev_name = "default" if self._device is None else f"device={self._device}"
            logger.info(
                "MicCapture: listening (rate=%d, frame=%dms, vad=%d, gain=%.1f, mic=%s)",
                SAMPLE_RATE, FRAME_DURATION_MS, self._vad_aggressiveness,
                self._gain, dev_name,
            )

            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SIZE,
                device=self._device,
            ) as stream:
                while not self._stop_event.is_set():
                    data, overflowed = stream.read(FRAME_SIZE)
                    if overflowed:
                        logger.warning("MicCapture: audio overflow")

                    # Apply software gain
                    if self._gain != 1.0:
                        amplified = np.clip(
                            data[:, 0].astype(np.float32) * self._gain,
                            -32768, 32767,
                        ).astype(np.int16)
                    else:
                        amplified = data[:, 0]

                    frame_bytes = amplified.tobytes()
                    frame_count += 1

                    # Compute audio level for HUD + diagnostics
                    rms = math.sqrt(float(np.mean(amplified.astype(np.float64) ** 2)))
                    db = 20 * math.log10(max(rms, 1)) - 90  # rough dBFS
                    level = min(1.0, rms / 4096)  # normalize to 0..1

                    # Log periodically
                    if frame_count % AUDIO_LEVEL_LOG_INTERVAL == 0:
                        logger.info("MicCapture: level %.1f dBFS (rms=%d, gain=%.1f)", db, int(rms), self._gain)

                    # Emit audio_level to HUD
                    if self._on_audio_level and frame_count % AUDIO_LEVEL_EMIT_INTERVAL == 0:
                        try:
                            self._on_audio_level(level)
                        except Exception:
                            pass

                    # WebRTC VAD expects specific frame sizes
                    try:
                        is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)
                    except Exception:
                        # Invalid frame (wrong size etc.) — skip
                        if self._on_frame:
                            try:
                                self._on_frame(frame_bytes, False)
                            except Exception:
                                logger.exception("MicCapture: on_frame callback failed")
                        pre_speech_buffer.append(frame_bytes)
                        continue

                    # Phase 5 seam: hand every classified frame to the
                    # streaming pipeline (partial STT + barge-in onset).
                    if self._on_frame:
                        try:
                            self._on_frame(frame_bytes, is_speech)
                        except Exception:
                            logger.exception("MicCapture: on_frame callback failed")

                    if is_speech:
                        if not in_speech:
                            # Speech onset — flush pre-speech buffer
                            in_speech = True
                            silence_count = 0
                            speech_frames = list(pre_speech_buffer)
                            pre_speech_buffer.clear()
                            logger.info("MicCapture: SPEECH ONSET detected")
                        speech_frames.append(frame_bytes)
                        silence_count = 0
                    else:
                        pre_speech_buffer.append(frame_bytes)
                        if in_speech:
                            silence_count += 1
                            speech_frames.append(frame_bytes)  # include trailing silence
                            if silence_count >= SILENCE_THRESHOLD_FRAMES:
                                # Speech offset — deliver chunk
                                in_speech = False
                                total_silence = len(speech_frames) - sum(
                                    1 for f in speech_frames
                                    if vad.is_speech(f, SAMPLE_RATE)
                                )
                                actual_speech = len(speech_frames) - total_silence
                                if actual_speech >= MIN_SPEECH_FRAMES:
                                    audio_bytes = b"".join(speech_frames)
                                    dur = len(speech_frames) * FRAME_DURATION_MS / 1000
                                    logger.info(
                                        "MicCapture: SPEECH CHUNK ready (%d frames, %.1fs) → sending to STT",
                                        len(speech_frames), dur,
                                    )
                                    if self._on_chunk:
                                        try:
                                            self._on_chunk(audio_bytes)
                                        except Exception:
                                            logger.exception("MicCapture: on_speech_chunk callback failed")
                                else:
                                    logger.debug("MicCapture: speech too short (%d frames), discarding", actual_speech)
                                speech_frames = []
                                silence_count = 0

        except Exception as exc:
            self._error = f"MicCapture fatal: {exc}"
            logger.exception("MicCapture thread crashed")
        finally:
            self._active = False
            logger.info("MicCapture thread ended")


def audio_bytes_to_wav(raw_pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16-bit PCM in a WAV container (in-memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(raw_pcm)
    return buf.getvalue()
