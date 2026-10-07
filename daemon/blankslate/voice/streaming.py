"""Full-duplex streaming voice pipeline (six-feature expansion, Phase 5).

Additive mode selected at startup: cfg.voice_mode == "streaming" picks
this class; the default "ptt" keeps blankslate/voice/pipeline.py byte-
identical (its behavior is re-proven every phase by test_tl5_audio.py).

What "full-duplex" means here:
- Capture: ONE continuous MicCapture (the existing primitive — it stays
  the single InputStream owner; PTT hotkey still works and force-dispatches).
- Incremental transcription: while a VAD segment is open, the growing PCM
  is re-transcribed every cfg.streaming_partial_interval_s and broadcast
  as {"type": "transcription_partial", "text", "final": False}. The
  finalized chunk still produces the normal {"type": "transcription"}
  broadcast and the exact same wake-word / PTT dispatch rules as PTT mode.
- Barge-in: while TTS is speaking (is_speaking window exposed by
  voice/tts.py), a sustained speech onset — >= cfg.streaming_barge_in_ms
  of frames that are both VAD-speech AND level >= cfg.streaming_barge_min_level
  (echo guard: the raised level bar keeps the daemon's own TTS pickup from
  counting as a user interruption) — stops TTS immediately; the interrupted
  turn is reported through tts_state {speaking:false, interrupted:true}
  (emitted by tts.py's on_state hook) and any still-pending reply task for
  the superseded turn is canceled.
- Gate path: inject_chunk_for_tests replays the injected post-VAD PCM as
  paced 30 ms frames through this SAME _on_frame segment logic (partials +
  barge onset fire for real), then finalizes at the chunk boundary — so the
  P5 gate proves the wire behavior without a human speaking on cue.

Known honest limitations:
- No acoustic echo cancellation: barge-in relies on the raised level bar.
  On this machine default output routes to the BT headset (whose own
  headphones are deaf to its mic — measured and documented in tl5), so TTS
  pickup at the mic is negligible; on loudspeakers a too-loud echo could
  self-interrupt (tunable via streaming_barge_min_level).
- An echo that survives VAD as a finalized chunk is NOT a barge-in (chunk
  arrival never triggers barge) and still passes through the wake-word
  gate before dispatch — same exposure PTT mode already has.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable

from blankslate.config import DaemonConfig
from blankslate.voice.mic import FRAME_DURATION_MS, MicCapture
from blankslate.voice.ptt import PushToTalk
from blankslate.voice.stt import STTEngine
from blankslate.voice.wakeword import detect_wake_word

logger = logging.getLogger("blankslate.voice.streaming")

_FRAME_BYTES = 960  # 480 samples (30 ms @ 16 kHz) * 2 bytes (int16)
_PRE_SILENCE_FRAMES = 15  # ~450 ms lead-in for the paced gate replay


def _frame_level(frame_bytes: bytes) -> float:
    """rms/4096 in [0, 1] — same normalization mic.py uses for audio_level."""
    import numpy as np

    if not frame_bytes:
        return 0.0
    arr = np.frombuffer(frame_bytes, dtype=np.int16)
    if len(arr) == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
    return min(1.0, rms / 4096)


class StreamingVoicePipeline:
    """Full-duplex coordinator: continuous mic -> partial STT -> judge.

    Duck-typed superset of the VoicePipeline surface main.py / ipc/server.py
    / timeline.service.py rely on: register_chunk_subscriber,
    unregister_chunk_subscriber, session_mic_acquire, session_mic_release,
    transcribe_shared, inject_chunk_for_tests, update_mic_gain,
    update_vad_aggressiveness, start(loop), stop().
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
        self._stt_lock = threading.Lock()
        self._mic: MicCapture | None = None
        self._ptt: PushToTalk | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ptt_held = False
        self._session_mic = False

        # Shared-capture subscribers (same fan-out contract as VoicePipeline)
        self._chunk_subs: list[Callable[[bytes], None]] = []
        self._subs_lock = threading.Lock()

        # Open-segment state for partial transcription
        self._seg_lock = threading.Lock()
        self._seg_frames: list[bytes] = []
        self._seg_open = False
        self._last_partial = 0.0
        self._partial_busy = False

        # Barge-in state
        self._tts_speaking = False
        self._onset_frames = 0
        self._barge_fired = False
        self._replay_active = False
        self._tts_control: Callable[[], Any] | None = None
        self._inflight: Any = None  # concurrent.futures.Future of last dispatch
        self._stopped = False

    # ── wiring (main.py streaming branch) ─────────────────────────────────

    def set_tts_control(self, provider: Callable[[], Any]) -> None:
        """Provider returns the process TTS instance (or None) without
        constructing one — used to stop playback on barge-in."""
        self._tts_control = provider

    def notify_tts_state(self, speaking: bool, interrupted: bool) -> None:
        """Called from tts.py's on_state hook (speak thread): arms/disarms
        the barge-in onset detector."""
        self._tts_speaking = speaking
        if speaking:
            self._onset_frames = 0
            self._barge_fired = False
        else:
            self._onset_frames = 0
            logger.info(
                "TTS state: speaking=%s interrupted=%s",
                speaking, interrupted,
            )

    # ── shared-capture subscribers (identical contract to VoicePipeline) ──

    def register_chunk_subscriber(self, cb: Callable[[bytes], None]) -> None:
        with self._subs_lock:
            if cb not in self._chunk_subs:
                self._chunk_subs.append(cb)

    def unregister_chunk_subscriber(self, cb: Callable[[bytes], None]) -> None:
        with self._subs_lock:
            if cb in self._chunk_subs:
                self._chunk_subs.remove(cb)

    def session_mic_acquire(self) -> None:
        """Timeline session audio: streaming already runs the mic 24/7."""
        self._session_mic = True
        if self._mic and not self._mic.is_active:
            logger.info("Streaming pipeline: restarting mic for session audio")
            self._mic.start()

    def session_mic_release(self) -> None:
        """Session released — the mic keeps running (continuous capture)."""
        self._session_mic = False
        logger.info("Streaming pipeline: session released (mic stays live)")

    def transcribe_shared(self, audio_bytes: bytes) -> Any:
        if self._stt is None or not self._stt.is_ready:
            return None
        with self._stt_lock:
            return self._stt.transcribe(audio_bytes)

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stopped = False

        # STT init (may fail — that's OK, same degradation as PTT mode)
        try:
            self._stt = STTEngine(
                model_name=self._cfg.stt_model,
                device=self._cfg.stt_device,
                compute_type=self._cfg.stt_compute_type,
                vocabulary_prompt=self._cfg.stt_vocabulary_prompt,
            )
            if self._stt.is_ready:
                logger.info("Streaming pipeline: STT ready (model=%s)", self._cfg.stt_model)
            else:
                logger.warning("Streaming pipeline: STT degraded (partial/final unavailable)")
        except Exception as exc:
            logger.error("Streaming pipeline: STT init failed: %s", exc)
            self._stt = None

        # Continuous mic — single InputStream owner, per-frame hook for
        # partials + barge onset. Starts immediately (full-duplex).
        self._mic = MicCapture(
            device=self._cfg.mic_device,
            on_speech_chunk=self._on_speech_chunk,
            gain=self._cfg.mic_gain,
            vad_aggressiveness=self._cfg.vad_aggressiveness,
            on_audio_level=self._broadcast_audio_level,
            on_frame=self._on_frame,
        )
        self._mic.start()
        mic_label = f"device={self._cfg.mic_device}" if self._cfg.mic_device is not None else "system default"
        logger.info("Streaming pipeline: continuous mic = %s", mic_label)

        # PTT hotkey (additive): force-dispatch, does NOT gate capture.
        self._ptt = PushToTalk(
            hotkey=self._cfg.ptt_hotkey,
            on_press=self._ptt_pressed,
            on_release=self._ptt_released,
            mic_capture=self._mic,
        )
        self._ptt.start()
        logger.info(
            "Streaming pipeline: full-duplex active (partials every %.2fs, "
            "barge-in %dms @ level>=%.2f, PTT force-dispatch additive)",
            self._cfg.streaming_partial_interval_s,
            self._cfg.streaming_barge_in_ms,
            self._cfg.streaming_barge_min_level,
        )

    def stop(self) -> None:
        self._stopped = True
        if self._ptt:
            self._ptt.stop()
        if self._mic:
            self._mic.stop()
        if self._tts_control:
            try:
                tts = self._tts_control()
                if tts is not None:
                    tts.stop()
            except Exception:
                logger.exception("streaming: TTS stop on shutdown failed")
        self._cancel_inflight()
        logger.info("Streaming pipeline stopped")

    def update_mic_gain(self, gain: float) -> None:
        if self._mic:
            self._mic.update_gain(gain)
            logger.info("Mic gain updated: %.1f", gain)

    def update_vad_aggressiveness(self, aggressiveness: int) -> None:
        if self._mic:
            self._mic.update_vad(aggressiveness)
            logger.info("VAD aggressiveness updated: %d", aggressiveness)

    # ── per-frame path (mic thread; also fed by the gate's paced replay) ──

    def _on_frame(self, frame_bytes: bytes, is_speech: bool,
                  from_replay: bool = False) -> None:
        """Every classified frame: maintain the open segment for partial
        transcription, then run the barge-in onset detector."""
        try:
            if is_speech:
                with self._seg_lock:
                    if not self._seg_open:
                        self._seg_open = True
                        self._seg_frames = []
                        self._last_partial = time.time()
                    self._seg_frames.append(frame_bytes)
                    due = (
                        not self._partial_busy
                        and (time.time() - self._last_partial)
                        >= self._cfg.streaming_partial_interval_s
                    )
                    if due:
                        self._partial_busy = True
                        self._last_partial = time.time()
                        snapshot = b"".join(self._seg_frames)
                    else:
                        snapshot = None
                if snapshot:
                    threading.Thread(
                        target=self._partial_worker,
                        args=(snapshot,),
                        daemon=True,
                        name="stt-partial",
                    ).start()
            else:
                with self._seg_lock:
                    if self._seg_open:
                        self._seg_frames.append(frame_bytes)

            self._maybe_barge(frame_bytes, is_speech, from_replay)
        except Exception:
            logger.exception("streaming: _on_frame failed")

    def _maybe_barge(self, frame_bytes: bytes, is_speech: bool,
                     from_replay: bool = False) -> None:
        """Sustained loud speech while TTS speaks -> barge-in (once per turn)."""
        if not self._tts_speaking or self._barge_fired:
            self._onset_frames = 0
            return
        if self._replay_active and not from_replay:
            return
        if is_speech and _frame_level(frame_bytes) >= self._cfg.streaming_barge_min_level:
            self._onset_frames += 1
            if self._onset_frames * FRAME_DURATION_MS >= self._cfg.streaming_barge_in_ms:
                self._barge_in()
        else:
            self._onset_frames = 0

    def _barge_in(self) -> None:
        self._barge_fired = True
        self._onset_frames = 0
        logger.info(
            "barge-in: sustained speech during TTS -> stopping playback "
            "+ canceling pending reply"
        )
        if self._tts_control:
            try:
                tts = self._tts_control()
                if tts is not None:
                    tts.stop()
            except Exception:
                logger.exception("streaming: TTS stop failed")
        # tts_state {speaking:false, interrupted:true} is emitted by
        # tts.py's own on_state hook inside speak()'s finally block.
        self._cancel_inflight()

    def _cancel_inflight(self) -> None:
        fut = self._inflight
        if fut is not None and not fut.done():
            try:
                fut.cancel()
                logger.info("streaming: in-flight reply task canceled")
            except Exception:
                logger.exception("streaming: inflight cancel failed")

    def _partial_worker(self, snapshot: bytes) -> None:
        try:
            if not snapshot or self._stt is None or not self._stt.is_ready:
                return
            with self._stt_lock:
                result = self._stt.transcribe(snapshot)
            if result is not None and result.text:
                self._broadcast_partial(result.text)
        except Exception:
            logger.exception("streaming: partial transcription failed")
        finally:
            self._partial_busy = False

    # ── finalize path (mic VAD chunk OR gate replay) ──────────────────────

    def _on_speech_chunk(self, audio_bytes: bytes) -> None:
        """Finalized VAD chunk: fan out to subscribers first (stress /
        timeline session audio), close the partial segment, dispatch."""
        with self._subs_lock:
            subs = list(self._chunk_subs)
        for cb in subs:
            try:
                cb(audio_bytes)
            except Exception:
                logger.exception("voice chunk subscriber callback failed")

        with self._seg_lock:
            self._seg_open = False
            self._seg_frames = []
        # Continuous capture: the mic is deliberately NOT stopped here.

        if self._stt is None or not self._stt.is_ready:
            logger.warning("STT not ready, discarding audio chunk (%d bytes)", len(audio_bytes))
            return

        ptt_initiated = self._ptt_held
        threading.Thread(
            target=self._transcribe_and_dispatch,
            args=(audio_bytes, ptt_initiated),
            daemon=True,
            name="stt-transcribe",
        ).start()

    def _transcribe_and_dispatch(self, audio_bytes: bytes, ptt_initiated: bool = False) -> None:
        try:
            with self._stt_lock:
                result = self._stt.transcribe(audio_bytes)
            if result is None:
                logger.info("STT: no speech detected in chunk")
                return

            logger.info(
                "STT: '%s' (lang=%s, conf=%.2f, latency=%.0fms)",
                result.text, result.language, result.confidence, result.duration_ms,
            )
            self._broadcast_transcription(result.text)

            if ptt_initiated:
                logger.info("PTT mode -> dispatching without wake word: '%s'", result.text)
                self._cancel_inflight()  # a new turn supersedes a pending one
                self._dispatch(result.text)
            else:
                matched, cleaned = detect_wake_word(result.text)
                if matched:
                    if cleaned:
                        logger.info("Wake word matched, dispatching: '%s'", cleaned)
                        self._cancel_inflight()
                        self._dispatch(cleaned)
                    else:
                        logger.info("Wake word only, no command text")
                else:
                    logger.debug("No wake word in: '%s'", result.text)
        except Exception:
            logger.exception("STT transcription thread failed")

    # ── gate hook: paced replay through the same frame path ───────────────

    def inject_chunk_for_tests(self, audio_bytes: bytes) -> None:
        """TEST-ONLY (BS_GATE_HOOKS=1): replay post-VAD PCM as paced 30 ms
        frames through _on_frame (partials + barge onset run for real),
        then finalize at the chunk boundary. Returns immediately — the
        replay runs in a thread so the wire ack stays prompt."""
        if os.environ.get("BS_GATE_HOOKS") != "1":
            logger.warning("voice: test chunk injection refused (BS_GATE_HOOKS unset)")
            return
        logger.info(
            "GATE HOOK: replaying %d bytes as paced live frames + finalize "
            "(streaming path)",
            len(audio_bytes),
        )
        threading.Thread(
            target=self._replay_utterance,
            args=(audio_bytes,),
            daemon=True,
            name="streaming-replay",
        ).start()

    def _replay_utterance(self, pcm: bytes) -> None:
        """Real-time-paced frame replay (test-only). Frames from the
        injected PCM are marked speech — the hook boundary is POST-VAD by
        definition; leading silence frames keep barge-onset counting honest."""
        try:
            self._replay_active = True
            silence = b"\x00\x00" * 480
            for _ in range(_PRE_SILENCE_FRAMES):
                self._on_frame(silence, False, from_replay=True)
                time.sleep(FRAME_DURATION_MS / 1000)
            for i in range(0, len(pcm), _FRAME_BYTES):
                frame = pcm[i:i + _FRAME_BYTES]
                if len(frame) < _FRAME_BYTES:
                    frame = frame + b"\x00" * (_FRAME_BYTES - len(frame))
                self._on_frame(frame, True, from_replay=True)
                time.sleep(FRAME_DURATION_MS / 1000)
            self._on_speech_chunk(pcm)
        except Exception:
            logger.exception("streaming: paced replay failed")
        finally:
            self._replay_active = False

    # ── broadcasts / dispatch (same shapes as VoicePipeline) ──────────────

    def _dispatch(self, text: str) -> None:
        if self._loop and self._on_transcription:
            self._inflight = asyncio.run_coroutine_threadsafe(
                self._safe_dispatch(text),
                self._loop,
            )

    async def _safe_dispatch(self, text: str) -> None:
        try:
            if self._on_transcription:
                await self._on_transcription(text)
        except Exception:
            logger.exception("Voice dispatch to intent handler failed")

    def _broadcast_partial(self, text: str) -> None:
        if not self._loop or not self._ipc_state:
            return
        from blankslate.ipc.server import broadcast
        asyncio.run_coroutine_threadsafe(
            broadcast(self._ipc_state, {
                "type": "transcription_partial",
                "text": text,
                "final": False,
            }),
            self._loop,
        )

    def _broadcast_transcription(self, text: str) -> None:
        if not self._loop or not self._ipc_state:
            return
        from blankslate.ipc.server import broadcast
        asyncio.run_coroutine_threadsafe(
            broadcast(self._ipc_state, {"type": "transcription", "text": text}),
            self._loop,
        )

    def _broadcast_audio_level(self, level: float) -> None:
        if not self._loop or not self._ipc_state:
            return
        from blankslate.ipc.server import broadcast
        asyncio.run_coroutine_threadsafe(
            broadcast(self._ipc_state, {"type": "audio_level", "level": level}),
            self._loop,
        )

    # ── PTT callbacks (force-dispatch only; capture is always on) ─────────

    def _ptt_pressed(self) -> None:
        self._ptt_held = True
        logger.info("PTT press -> force-dispatch armed (mic already live)")

    def _ptt_released(self, _audio: bytes | None) -> None:
        self._ptt_held = False
        logger.info("PTT release -> force-dispatch disarmed")
