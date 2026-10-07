"""RapidOCR text extraction for timeline frames.

Engine is lazy-initialized inside the caller's worker thread (first load is
CPU-bound). A missing engine is reported loudly at session start (doc
section 9: dependencies fail the gate, never silently degrade).
"""

from __future__ import annotations

import logging
import threading

import numpy as np
from PIL import Image

logger = logging.getLogger("blankslate.timeline.ocr")

_engine = None
_engine_lock = threading.Lock()
_engine_error: str | None = None


def probe() -> str | None:
    """Return None when rapidocr is importable, else the failure reason."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return None
    except Exception as exc:  # pragma: no cover - env-dependent
        return f"rapidocr unavailable: {exc}"


def _get_engine():
    global _engine, _engine_error
    with _engine_lock:
        if _engine is None and _engine_error is None:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _engine = RapidOCR()
                logger.info("timeline OCR engine loaded")
            except Exception as exc:
                _engine_error = f"rapidocr init failed: {exc}"
                logger.exception(_engine_error)
    return _engine


def extract_text(img: Image.Image) -> str:
    """Run OCR on a PIL image; returns joined text ('' on no detections)."""
    engine = _get_engine()
    if engine is None:
        return ""
    arr = np.asarray(img.convert("RGB"))
    bgr = arr[:, :, ::-1]  # RapidOCR expects BGR channel order for ndarrays
    try:
        result, _elapse = engine(bgr)
    except Exception:
        logger.exception("timeline OCR failed")
        return ""
    if not result:
        return ""
    parts = [str(item[1]) for item in result if item and len(item) >= 2]
    return " ".join(p for p in parts if p)
