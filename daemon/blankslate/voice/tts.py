"""Text-to-Speech — Pocket TTS with FRIDAY-like voice cloning.

Uses Kyutai Pocket TTS (100M params) for fast CPU inference with
zero-shot voice cloning from a clean reference sample.

Phase 5 (full-duplex): playback is cancellable — `stop()` interrupts an
in-flight synthesize/playback, `is_speaking` exposes the active window,
and `on_state(speaking, interrupted)` reports transitions (wired to the
tts_state broadcast only in streaming mode; PTT mode passes no hook).
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time
from typing import Callable

import numpy as np
import soundfile as sf

logger = logging.getLogger("blankslate.voice.tts")

_REF_WAV = os.path.join(os.path.dirname(__file__), "..", "..", "voice_ref_friday_clean.wav")


class TTS:
    """Pocket TTS voice-cloned TTS with FRIDAY-like voice.

    - `is_ready` is True once the Pocket TTS model loads.
    - `synthesize(text)` returns raw WAV bytes, or None on failure.
    - `play(wav_bytes)` streams WAV through sounddevice.
    - `speak(text)` = synthesize + play, best-effort, never raises.
    - `stop()` / `is_speaking` / `on_state` (Phase 5 barge-in): speak()
      turns are serialized on `_speak_lock`, so a newer reply can never
      overlap another thread's playback.
    """

    def __init__(
        self,
        device: int | None = None,
        on_state: Callable[[bool, bool], None] | None = None,
    ) -> None:
        self._device = device
        self._on_state = on_state
        self._ready = False
        self._model = None
        self._voice_state = None
        self._speaking = False
        self._stop_evt = threading.Event()
        self._speak_lock = threading.Lock()
        self._play_stream: object | None = None
        try:
            self._load_model()
            self._ready = True
            logger.info("TTS ready: Pocket TTS (FRIDAY voice clone)")
        except Exception as exc:
            logger.error("Pocket TTS init failed (degraded): %s", exc)
            self._ready = False

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def is_speaking(self) -> bool:
        """True while a speak() turn is active (synthesis or playback)."""
        return self._speaking

    def stop(self) -> None:
        """Cancel an in-flight speak() (barge-in). Safe when idle: the
        stop latch is cleared at the start of every speak() turn.

        Only the playback OutputStream is stopped — never the global
        sounddevice stop() call, which would also kill the mic's
        InputStream (the mic must stay the single continuous capture
        owner in streaming mode)."""
        self._stop_evt.set()
        stream = self._play_stream
        if stream is not None:
            try:
                stream.stop()  # type: ignore[union-attr]
            except Exception:
                pass
        if self._speaking:
            self._speaking = False
            self._emit(False, True)

    def _emit(self, speaking: bool, interrupted: bool) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(speaking, interrupted)
        except Exception:
            logger.exception("TTS on_state callback failed")

    def _load_model(self) -> None:
        """Load Pocket TTS model and compute voice state from reference."""
        from pocket_tts import TTSModel

        logger.info("Loading Pocket TTS model...")
        t0 = time.time()
        self._model = TTSModel.load_model()
        logger.info("Pocket TTS model loaded in %.1fs", time.time() - t0)

        logger.info("Computing voice state from reference...")
        t0 = time.time()
        self._voice_state = self._model.get_state_for_audio_prompt(_REF_WAV)
        logger.info("Voice state computed in %.1fs", time.time() - t0)

    def synthesize(self, text: str) -> bytes | None:
        """Return WAV bytes for `text`, or None on failure."""
        if not self._ready or not text:
            return None
        try:
            t0 = time.time()
            audio_tensor = self._model.generate_audio(self._voice_state, text)
            elapsed = time.time() - t0

            audio_np = audio_tensor.numpy()
            duration = len(audio_np) / self._model.sample_rate
            rtf = elapsed / duration if duration > 0 else float("inf")
            logger.info(
                "Pocket TTS synthesized in %.1fs (%.1fs audio, RTF=%.2f)",
                elapsed, duration, rtf,
            )

            buf = io.BytesIO()
            sf.write(buf, audio_np, self._model.sample_rate, format="WAV", subtype="PCM_16")
            return buf.getvalue()
        except Exception as exc:
            logger.error("Pocket TTS synthesis error: %s", exc)
            return None

    def play(self, wav_bytes: bytes) -> None:
        """Play raw WAV bytes through an explicit OutputStream (blocks
        until done or stop()).

        Phase 5: a dedicated output stream (not sd.play) so stop() can
        interrupt playback without touching any other sounddevice stream,
        plus a per-chunk stop check for bounded interruption latency.
        """
        if not wav_bytes:
            return
        if self._stop_evt.is_set():
            return
        try:
            import wave
            import sounddevice as sd

            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                sr = wf.getframerate()
                n = wf.getnframes()
                raw = wf.readframes(n)
                audio = np.frombuffer(raw, dtype=np.int16) if wf.getsampwidth() == 2 else np.frombuffer(raw, dtype=np.float32)
                if wf.getnchannels() == 2:
                    audio = audio.reshape(-1, 2).mean(axis=1).astype(audio.dtype)

            stream = sd.OutputStream(
                samplerate=sr, channels=1, dtype=audio.dtype, device=self._device,
            )
            self._play_stream = stream
            chunk = max(1, int(sr // 4))  # ~250 ms per chunk
            try:
                stream.start()
                for i in range(0, len(audio), chunk):
                    if self._stop_evt.is_set():
                        break
                    stream.write(audio[i:i + chunk])
            finally:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
                if self._play_stream is stream:
                    self._play_stream = None
        except Exception as exc:
            logger.error("TTS playback error: %s", exc)

    def speak(self, text: str) -> None:
        """Synthesize + play, best-effort. Never raises.

        Phase 5: one speak turn at a time (serialized), cancelable via
        stop(); emits on_state(True, False) at turn start and
        on_state(False, interrupted=stop-latched) at turn end.
        """
        if not self._ready or not text:
            return
        with self._speak_lock:
            self._stop_evt.clear()
            self._speaking = True
            self._emit(True, False)
            try:
                wav = self.synthesize(text)
                if wav and not self._stop_evt.is_set():
                    self.play(wav)
            except Exception as exc:
                logger.error("TTS speak error: %s", exc)
            finally:
                if self._speaking:
                    interrupted = self._stop_evt.is_set()
                    self._speaking = False
                    self._emit(False, interrupted)
