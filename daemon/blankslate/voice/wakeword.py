"""Wake-word detection — anywhere-in-sentence matching.

Detects "hey blank", "blank slate", or "assistant" anywhere in a
transcribed sentence. Case-insensitive. No exact-start requirement.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("blankslate.voice.wakeword")

# Patterns: wake word can appear anywhere in the transcript
_WAKE_PATTERNS = [
    re.compile(r"\bhey\s*blank\b", re.IGNORECASE),
    re.compile(r"\bblank\s*slate\b", re.IGNORECASE),
    re.compile(r"\bhey\s*assistant\b", re.IGNORECASE),
]


def detect_wake_word(text: str) -> tuple[bool, str | None]:
    """Check if text contains a wake word.

    Returns (matched: bool, cleaned_text: str | None).
    If matched, cleaned_text has the wake word removed.
    """
    for pattern in _WAKE_PATTERNS:
        m = pattern.search(text)
        if m:
            # Remove the wake word and leading/trailing whitespace
            cleaned = (text[:m.start()] + text[m.end():]).strip()
            logger.info("Wake word detected: '%s' in '%s'", m.group(), text)
            return True, cleaned

    return False, None


def is_wake_command(text: str) -> bool:
    """Quick check: does this text contain a wake word?"""
    matched, _ = detect_wake_word(text)
    return matched
