"""Real-time redaction of secrets / API-key-shaped strings.

Applied BEFORE anything is persisted to disk. Replaces matches with
[REDACTED:<type>] labels while preserving the surrounding text.

No secrets ever hit disk or logs.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("blankslate.guardrail.redact")

# API-key / secret shaped patterns
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # OpenAI-style sk-...
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    # Anthropic sk-ant-...
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    # AWS access key
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Generic long alphanumeric tokens (JWT, etc.)
    ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # AWS secret key
    ("aws_secret", re.compile(r"\b[0-9a-zA-Z/+]{40}\b")),
    # Private key PEM headers
    ("private_key", re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----")),
    # Long hex strings that look like secrets
    ("hex_secret", re.compile(r"\b[0-9a-f]{32,64}\b")),
    # Password-shaped key:value pairs, e.g. password=..., api_key=..., token=...
    ("key_value_secret", re.compile(r"\b(?:password|passwd|api[_-]?key|secret|token)\s*[=:]\s*['\"]?[A-Za-z0-9_\-\./+]{8,}['\"]?", re.IGNORECASE)),
]


def redact_text(text: str) -> str:
    """Redact secrets from text, in place.

    Returns redacted string. Missing/empty input returns input unchanged.
    """
    if not text:
        return text

    redacted = text
    for label, pattern in _PATTERNS:
        redacted, count = pattern.subn(f"[REDACTED:{label}]", redacted)
        if count:
            logger.info("Redacted %d %s-shaped value", count, label)

    return redacted


def is_redacted(text: str) -> bool:
    """Quick check whether a string contains redaction markers."""
    return "[REDACTED:" in text
