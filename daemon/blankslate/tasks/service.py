"""Task tool handlers (Phase 1) — standard dispatcher contract.

Every handler: structured args win when present (existing contract), NL
`query` extraction otherwise. All replies are self-contained →
deterministic short-circuit in the orchestrator (no LLM echo round trip).
Storage goes exclusively through the encrypted TaskStore (vault.seal()).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from blankslate.tasks.extract import extract_task
from blankslate.tasks.store import Task, get_store

logger = logging.getLogger("blankslate.tasks.service")


def _fmt(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _parse_due_arg(value: Any) -> float | None:
    """Structured due_at: ISO string or epoch float."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip().replace(" ", "T")
    try:
        return datetime.fromisoformat(txt).timestamp()
    except ValueError:
        logger.info("unparseable structured due_at: %r", value)
        return None


async def _resolve(args: dict[str, Any]) -> tuple[str, float | None, str]:
    """(title, due_at, source) from structured args, else NL extraction."""
    query = str(args.get("query", "")).strip()
    title = str(args.get("title", args.get("name", ""))).strip()
    due = _parse_due_arg(args.get("due_at"))
    source = str(args.get("source", "text")).strip() or "text"

    if not title or due is None:
        nl = await extract_task(query)
        if not title:
            title = nl["title"]
        if due is None:
            due = nl["due_at"]
    return title[:200], due, source


async def add_task(args: dict[str, Any]) -> dict[str, Any]:
    title, due, source = await _resolve(args)
    if not title:
        return {
            "tool": "add_task", "deterministic": True,
            "result": "Could not understand a task title.",
            "reply": "I couldn't catch a task title — try 'add task: buy milk'.",
        }
    task = get_store().add(title, due_at=due, source=source)
    due_txt = _fmt(task.due_at)
    reply = f"Added task '{task.title}'" + (f", due {due_txt}." if due_txt else ".")
    return {
        "tool": "add_task", "deterministic": True,
        "task": task.to_public(),
        "result": reply, "reply": reply,
    }


async def set_reminder(args: dict[str, Any]) -> dict[str, Any]:
    title, due, source = await _resolve(args)
    if not title:
        return {
            "tool": "set_reminder", "deterministic": True,
            "result": "Could not understand what to remind you about.",
            "reply": "I couldn't catch what to remind you about — try "
                     "'remind me to call Ned tomorrow at 5pm'.",
        }
    if due is None:
        return {
            "tool": "set_reminder", "deterministic": True,
            "result": "No due time given; no reminder created.",
            "reply": "When should I remind you? Add a time, e.g. "
                     "'remind me to check the oven in 20 minutes'. No task created yet.",
        }
    task = get_store().add(title, due_at=due, source=source, remind=True)
    reply = f"Reminder set for '{task.title}' — I'll alert you at {_fmt(task.due_at)}."
    return {
        "tool": "set_reminder", "deterministic": True,
        "task": task.to_public(),
        "result": reply, "reply": reply,
    }


async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).lower()
    filt = str(args.get("filter", "")).lower()
    if not filt:
        if re.search(r"\b(today|due today)\b", query):
            filt = "today"
        elif re.search(r"\b(overdue|late)\b", query):
            filt = "overdue"
        elif re.search(r"\b(completed|done|finished)\b", query):
            filt = "completed"
        elif re.search(r"\b(all|everything)\b", query):
            filt = "all"
        else:
            filt = "pending"

    now = time.time()
    day_end = datetime.now().replace(hour=23, minute=59, second=59).timestamp()
    tasks: list[Task] = get_store().all()
    if filt == "pending":
        tasks = [t for t in tasks if not t.is_completed]
    elif filt == "completed":
        tasks = [t for t in tasks if t.is_completed]
    elif filt == "overdue":
        tasks = [
            t for t in tasks
            if not t.is_completed and t.due_at is not None and t.due_at < now
        ]
    elif filt == "today":
        tasks = [
            t for t in tasks
            if not t.is_completed and t.due_at is not None
            and now <= t.due_at <= day_end
        ]

    if not tasks:
        reply = f"No {filt} tasks."
        return {
            "tool": "list_tasks", "deterministic": True, "filter": filt,
            "tasks": [], "count": 0, "result": reply, "reply": reply,
        }

    parts = []
    for i, t in enumerate(tasks, 1):
        due = _fmt(t.due_at)
        tag = f" [due {due}]" if due else ""
        if t.is_completed:
            tag = " [done]"
        elif due and t.due_at is not None and t.due_at < now:
            tag = f" [overdue {due}]"
        parts.append(f"{i}) {t.title}{tag}")
    reply = f"{len(tasks)} {filt} task" + ("s: " if len(tasks) != 1 else ": ") + " ".join(parts)
    return {
        "tool": "list_tasks", "deterministic": True, "filter": filt,
        "tasks": [t.to_public(now) for t in tasks], "count": len(tasks),
        "result": reply, "reply": reply,
    }


async def complete_task(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    ref = str(args.get("title", args.get("id", ""))).strip()
    if not ref:
        ref = re.sub(
            r"^(?:please\s+)?(?:complete|finish|mark(?:\s+(?:it|this|the))?|"
            r"done with|i('| i)?m done with)\s*",
            "", query, flags=re.I,
        ).strip(" .,:!?-")
    task = get_store().complete(ref) if ref else None
    if task is None:
        reply = f"No matching task for '{ref or query}'."
        return {
            "tool": "complete_task", "deterministic": True, "matched": False,
            "result": reply, "reply": reply,
        }
    if task.completed_at is None:
        reply = f"'{task.title}' is not open — it's already done."
    else:
        reply = f"Marked '{task.title}' done."
    return {
        "tool": "complete_task", "deterministic": True, "matched": True,
        "task": task.to_public(), "result": reply, "reply": reply,
    }


async def delete_task(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    ref = str(args.get("title", args.get("id", ""))).strip()
    if not ref:
        ref = re.sub(
            r"^(?:please\s+)?(?:delete|remove|cancel|drop)\s+(?:the\s+|task\s+)?",
            "", query, flags=re.I,
        ).strip(" .,:!?-")
    ok = get_store().delete(ref) if ref else False
    if ok:
        reply = f"Deleted task '{ref}'."
    else:
        reply = f"No matching task for '{ref or query}'."
    return {
        "tool": "delete_task", "deterministic": True, "deleted": ok,
        "result": reply, "reply": reply,
    }


# ── ICS import (local file only, built-in parser — no new dependency) ────────

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _ics_dt(value: str) -> float | None:
    value = value.strip()
    try:
        if value.endswith("Z") and len(value) == 16:
            from datetime import timezone

            return (
                datetime.strptime(value, "%Y%m%dT%H%M%SZ")
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        if "T" in value:
            return datetime.strptime(value, "%Y%m%dT%H%M").timestamp()
        if len(value) == 8:
            d = datetime.strptime(value, "%Y%m%d")
            return d.replace(hour=9, minute=0).timestamp()  # all-day → 09:00
    except ValueError:
        logger.info("ICS unparseable DTSTART: %r", value)
    return None


def _ics_unescape(v: str) -> str:
    return (
        v.replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def _parse_ics(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in _unfold(text):
        up = line.upper()
        if up == "BEGIN:VEVENT":
            cur = {}
        elif up == "END:VEVENT":
            if cur is not None and cur.get("summary"):
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            key = key.split(";")[0].upper()
            if key == "SUMMARY":
                cur["summary"] = _ics_unescape(val).strip()
            elif key == "DTSTART":
                cur["due_at"] = _ics_dt(val)
    return events


async def import_ics(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", "")).strip().strip('"')
    if not path:
        m = re.search(r"([A-Za-z]:\\[^\s\"]+\.ics|[\w./\\-]+\.ics)", str(args.get("query", "")))
        if m:
            path = m.group(1)
    if not path:
        return {
            "tool": "import_ics", "deterministic": True,
            "result": "No .ics file path given.",
            "reply": "Which .ics file? e.g. 'import ics file C:\\cal\\plans.ics' (local files only).",
        }
    p = Path(path)
    if not p.is_file():
        return {
            "tool": "import_ics", "deterministic": True, "path": path,
            "result": f"File not found: {path}",
            "reply": f"I couldn't find that file: {path}",
        }
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "tool": "import_ics", "deterministic": True, "path": path,
            "result": f"read error: {exc}",
            "reply": f"I couldn't read that file: {exc}",
        }

    events = _parse_ics(text)
    store = get_store()
    imported = 0
    names: list[str] = []
    for ev in events:
        store.add(ev["summary"][:200], due_at=ev.get("due_at"), source="ics")
        imported += 1
        names.append(ev["summary"])
    reply = (
        f"Imported {imported} event" + ("s" if imported != 1 else "")
        + (": " + ", ".join(names[:5]) + ("…" if len(names) > 5 else "") if imported else ".")
    )
    return {
        "tool": "import_ics", "deterministic": True, "path": path,
        "imported": imported, "result": reply, "reply": reply,
    }
