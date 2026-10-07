"""Timeline tool handlers (standard dispatcher contract, wire-only args).

Deterministic replies; structured args win over NL extraction for callers
that pass them (the standard contract).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from blankslate.timeline import store as timeline_store
from blankslate.timeline.index import ensure_index
from blankslate.timeline.service import get_service
from blankslate.vault import core as vault

_START_RE = re.compile(
    r"^\s*(?:please\s+)?(?:start|begin|enable)\s+(?:the\s+)?"
    r"(?:timeline|screen\s*recording|recording)\b",
    re.I,
)
_STOP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:stop|end|disable|halt)\s+(?:the\s+)?timeline\b",
    re.I,
)
_SEARCH_RE = re.compile(
    r"^\s*(?:please\s+)?(?:search|find|lookup|look\s+up)(?:\s+in)?\s+"
    r"(?:the\s+)?timeline(?:\s+(?:for|about))?\s*(?P<q>.*)$",
    re.I,
)
_SEARCH_ALT_RE = re.compile(r"^\s*timeline\s+(?:search|find|for)\s+(?P<q>.*)$", re.I)


def _terms(args: dict[str, Any]) -> str:
    q = str(args.get("q", args.get("text", ""))).strip()
    if q:
        return q
    query = str(args.get("query", "")).strip()
    m = _SEARCH_RE.match(query) or _SEARCH_ALT_RE.match(query)
    if m:
        return m.group("q").strip().strip("'\"")
    return re.sub(r"\b(timeline|search|for|in|the)\b", " ", query,
                  flags=re.I).strip() or ""


async def start_timeline(args: dict[str, Any]) -> dict[str, Any]:
    svc = get_service()
    if svc is None:
        return {
            "tool": "start_timeline", "deterministic": True,
            "result": "timeline service not wired",
            "reply": "Timeline is not available in this daemon build.",
        }
    query = str(args.get("query", "")).strip()
    if not args.get("action") and query and not _START_RE.match(query):
        pass  # router already decided this is a start intent; parse leniently
    audio = bool(re.search(r"\b(?:audio|microphone)\b", query, re.I))
    ok, msg = await svc.start(audio=audio)
    if not ok:
        return {
            "tool": "start_timeline", "deterministic": True,
            "result": f"start failed: {msg}",
            "reply": f"Timeline can't start: {msg}",
        }
    if msg == "already":
        return {
            "tool": "start_timeline", "deterministic": True,
            "result": "already active",
            "reply": "Timeline session is already active.",
        }
    if audio and svc.state.get("audio"):
        return {
            "tool": "start_timeline", "deterministic": True,
            "result": "session started with audio",
            "reply": "Timeline session started with audio - mic subscribed, "
                     "capturing the active window now.",
        }
    return {
        "tool": "start_timeline", "deterministic": True,
        "result": "session started",
        "reply": "Timeline session started - capturing the active window now.",
    }


async def stop_timeline(args: dict[str, Any]) -> dict[str, Any]:
    svc = get_service()
    if svc is None:
        return {
            "tool": "stop_timeline", "deterministic": True,
            "result": "timeline service not wired",
            "reply": "Timeline is not available in this daemon build.",
        }
    _ok, msg = await svc.stop()
    if msg == "idle":
        return {
            "tool": "stop_timeline", "deterministic": True,
            "result": "no active session",
            "reply": "No timeline session is running.",
        }
    frames = svc.state.get("frames", 0)
    return {
        "tool": "stop_timeline", "deterministic": True,
        "frames": frames, "result": "session stopped",
        "reply": f"Timeline session stopped ({frames} frame(s) indexed).",
    }


async def search_timeline(args: dict[str, Any]) -> dict[str, Any]:
    terms = _terms(args)
    if not terms:
        return {
            "tool": "search_timeline", "deterministic": True,
            "result": "no search terms",
            "reply": "What should I search the timeline for?",
        }
    idx = ensure_index()
    idx.ensure_built()
    hits = idx.search(terms, limit=8)

    parts: list[str] = []
    for rid, snippet in hits:
        row = timeline_store.get(rid)
        if row is None:
            continue
        _created, payload = row
        try:
            import json

            meta = json.loads(vault.open_envelope(payload))
        except Exception:
            meta = {}
        ts = float(meta.get("ts", 0.0))
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
        proc = str(meta.get("process", "")) or "?"
        title = str(meta.get("title", ""))[:60]
        parts.append(f"{when} {proc} '{title}' - {snippet[:100]}")

    if not parts:
        return {
            "tool": "search_timeline", "deterministic": True,
            "terms": terms, "count": 0,
            "result": "no matches", "reply": f"Timeline: no matches for '{terms}'.",
        }
    joined = " | ".join(f"{i}) {p}" for i, p in enumerate(parts, 1))
    reply = f"Timeline: {len(parts)} match(es) for '{terms}': {joined}"
    return {
        "tool": "search_timeline", "deterministic": True,
        "terms": terms, "count": len(parts), "hits": parts,
        "result": reply, "reply": reply,
    }
