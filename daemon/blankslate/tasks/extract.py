"""NL → task extraction (Phase 1): local LLM primary, deterministic fallback.

Pipeline (per the approved plan):
  1. DUE: deterministic regex pass FIRST — relative times ("in 10 minutes"),
     ISO datetimes, today/tomorrow, weekdays, bare clock times. Clock math
     never depends on the LLM; the matched span is recorded for title cleanup.
  2. TITLE: local LLM via the proven planner.py pattern (prompt for JSON,
     brace-extract, validate) — the model strips command phrasing into a
     clean short title.
  3. FALLBACK: if the LLM is unavailable or returns garbage, a regex lead-
     verb strip + due-span removal produces the title. Both paths are
     exercised by the gate.

No LLM-client change: `llm.chat()` + brace extraction only (planner.py
precedent). set_llm() is wired by main.py (TTS-singleton precedent).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger("blankslate.tasks.extract")

_llm: Any = None


def set_llm(llm: Any) -> None:
    global _llm
    _llm = llm


# ── due-date regex pass ─────────────────────────────────────────────────────

_REL_RE = re.compile(
    r"\bin\s+(\d{1,3})\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
    re.I,
)
_ISO_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::(\d{2}))?)?\b"
)
_DAY_RE = re.compile(r"\b(today|tomorrow)\b\s*(?:at|by|before)?\s*", re.I)
_WD_RE = re.compile(
    r"\b(?:on\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"\s*(?:at|by|before)?\s*",
    re.I,
)
_CLOCK_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I
)
_CLOCK24_RE = re.compile(r"\b(?:at\s+)(\d{1,2}):(\d{2})\b", re.I)
# 24h clock for text AFTER a day/weekday word — "at" was already consumed
# by _DAY_RE/_WD_RE, so it must be optional here.
_CLOCK24_ANY = re.compile(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\b", re.I)

_WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_REL_UNITS = {
    "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "day": 86400.0, "days": 86400.0,
    "week": 604800.0, "weeks": 604800.0,
}

DEFAULT_TIME = (9, 0)  # date-only / bare-day phrases default to 09:00 local


def _clock(hour: int, minute: int, ampm: str | None, base: datetime) -> datetime | None:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def parse_due(query: str) -> tuple[float | None, str]:
    """Return (epoch, matched_span_text). First pattern wins.

    Order: relative → ISO → today/tomorrow → weekday → bare clock time.
    Ambiguous bare times land on today, or tomorrow if already past
    (same convention as every assistant).
    """
    now = datetime.now()

    m = _REL_RE.search(query)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        seconds = n * _REL_UNITS.get(unit, 60.0)
        return (now + timedelta(seconds=seconds)).timestamp(), m.group(0)

    m = _ISO_RE.search(query)
    if m:
        date_part, hms, sec = m.group(1), m.group(2), m.group(3)
        try:
            d = datetime.strptime(date_part, "%Y-%m-%d")
            if hms:
                dt = _clock(int(hms[:2]), int(hms[3:5]), None, d)
            else:
                dt = d.replace(hour=DEFAULT_TIME[0], minute=DEFAULT_TIME[1])
            if dt is not None:
                if sec:
                    dt = dt.replace(second=int(sec))
                return dt.timestamp(), m.group(0)
        except ValueError:
            pass

    m = _DAY_RE.search(query)
    if m:
        word = m.group(1).lower()
        base = now if word == "today" else now + timedelta(days=1)
        rest = query[m.end():]
        t = _CLOCK_RE.search(rest) or _CLOCK24_ANY.search(rest)
        if t:
            hour = int(t.group(1))
            minute = int(t.group(2)) if t.group(2) else 0
            dt = _clock(hour, minute, t.group(3) if t.lastindex and t.lastindex >= 3 else None, base)
            if dt is None:
                return None, ""
            # exact contiguous slice of the real query text (span removal)
            return dt.timestamp(), query[m.start():m.end() + t.end()]
        dt = base.replace(hour=DEFAULT_TIME[0], minute=DEFAULT_TIME[1], second=0, microsecond=0)
        return dt.timestamp(), query[m.start():m.end()]

    m = _WD_RE.search(query)
    if m:
        name = m.group(1).lower()
        target_idx = _WEEKDAYS.index(name)
        days_ahead = (target_idx - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7  # "friday" said on Friday = next Friday
        base = now + timedelta(days=days_ahead)
        rest = query[m.end():]
        t = _CLOCK_RE.search(rest) or _CLOCK24_ANY.search(rest)
        if t:
            hour = int(t.group(1))
            minute = int(t.group(2)) if t.group(2) else 0
            dt = _clock(hour, minute, t.group(3) if t.lastindex and t.lastindex >= 3 else None, base)
            if dt is None:
                return None, ""
            return dt.timestamp(), query[m.start():m.end() + t.end()]
        dt = base.replace(hour=DEFAULT_TIME[0], minute=DEFAULT_TIME[1], second=0, microsecond=0)
        return dt.timestamp(), query[m.start():m.end()]

    m = _CLOCK_RE.search(query) or _CLOCK24_RE.search(query)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = m.group(3) if m.lastindex and m.lastindex >= 3 else None
        dt = _clock(hour, minute, ampm, now)
        if dt is not None and dt <= now:
            dt = dt + timedelta(days=1)
        if dt is not None:
            return dt.timestamp(), m.group(0)

    return None, ""


# ── title extraction ────────────────────────────────────────────────────────

_LEAD_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"remind\s+me\s+to|remind\s+me|reminders?\s*:?"
    r"|add\s+(?:a\s+)?task\s*(?:to|about|for|:)?"
    r"|create\s+(?:a\s+)?task\s*(?:to|about|for|:)?"
    r"|new\s+task\s*(?:to|about|for|:)?"
    r"|i\s+need\s+to|todo\s*:?"
    r")\s+",
    re.I,
)


def _regex_title(query: str, due_span: str) -> str:
    t = query.strip()
    if due_span:
        t = re.sub(re.escape(due_span), " ", t, count=1, flags=re.I)
    for _ in range(3):
        m = _LEAD_RE.match(t)
        if not m:
            break
        t = t[m.end():]
    t = re.sub(r"\s+", " ", t).strip(" .,:!?-")
    return t[:200]


async def extract_task(query: str) -> dict[str, Any]:
    """Extract {title, due_at} from natural language. Never raises."""
    query = str(query or "").strip()
    due_at, due_span = parse_due(query)
    title = ""

    if _llm is not None and query:
        try:
            from blankslate.llm.ollama_client import ChatMessage

            prompt = (
                "You extract a task reminder from user text. Reply ONLY with JSON: "
                '{"title": "short task phrase, WITHOUT dates/times", '
                '"due": "YYYY-MM-DDTHH:MM in local time, or empty string"}\n'
                f"User text: {query}\nJSON:"
            )
            resp = await _llm.chat(
                [ChatMessage(role="user", content=prompt)], num_ctx=512
            )
            text = resp.content.strip()
            start, end = text.find("{"), text.rfind("}") + 1
            if 0 <= start < end:
                data = json.loads(text[start:end])
                cand = str(data.get("title", "")).strip()[:200]
                if cand:
                    title = cand
                # LLM due only used when the deterministic pass found none.
                if due_at is None:
                    due_txt = str(data.get("due", "")).strip().replace(" ", "T")
                    if due_txt:
                        try:
                            due_at = datetime.fromisoformat(due_txt).timestamp()
                        except ValueError:
                            logger.info("task extract: bad LLM due %r ignored", due_txt)
        except Exception:
            logger.exception("task extract LLM pass failed — regex fallback")

    if due_span:
        # Drop the due phrase from the title no matter which path produced it.
        title = re.sub(re.escape(due_span), " ", title, count=1, flags=re.I)
    if due_at is not None and title:
        # The LLM may REORDER the due phrase ("call Ned at 5pm tomorrow" vs
        # the query's "tomorrow at 5pm") so exact-span removal can miss —
        # strip clock/date/weekday tokens whenever a due was actually parsed.
        title = re.sub(
            r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b",
            " ", title, flags=re.I,
        )
        title = re.sub(
            r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|"
            r"friday|saturday|sunday)\b|\b\d{4}-\d{2}-\d{2}\b",
            " ", title, flags=re.I,
        )
    if not title:
        title = _regex_title(query, due_span)
    title = re.sub(r"\s+", " ", title).strip(" .,:!?-")[:200]

    return {"title": title, "due_at": due_at, "source": "text"}
