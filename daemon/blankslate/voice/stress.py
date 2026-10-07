"""Voice-stress adaptation — per-utterance urgency estimation (Phase 3).

Pure functions over the already-in-memory utterance frames (int16 PCM from
the finalized VAD speech chunk, mic.py SAMPLE_RATE) plus STT metadata
(word count from the transcribed text, duration derived from frame count).
Nothing here is ever written to disk or into conversation history — the
reading rides the wire as advisory metadata only and lives for the single
turn it was computed for.

Features (numpy only — no librosa):
- mean/dynamic RMS energy over voiced frames
- f0 via autocorrelation on voiced frames (median across frames)
- words/sec + voiced-frame rate + articulation rate (words per voiced second)

Composite urgency in [0.0, 1.0] with bands:
    calm     <  CALM_MAX
    elevated <  RUSH_MIN
    rushed   >= RUSH_MIN

Prompt-bias contract (propose-and-advisory, voice path only):
apply_stress_bias() appends ONE advisory system suffix to the LLM message
list iff ALL of: enabled, a stress reading is present, urgency >=
BIAS_THRESHOLD (== RUSH_MIN), and the user text contains no explicit
length/detail request. Every other case returns the input list unchanged
(byte-identical assembly — asserted directly by test_st3_stress.py).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import numpy as np

from blankslate.llm.ollama_client import ChatMessage

# Frame analysis (matches mic.py: 30 ms frames, 16 kHz int16)
FRAME_MS = 30
F0_MIN_HZ = 60.0
F0_MAX_HZ = 400.0
VOICING_MIN_RATIO = 0.30   # autocorr peak vs lag-0 must reach this

# Composite weights (sum = 1.0) — words/sec and articulation dominate:
# they separate rushed from calm speech for identical text.
W_WPS = 0.35
W_ARTICULATION = 0.35
W_VOICED_RATE = 0.05
W_F0 = 0.10
W_ENERGY = 0.10
W_DYNAMIC = 0.05

# Normalization ranges (feature -> [0,1]) — calibrated against real SAPI
# round-trips through the full PCM -> STT path (calm ~0.2, rushed ~0.7).
_WPS_RANGE = (2.0, 4.0)
_ARTIC_RANGE = (3.5, 10.0)
_F0_RANGE = (85.0, 300.0)
_ENERGY_RANGE = (0.020, 0.300)   # rms_mean / 32767
_DYNAMIC_RANGE = (0.020, 0.300)   # rms_dynamic / 32767

# Bands + bias threshold. Bias fires exactly for the "rushed" band.
CALM_MAX = 0.35
RUSH_MIN = 0.50
BIAS_THRESHOLD = RUSH_MIN

# Advisory suffix appended as a system message when the bias applies.
BIAS_SUFFIX = (
    "The user's speech sounds rushed - be as brief as possible unless "
    "they explicitly ask for detail."
)

# Explicit request guard: an in-conversation request for detail always
# outranks the acoustic hint (single words matched on boundaries so
# "along"/"longer" never trigger).
DETAIL_PATTERNS: tuple[str, ...] = (
    r"\bin detail\b",
    r"\bstep by step\b",
    r"\bthoroughly\b",
    r"\blong\b",
    r"\bfull explanation\b",
)


def has_detail_request(text: str) -> bool:
    """True when the user explicitly asked for length/detail in this turn."""
    t = (text or "").lower()
    return any(re.search(p, t) for p in DETAIL_PATTERNS)


def band_of(urgency: float) -> str:
    if urgency < CALM_MAX:
        return "calm"
    if urgency < RUSH_MIN:
        return "elevated"
    return "rushed"


def _norm(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (float(x) - lo) / (hi - lo)))


def _frame_rms(frames: np.ndarray, frame_len: int) -> np.ndarray:
    n = len(frames)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    usable = (n // frame_len) * frame_len
    if usable == 0:
        return np.zeros(0, dtype=np.float64)
    blocks = frames[:usable].astype(np.float64).reshape(-1, frame_len)
    return np.sqrt(np.mean(blocks * blocks, axis=1))


def _median_f0(voiced_blocks: np.ndarray, sample_rate: int) -> float | None:
    """Median f0 (Hz) via autocorrelation per voiced frame. numpy only."""
    if voiced_blocks.size == 0:
        return None
    frame_len = voiced_blocks.shape[1]
    lag_min = max(2, int(sample_rate / F0_MAX_HZ))
    lag_max = min(frame_len - 1, int(sample_rate / F0_MIN_HZ))
    if lag_max <= lag_min:
        return None
    estimates: list[float] = []
    for block in voiced_blocks:
        x = block.astype(np.float64)
        x = x - x.mean()
        energy = float(np.dot(x, x))
        if energy <= 0.0:
            continue
        # full autocorrelation, take positive lags
        ac = np.correlate(x, x, mode="full")[frame_len - 1 :]
        if ac[0] <= 0.0:
            continue
        peak = float(np.max(ac[lag_min : lag_max + 1]))
        if peak < VOICING_MIN_RATIO * float(ac[0]):
            continue  # unvoiced / noise-dominated frame
        lag = lag_min + int(np.argmax(ac[lag_min : lag_max + 1]))
        if lag > 0:
            estimates.append(sample_rate / lag)
    if not estimates:
        return None
    return float(np.median(estimates))


def compute_stress(
    frames: np.ndarray,
    text: str,
    sample_rate: int = 16000,
) -> dict[str, Any] | None:
    """Utterance frames + STT text -> stress reading dict (or None).

    None means "not enough signal to judge" — callers treat it as
    no-stress (never a failure).
    """
    if frames is None or len(frames) == 0:
        return None
    words = len(str(text or "").split())
    duration_s = len(frames) / float(sample_rate)
    if duration_s < 0.2 or words == 0:
        return None

    frame_len = max(1, int(sample_rate * FRAME_MS / 1000))
    rms = _frame_rms(frames, frame_len)
    if rms.size == 0:
        return None

    # Voiced threshold: adaptive from the utterance itself (SAPI/mic gain
    # agnostic), floored for near-silence safety.
    voiced_cut = max(300.0, 0.15 * float(np.percentile(rms, 90)))
    voiced_mask = rms > voiced_cut
    voiced_count = int(np.count_nonzero(voiced_mask))
    if voiced_count == 0:
        return None

    voiced_s = voiced_count * FRAME_MS / 1000.0
    rms_mean = float(np.mean(rms[voiced_mask]))
    rms_dynamic = float(np.percentile(rms[voiced_mask], 90) - np.percentile(rms[voiced_mask], 10))
    voiced_rate = voiced_count / float(rms.size)

    usable = (len(frames) // frame_len) * frame_len
    blocks = frames[:usable].astype(np.float64).reshape(-1, frame_len)
    f0_mean = _median_f0(blocks[voiced_mask[: blocks.shape[0]]], sample_rate)

    wps = words / duration_s
    articulation = words / voiced_s if voiced_s > 0 else 0.0

    urgency = (
        W_WPS * _norm(wps, *_WPS_RANGE)
        + W_ARTICULATION * _norm(articulation, *_ARTIC_RANGE)
        + W_VOICED_RATE * max(0.0, min(1.0, voiced_rate))
        + W_F0 * (_norm(f0_mean, *_F0_RANGE) if f0_mean else 0.0)
        + W_ENERGY * _norm(rms_mean / 32767.0, *_ENERGY_RANGE)
        + W_DYNAMIC * _norm(rms_dynamic / 32767.0, *_DYNAMIC_RANGE)
    )
    urgency = round(max(0.0, min(1.0, urgency)), 4)

    return {
        "urgency": urgency,
        "band": band_of(urgency),
        "features": {
            "words": words,
            "duration_s": round(duration_s, 3),
            "words_per_sec": round(wps, 3),
            "articulation_wps": round(articulation, 3),
            "voiced_rate": round(voiced_rate, 4),
            "voiced_s": round(voiced_s, 3),
            "rms_mean": round(rms_mean, 1),
            "rms_dynamic": round(rms_dynamic, 1),
            "f0_mean": round(f0_mean, 1) if f0_mean else None,
        },
    }


def compute_from_pcm(pcm: bytes, text: str, sample_rate: int = 16000) -> dict[str, Any] | None:
    """Raw int16 PCM bytes (the finalized speech chunk) -> stress reading."""
    try:
        frames = np.frombuffer(pcm, dtype=np.int16)
    except Exception:
        return None
    return compute_stress(frames, text, sample_rate=sample_rate)


def apply_stress_bias(
    messages: Iterable[Any],
    stress: dict[str, Any] | None,
    *,
    enabled: bool = True,
    text: str = "",
) -> list[Any]:
    """Append the advisory brevity suffix — or return assembly unchanged.

    Contract (asserted by test_st3_stress.py):
    - disabled or no reading            -> input list returned unchanged
    - urgency < BIAS_THRESHOLD          -> unchanged (calm/elevated)
    - explicit detail request in text   -> unchanged (never overridden)
    - otherwise                         -> input + [system BIAS_SUFFIX]
    """
    if not enabled or not isinstance(stress, dict):
        return list(messages)
    try:
        urgency = float(stress.get("urgency", 0.0))
    except (TypeError, ValueError):
        return list(messages)
    if urgency < BIAS_THRESHOLD:
        return list(messages)
    if has_detail_request(text):
        return list(messages)
    return list(messages) + [ChatMessage(role="system", content=BIAS_SUFFIX)]
