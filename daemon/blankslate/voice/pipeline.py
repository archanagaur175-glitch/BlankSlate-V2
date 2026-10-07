"""Voice pipeline coordinator — ties mic, VAD, STT, PTT, wake word together.

Runs in its own error boundary. Audio/STT failures never crash the daemon.
PTT starts unconditionally at daemon boot (Issue B fix from v1).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable

from blankslate.config import DaemonConfig
from blankslate.voice.mic import MicCapture, audio_bytes_to_wav
from blankslate.voice.stt import STTEngine
from blankslate.voice.ptt import PushToTalk
from blankslate.voice.wakeword import detect_wake_word

logger = logging.getLogger("blankslate.voice")


class VoicePipeline:
    """Coordinator for mic → VAD → STT → intent pipeline.

    Key design:
    - PTT starts unconditionally (STT failure does NOT block PTT)
    - Mic capture in its own thread (audio failure does NOT crash daemon)
    - STT runs in thread pool (slow transcription does NOT block IPC)
    """

    def __init__(
        self,
        cfg: DaemonConfig,
        on_transcription: Callable[[str], Any] | None = None,
        ipc_state: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self._on_transcription = on_transcription
        self._ipc_state = ipc_state
        self._stt: STTEngine | None = None
        self._mic: MicCapture | None = None
        self._ptt: PushToTalk | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ptt_held = False
        self._wake_mode = cfg.ollama_model  # always require wake word in voice mode
        # Shared-capture subscribers (Phase 5 session audio): the mic thread
        # remains the ONE owner of the input stream; subscribers only get a
        # copy of each finalized VAD speech chunk.
        self._chunk_subs: list[Callable[[bytes], None]] = []
        self._subs_lock = threading.Lock()
        self._session_mic = False

    # ── shared-capture subscribers (Phase 5, design doc section 8) ───────

    def register_chunk_subscriber(self, cb: Callable[[bytes], None]) -> None:
        """Subscribe to VAD speech chunks (cb must not block the mic thread)."""
        with self._subs_lock:
            if cb not in self._chunk_subs:
                self._chunk_subs.append(cb)

    def unregister_chunk_subscriber(self, cb: Callable[[bytes], None]) -> None:
        with self._subs_lock:
            if cb in self._chunk_subs:
                self._chunk_subs.remove(cb)

    def session_mic_acquire(self) -> None:
        """Timeline session audio keeps the shared mic running (refcount-ish)."""
        self._session_mic = True
        if self._mic and not self._mic.is_active:
            logger.info("Voice pipeline: starting mic for timeline session audio")
            self._mic.start()

    def session_mic_release(self) -> None:
        """Session released the mic — stop it only if PTT does not hold it."""
        self._session_mic = False
        if self._mic and self._mic.is_active and not self._ptt_held:
            logger.info("Voice pipeline: releasing mic (timeline session audio off)")
            threading.Thread(target=self._mic.stop, daemon=True).start()

    def transcribe_shared(self, audio_bytes: bytes) -> Any:
        """Transcribe a chunk with the shared STT engine (None if not ready)."""
        if self._stt is None or not self._stt.is_ready:
            return None
        return self._stt.transcribe(audio_bytes)

    def inject_chunk_for_tests(self, audio_bytes: bytes) -> None:
        """TEST-ONLY: feed PCM into the same boundary mic.py calls after VAD.

        Active only when BS_GATE_HOOKS=1 (set exclusively by test spawns in
        env). In normal operation the env var is unset and this is a refused
        no-op - the daemon's speech path is never wire-writable.
        """
        if os.environ.get("BS_GATE_HOOKS") != "1":
            logger.warning("voice: test chunk injection refused (BS_GATE_HOOKS unset)")
            return
        logger.info(
            "GATE HOOK: injecting %d bytes of test speech PCM at the VAD chunk boundary",
            len(audio_bytes),
        )
        self._on_speech_chunk(audio_bytes)

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start voice pipeline — PTT always starts, STT may be degraded."""
        self._loop = loop

        # ── STT init (may fail — that's OK) ──
        try:
            self._stt = STTEngine(
                model_name=self._cfg.stt_model,
                device=self._cfg.stt_device,
                compute_type=self._cfg.stt_compute_type,
                vocabulary_prompt=self._cfg.stt_vocabulary_prompt,
            )
            if self._stt.is_ready:
                logger.info("Voice pipeline: STT ready (model=%s)", self._cfg.stt_model)
            else:
                logger.warning("Voice pipeline: STT degraded (transcription unavailable)")
        except Exception as exc:
            logger.error("Voice pipeline: STT init failed: %s", exc)
            self._stt = None

        # ── Mic capture (always start) ──
        self._mic = MicCapture(
            device=self._cfg.mic_device,
            on_speech_chunk=self._on_speech_chunk,
            gain=self._cfg.mic_gain,
            vad_aggressiveness=self._cfg.vad_aggressiveness,
            on_audio_level=self._broadcast_audio_level,
        )
        mic_label = f"device={self._cfg.mic_device}" if self._cfg.mic_device is not None else "system default"
        logger.info("Voice pipeline: mic = %s", mic_label)

        # ── PTT (always starts, regardless of STT state — Issue B fix) ──
        self._ptt = PushToTalk(
            hotkey=self._cfg.ptt_hotkey,
            on_press=self._ptt_pressed,
            on_release=self._ptt_released,
            mic_capture=self._mic,
        )
        self._ptt.start()
        logger.info("Voice pipeline: PTT started (always active)")

    def stop(self) -> None:
        if self._ptt:
            self._ptt.stop()
        if self._mic:
            self._mic.stop()
        logger.info("Voice pipeline stopped")

    def _ptt_pressed(self) -> None:
        """PTT pressed — start mic capture."""
        self._ptt_held = True
        logger.info("PTT press → starting mic capture")
        if self._mic:
            self._mic.start()

    def _ptt_released(self, _audio: bytes | None) -> None:
        """PTT released — mic keeps running until VAD finalizes the chunk."""
        self._ptt_held = False
        logger.info("PTT release → mic keeps running until speech chunk finalizes")

    def _on_speech_chunk(self, audio_bytes: bytes) -> None:
        """Called from mic thread when speech chunk is ready."""
        # Fan out to session-audio subscribers BEFORE the voice path (they
        # must never block or alter this callback — PTT wins the utterance).
        with self._subs_lock:
            subs = list(self._chunk_subs)
        for cb in subs:
            try:
                cb(audio_bytes)
            except Exception:
                logger.exception("voice chunk subscriber callback failed")

        # Capture whether PTT initiated this recording session
        ptt_initiated = self._ptt_held

        # Stop mic after chunk delivery if PTT is not held AND no timeline
        # session audio is holding the shared capture (Phase 5 arbitration).
        # Must schedule in a separate thread — we're inside the mic thread here
        if not self._ptt_held and not self._session_mic and self._mic:
            logger.info("Speech chunk delivered → scheduling mic stop")
            threading.Thread(target=self._mic.stop, daemon=True).start()

        if self._stt is None or not self._stt.is_ready:
            logger.warning("STT not ready, discarding audio chunk (%d bytes)", len(audio_bytes))
            return

        # Transcribe in a thread to avoid blocking the mic callback
        thread = threading.Thread(
            target=self._transcribe_and_dispatch,
            args=(audio_bytes, ptt_initiated),
            daemon=True,
            name="stt-transcribe",
        )
        thread.start()

    def _transcribe_and_dispatch(self, audio_bytes: bytes, ptt_initiated: bool = False) -> None:
        """Transcribe audio and dispatch to intent handler (runs in thread)."""
        try:
            result = self._stt.transcribe(audio_bytes)
            if result is None:
                logger.info("STT: no speech detected in chunk")
                return

            logger.info("STT: '%s' (lang=%s, conf=%.2f, latency=%.0fms)", result.text, result.language, result.confidence, result.duration_ms)

            # Broadcast transcription to HUD
            self._broadcast_transcription(result.text)

            if ptt_initiated:
                # PTT mode: skip wake word, dispatch directly
                logger.info("PTT mode → dispatching without wake word: '%s'", result.text)
                self._dispatch(result.text)
            else:
                # Ambient mode: require wake word
                matched, cleaned = detect_wake_word(result.text)
                if matched:
                    if cleaned:
                        logger.info("Wake word matched, dispatching: '%s'", cleaned)
                        self._dispatch(cleaned)
                    else:
                        logger.info("Wake word only, no command text")
                else:
                    logger.debug("No wake word in: '%s'", result.text)

        except Exception:
            logger.exception("STT transcription thread failed")

    def _broadcast_transcription(self, text: str) -> None:
        """Broadcast transcription text to HUD via IPC (thread-safe)."""
        if not self._loop or not self._ipc_state:
            return
        from blankslate.ipc.server import broadcast
        asyncio.run_coroutine_threadsafe(
            broadcast(self._ipc_state, {"type": "transcription", "text": text}),
            self._loop,
        )

    def _broadcast_audio_level(self, level: float) -> None:
        """Broadcast audio level to HUD via IPC (thread-safe, called from mic thread)."""
        if not self._loop or not self._ipc_state:
            return
        from blankslate.ipc.server import broadcast
        asyncio.run_coroutine_threadsafe(
            broadcast(self._ipc_state, {"type": "audio_level", "level": level}),
            self._loop,
        )

    def update_mic_gain(self, gain: float) -> None:
        """Update mic gain at runtime (called from IPC handler)."""
        if self._mic:
            self._mic.update_gain(gain)
            logger.info("Mic gain updated: %.1f", gain)

    def update_vad_aggressiveness(self, aggressiveness: int) -> None:
        """Update VAD aggressiveness at runtime (called from IPC handler)."""
        if self._mic:
            self._mic.update_vad(aggressiveness)
            logger.info("VAD aggressiveness updated: %d", aggressiveness)

    def _dispatch(self, text: str) -> None:
        """Dispatch transcribed text to the intent handler via the event loop."""
        if self._loop and self._on_transcription:
            asyncio.run_coroutine_threadsafe(
                self._safe_dispatch(text),
                self._loop,
            )

    async def _safe_dispatch(self, text: str) -> None:
        """Dispatch with error boundary."""
        try:
            if self._on_transcription:
                await self._on_transcription(text)
        except Exception:
            logger.exception("Voice dispatch to intent handler failed")
