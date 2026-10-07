"""Speech-to-Text engine — faster-whisper, isolated init.

Init failure is caught and logged — the STT object will have
`is_ready == False` and `transcribe()` will return None.
This ensures the PTT loop never freezes behind a failed STT init.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from blankslate.voice.text_correction import correct_text

logger = logging.getLogger("blankslate.voice.stt")


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str
    confidence: float
    duration_ms: float


class STTEngine:
    """Wrapper around faster-whisper with graceful degradation.

    If the model fails to load, is_ready stays False and transcribe()
    returns None — never raises to the caller.

    Supports vocabulary priming via initial_prompt to bias the model
    toward specific words (wake words, app names, etc.).
    """

    def __init__(
        self,
        model_name: str = "small.en",
        device: str = "auto",
        compute_type: str | None = None,
        vocabulary_prompt: str = "",
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._vocabulary_prompt = vocabulary_prompt
        self._model: Any = None
        self._ready = False

        try:
            self._init_model()
            self._ready = True
            logger.info(
                "STT ready: model=%s, device=%s, compute_type=%s, vocab_prompt=%d chars",
                model_name, device, compute_type or "auto", len(vocabulary_prompt),
            )
        except Exception as exc:
            logger.error("STT init failed (degraded mode): %s", exc)
            self._ready = False

    def _init_model(self) -> None:
        """Initialize model with GPU fallback to CPU.

        If GPU (CUDA) fails (e.g., missing cuBLAS), falls back to CPU with int8.
        """
        from faster_whisper import WhisperModel

        # Determine compute_type
        if self._compute_type:
            compute_type = self._compute_type
        elif self._device in ("cuda", "gpu"):
            compute_type = "float16"
        else:
            compute_type = "int8"

        # Try GPU first, fall back to CPU
        try:
            self._model = WhisperModel(
                self._model_name,
                device=self._device,
                compute_type=compute_type,
            )
            logger.info("STT model loaded: device=%s, compute_type=%s", self._device, compute_type)
        except Exception as exc:
            if self._device in ("cuda", "gpu"):
                logger.warning(
                    "GPU load failed (%s), falling back to CPU int8", exc
                )
                self._device = "cpu"
                compute_type = "int8"
                self._model = WhisperModel(
                    self._model_name,
                    device="cpu",
                    compute_type="int8",
                )
                logger.info("STT model loaded (fallback): device=cpu, compute_type=int8")
            else:
                raise

    @property
    def is_ready(self) -> bool:
        return self._ready

    def transcribe(self, audio_bytes: bytes) -> TranscriptionResult | None:
        """Transcribe raw 16-bit PCM audio bytes.

        Uses vocabulary priming (initial_prompt) to bias the model
        toward recognized words.

        Returns None if STT is not ready or transcription fails.
        Never raises.
        """
        if not self._ready or self._model is None:
            logger.debug("STT not ready, skipping transcription")
            return None

        try:
            import numpy as np

            # Convert bytes to float32 numpy array for faster-whisper
            audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            t0 = time.perf_counter()
            segments, info = self._model.transcribe(
                audio_np,
                beam_size=3,
                language="en",
                vad_filter=True,
                initial_prompt=self._vocabulary_prompt or None,
            )

            texts = []
            avg_prob = 0.0
            count = 0
            for seg in segments:
                texts.append(seg.text)
                avg_prob += seg.avg_logprob
                count += 1

            elapsed_ms = (time.perf_counter() - t0) * 1000
            full_text = " ".join(texts).strip()
            avg_prob = avg_prob / max(count, 1)

            if not full_text:
                return None

            # Apply fuzzy correction for accent mishearings
            corrected_text = correct_text(full_text)

            return TranscriptionResult(
                text=corrected_text,
                language=info.language,
                confidence=avg_prob,
                duration_ms=elapsed_ms,
            )

        except Exception as exc:
            logger.error("STT transcription failed: %s", exc)
            return None
