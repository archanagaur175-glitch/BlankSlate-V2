"""Built-in tool handlers — real local implementations.

Design: local-first. Everything here runs on-device with zero network or
API keys. `web_search` and `weather` have no local engine yet, so they
return a graceful, honest "not configured" note that the LLM can relay
without inventing facts. The daemon stays truthful by default.
"""

from __future__ import annotations

import logging
import operator
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("blankslate.tools.handlers")

# Module-level state for online/offline mode (set by IPC server)
_online_mode: bool = False


def set_online_mode(enabled: bool) -> None:
    """Update the global online mode flag."""
    global _online_mode
    _online_mode = enabled


def get_online_mode() -> bool:
    """Get current online mode."""
    return _online_mode

# ── get_current_time ───────────────────────────────────────────────────────

# Words that may follow "note" in a query but are never a note label.
_NOTE_LABEL_STOP = frozenset({
    "say", "says", "read", "the", "my", "a", "an", "about", "contains",
    "contain", "was", "is", "were", "it", "have", "has", "do", "does",
    "did", "there", "list", "delete", "remove", "show", "get", "what",
    "which", "please", "that", "this", "note", "notes", "rm", "ls",
})


async def get_current_time(_args: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now()
    label = now.strftime("%A, %B %d %Y at %I:%M:%S %p")
    return {
        "tool": "get_current_time",
        "result": label,
        "iso": now.isoformat(timespec="seconds"),
        "timestamp": time.time(),
        "reply": f"The current time is {label}.",
        "deterministic": True,
    }


# ── calculator (safe evaluation, no eval) ──────────────────────────────────

_BINARY = {
    "+": operator.add, "-": operator.sub,
    "*": operator.mul, "/": operator.truediv,
    "^": operator.pow, "**": operator.pow,
}


_ARITH_PAT = re.compile(r"[\d()+*/\-^]+")


def _extract_expression(text: str) -> str | None:
    """Pull a compact arithmetic expression out of natural language.

    Handles "calculate 12 * 8", "what is (10-4)/2", "12+8", and spoken-word
    operators like "6 times 7", "3 plus 4", "10 divided by 2". Whitespace is
    stripped globally, then every maximal contiguous run of digits, operators,
    and parens is examined; the one containing both a digit and an operator is
    returned. Letters break runs, so surrounding words are ignored cleanly.
    """
    compact = re.sub(r"\s+", "", text)
    # Translate spoken-word operators into symbols so expression runs stay whole.
    compact = (
        compact
        .replace("dividedby", "/")
        .replace("multipliedby", "*")
        .replace("times", "*")
        .replace("minus", "-")
        .replace("plus", "+")
        .replace("over", "/")
    )
    best = None
    for run in _ARITH_PAT.findall(compact):
        run = run.strip("+-")
        if not re.search(r"\d", run):
            continue
        if re.search(r"[+\-*/^()]", run):
            if best is None or len(run) > len(best):
                best = run
    return best


async def calculator(args: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a simple arithmetic expression safely (no eval/imports).

    Accepts either an explicit `expression`/`query` arg or a raw natural-
    language query from which the arithmetic expression is extracted.
    """
    raw = str(args.get("expression") or args.get("query") or "").strip()

    # Try to extract an arithmetic-looking substring from natural language,
    # e.g. "calculate 12 * 8" → "12 * 8". Walk tokens looking for the operand
    # sequence.
    expr = _extract_expression(raw)
    if expr is None:
        return {
            "tool": "calculator",
            "error": f"no arithmetic expression found in: {raw!r}",
            "result": None,
        }
    expr = re.sub(r"\s+", "", expr)

    # Validate only digits, operators, parens, and dot
    if not re.fullmatch(r"[0-9+\-*/^().]+", expr):
        return {
            "tool": "calculator",
            "error": f"unsupported expression: {expr!r}",
            "result": None,
        }

    def parse(tokens: list[str]) -> float:
        # Recursive descent: expression → term → factor → number
        pos = 0

        def peek() -> str | None:
            return tokens[pos] if pos < len(tokens) else None

        def number() -> float:
            nonlocal pos
            tok = tokens[pos]
            pos += 1
            return float(tok)

        def factor() -> float:
            nonlocal pos
            if peek() == "(":
                pos += 1  # consume '('
                val = expression()
                if peek() == ")":
                    pos += 1
                return val
            if peek() and (peek()[-1] in "0123456789." or peek() in {"+", "-"}):
                # Unary sign
                sign = 1.0
                if peek() in {"+", "-"}:
                    sign = -1.0 if tokens[pos] == "-" else 1.0
                    pos += 1
                return sign * number()
            return number()

        def term() -> float:
            nonlocal pos
            val = factor()
            while pos < len(tokens):
                op = peek()
                if op in {"*", "/"}:
                    pos += 1
                    rhs = factor()
                    val = val / rhs if op == "/" else val * rhs
                else:
                    break
            return val

        def expression() -> float:
            nonlocal pos
            val = term()
            while pos < len(tokens):
                op = peek()
                if op in {"+", "-"}:
                    pos += 1
                    rhs = term()
                    val = val + rhs if op == "+" else val - rhs
                else:
                    break
            return val

        return expression()

    # Tokenize: split numbers/operators/parens
    tokens = re.findall(r"\d+\.\d+|\d+|[+\-*/^()]", expr)
    try:
        result = parse(tokens)
        if result.is_integer():
            formatted = str(int(result))
        else:
            formatted = f"{result:.6f}".rstrip("0").rstrip(".")
        return {
            "tool": "calculator",
            "expression": expr,
            "result": formatted,
            "reply": f"The result of {expr} is {formatted}.",
            "deterministic": True,
        }
    except Exception as exc:
        return {"tool": "calculator", "error": f"could not evaluate: {exc}", "result": None}


# ── note_taking (local file-backed) ─────────────────────────────────────────


def _notes_dir() -> Path:
    # Notes live in a `notes` folder under the daemon working directory.
    d = Path.cwd() / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def note_taking(args: dict[str, Any]) -> dict[str, Any]:
    from blankslate.vault import core as vault

    action = str(args.get("action", "")).lower()
    label = str(args.get("label", args.get("name", "note"))).strip().lower()
    text = str(args.get("text", args.get("content", ""))).strip()
    query = str(args.get("query", "")).strip()

    # Infer action + content from the raw query when structured args absent.
    # READ/LIST/DELETE are checked BEFORE the bare "note" write-pattern:
    # otherwise "what does note X say" matched WRITE first and the handler
    # OVERWROTE the note with query fragments (integration-gate discovery).
    if not action:
        has_note_word = re.search(r"\bnotes?\b", query, re.I)
        if has_note_word and re.search(r"\b(delete|remove|rm)\b", query, re.I):
            action = "delete"
        elif has_note_word and re.search(r"\b(list|ls)\b", query, re.I):
            action = "list"
        elif has_note_word and re.search(r"\b(what|read|recall|show|get|which)\b", query, re.I):
            action = "read"
        elif re.search(
            r"\b(remember|remember that|note(?:\s+down)?|write down|save)\b",
            query, re.I,
        ):
            action = "write"
        else:
            action = "read"
    if not label:
        label = "note"
    # NL label: "note hw4", "note about groceries" (stopword hits rejected
    # so "what does my note say" does not become label='say').
    if label == "note" and query:
        m = re.search(
            r"\bnotes?\s+(?:named\s+|called\s+|about\s+)?([a-z0-9_-]+)",
            query, re.I,
        )
        if m and m.group(1).lower() not in _NOTE_LABEL_STOP:
            label = m.group(1).lower()
    if not text and action in ("write", "append"):
        # Capture the remembered phrase after a leading verb phrase.
        m = re.search(
            r"(?:remember that|remember|note(?: down)?(?: that)?|write down|save)"
            r"\s+(?:my\s+)?(.+)", query, re.I
        )
        if m:
            text = m.group(1).strip(" .!?")
    # Default label to first word of content when still generic.
    if label == "note" and text:
        word = re.sub(r"[^a-z0-9_-]+", "_", text.split()[0].lower()) if text.split() else "note"
        label = word or "note"
    label = re.sub(r"[^a-z0-9_-]+", "_", label) or "note"
    path = _notes_dir() / f"{label}.txt"

    if action in ("add", "create", "write", "append", "save"):
        # `text` was already resolved above (explicit arg OR extracted from
        # the query). Only fall back to args if it is still empty.
        if not text:
            text = str(args.get("text", args.get("content", ""))).strip()
        mode = "a" if action in ("append",) else "w"
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "a":
            existing = vault.read_bytes(path) if path.exists() else b""
            payload = existing + text.encode("utf-8") + b"\n"
        else:
            payload = text.encode("utf-8")
        vault.write_bytes(path, payload)
        summary = f"Saved note '{label}' with: {text}"
        return {
            "tool": "note_taking", "action": action, "note": label,
            "saved": str(path),
            "result": summary,
            "reply": f"I saved your note under '{label}'.",
            "deterministic": True,
        }
    if action in ("read", "get", "show"):
        if not path.exists():
            return {
                "tool": "note_taking", "action": "read", "note": label,
                "result": f"No note named '{label}' exists yet.",
                "reply": f"There's no note named '{label}' yet.",
                "deterministic": True,
            }
        content = vault.read_text(path).strip()
        return {
            "tool": "note_taking", "action": "read", "note": label,
            "result": content or f"(note '{label}' is empty)",
            "reply": f"Your note '{label}' says: {content}" if content else f"The note '{label}' is empty.",
            "deterministic": True,
        }
    if action in ("list", "ls"):
        files = sorted(p.stem for p in _notes_dir().glob("*.txt"))
        names = ", ".join(files) or "no notes yet"
        return {
            "tool": "note_taking", "action": "list", "notes": files,
            "result": names,
            "reply": f"Your notes are: {names}." if files else "You don't have any notes yet.",
            "deterministic": True,
        }
    if action in ("delete", "remove", "rm"):
        if path.exists():
            path.unlink()
            return {
                "tool": "note_taking", "action": "delete", "note": label,
                "deleted": True, "result": f"Deleted note '{label}'",
                "reply": f"I deleted your note '{label}'.",
                "deterministic": True,
            }
        return {
            "tool": "note_taking", "action": "delete", "note": label,
            "deleted": False, "result": f"No note named '{label}' to delete",
            "reply": f"There's no note named '{label}' to delete.",
            "deterministic": True,
        }
    return {"tool": "note_taking", "error": f"unknown action: {action}"}


# ── web_search / weather (graceful local stubs) ─────────────────────────────

async def web_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()

    if not _online_mode:
        return {
            "tool": "web_search",
            "result": (
                f"Web search requires online mode (query was: {query!r}). "
                "Ask the user: 'Web search requires online mode. Switch to online?'"
            ),
            "local_only": True,
            "reply": "Web search requires online mode. Switch to online to search?",
            "deterministic": True,
            "needs_online": True,
        }

    # Online mode — use DuckDuckGo
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

        if not results:
            return {
                "tool": "web_search",
                "result": f"No results found for: {query!r}",
                "reply": f"I couldn't find any results for '{query}'.",
                "deterministic": True,
            }

        # Format results for LLM — clean up noisy snippets
        import re
        formatted = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            # Strip leading dates/junk like "2 weeks ago -" or "July 28, 2026 -"
            body = re.sub(r"^[\w\s]{0,20}(ago|ago)\s*[-–—]\s*", "", body)
            body = re.sub(r"^[A-Z][a-z]+ \d{1,2}, \d{4}\s*[-–—]\s*", "", body)
            # Take only the first sentence (up to 200 chars)
            body = body[:200].rsplit(".", 1)[0] + "." if "." in body[:200] else body[:200]
            formatted.append(f"{i}. {title}\n   {body}\n   {href}")

        result_text = "\n\n".join(formatted)
        return {
            "tool": "web_search",
            "result": result_text,
            "reply": None,  # let LLM summarize
            "deterministic": False,
        }
    except Exception as exc:
        logger.error("DuckDuckGo search failed: %s", exc)
        return {
            "tool": "web_search",
            "result": f"Search failed: {exc}",
            "reply": f"Web search failed: {exc}",
            "deterministic": True,
            "error": str(exc),
        }


async def weather(args: dict[str, Any]) -> dict[str, Any]:
    place = str(args.get("location", args.get("city", ""))).strip()
    query = args.get("query", "")

    # When online, fall back to web search for real weather data
    if _online_mode:
        return await web_search({"query": f"weather in {place} today" if place else query})

    return {
        "tool": "weather",
        "result": (
            "weather is not configured (no external forecast API) in this "
            f"local-first build (location: {place!r}). I can't report live conditions."
        ),
        "local_only": True,
        "reply": "I can't report live weather — this is a local-first build with no forecast service configured.",
        "deterministic": True,
    }


# ── app launcher / killer (Phase 1 — desktop automation) ────────────────────

from blankslate.tools.app_manager import app_launch as _app_launch, app_kill as _app_kill

async def app_launch(args: dict[str, Any]) -> dict[str, Any]:
    return await _app_launch(args)

async def app_kill(args: dict[str, Any]) -> dict[str, Any]:
    return await _app_kill(args)


# ── window manager (Phase 2 — focus switching) ──────────────────────────────

from blankslate.tools.window_manager import (
    list_windows as _list_windows,
    switch_window as _switch_window,
    get_foreground_window as _get_foreground_window,
)

async def list_windows(args: dict[str, Any]) -> dict[str, Any]:
    return await _list_windows(args)

async def switch_window(args: dict[str, Any]) -> dict[str, Any]:
    return await _switch_window(args)

async def get_foreground_window(args: dict[str, Any]) -> dict[str, Any]:
    return await _get_foreground_window(args)


# ── text injector (Phase 3 — safe text injection) ──────────────────────────

from blankslate.tools.text_injector import (
    type_text as _type_text,
    press_key as _press_key,
    hotkey as _hotkey,
)

async def type_text(args: dict[str, Any]) -> dict[str, Any]:
    return await _type_text(args)

async def press_key(args: dict[str, Any]) -> dict[str, Any]:
    return await _press_key(args)

async def hotkey(args: dict[str, Any]) -> dict[str, Any]:
    return await _hotkey(args)


# ── focus memory (Phase 1 — snapshot/restore foreground window) ─────────────

from blankslate.focus.memory import (
    focus_snapshot as _focus_snapshot,
    focus_restore as _focus_restore,
)

async def focus_snapshot(args: dict[str, Any]) -> dict[str, Any]:
    return await _focus_snapshot(args)

async def focus_restore(args: dict[str, Any]) -> dict[str, Any]:
    return await _focus_restore(args)


# ── screen/clipboard X-Ray (Phase 2 — local capture + vision Q&A) ────────────

from blankslate.tools.xray import (
    screen_xray as _screen_xray,
    clipboard_read as _clipboard_read,
)

async def screen_xray(args: dict[str, Any]) -> dict[str, Any]:
    return await _screen_xray(args)

async def clipboard_read(args: dict[str, Any]) -> dict[str, Any]:
    return await _clipboard_read(args)


# ── voice macros (Phase 3 — JSON multi-step sequences) ───────────────────────

from blankslate.macros.runner import run_macro as _run_macro

async def run_macro(args: dict[str, Any]) -> dict[str, Any]:
    return await _run_macro(args)


# ── hardware & media (Phase 1 — local COM/Win32 only) ─────────────────────────

from blankslate.tools.hardware import (
    media_control as _media_control,
    mute_microphone as _mute_microphone,
    set_volume as _set_volume,
)

async def set_volume(args: dict[str, Any]) -> dict[str, Any]:
    return await _set_volume(args)


async def mute_microphone(args: dict[str, Any]) -> dict[str, Any]:
    return await _mute_microphone(args)


async def media_control(args: dict[str, Any]) -> dict[str, Any]:
    return await _media_control(args)


# ── local task planner (Phase 1) ───────────────────────────────────────────

from blankslate.tasks.service import (
    add_task as _add_task,
    complete_task as _complete_task,
    delete_task as _delete_task,
    import_ics as _import_ics,
    list_tasks as _list_tasks,
    set_reminder as _set_reminder,
)

async def add_task(args: dict[str, Any]) -> dict[str, Any]:
    return await _add_task(args)


async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    return await _list_tasks(args)


async def set_reminder(args: dict[str, Any]) -> dict[str, Any]:
    return await _set_reminder(args)


async def complete_task(args: dict[str, Any]) -> dict[str, Any]:
    return await _complete_task(args)


async def delete_task(args: dict[str, Any]) -> dict[str, Any]:
    return await _delete_task(args)


async def import_ics(args: dict[str, Any]) -> dict[str, Any]:
    return await _import_ics(args)


# ── proactive background event agents (Phase 2) ──────────────────────────────

from blankslate.monitor.watcher import (
    add_watch_rule as _add_watch_rule,
    list_watch_rules as _list_watch_rules,
    remove_watch_rule as _remove_watch_rule,
)

async def add_watch_rule(args: dict[str, Any]) -> dict[str, Any]:
    return await _add_watch_rule(args)


async def remove_watch_rule(args: dict[str, Any]) -> dict[str, Any]:
    return await _remove_watch_rule(args)


async def list_watch_rules(args: dict[str, Any]) -> dict[str, Any]:
    return await _list_watch_rules(args)


# ── local timeline (Phase 4) ─────────────────────────────────────────────────

from blankslate.timeline.tools import (
    search_timeline as _search_timeline,
    start_timeline as _start_timeline,
    stop_timeline as _stop_timeline,
)

async def start_timeline(args: dict[str, Any]) -> dict[str, Any]:
    return await _start_timeline(args)


async def stop_timeline(args: dict[str, Any]) -> dict[str, Any]:
    return await _stop_timeline(args)


async def search_timeline(args: dict[str, Any]) -> dict[str, Any]:
    return await _search_timeline(args)


# ���"?�"? floating overlay pill (six-feature expansion, Phase 1) ���"?�"?���"?�"?���"?�"?���"?�"?

from blankslate.overlay import show_pill as _overlay_show_pill


async def show_context_pill(args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("target", args.get("target_title", "")))
    text = str(args.get("text", ""))
    result = await _overlay_show_pill(target, text)
    if "error" in result:
        return {"tool": "show_context_pill", **result, "result": None}
    return {
        "tool": "show_context_pill",
        "target": result["target_title"],
        "x": result["x"],
        "y": result["y"],
        "result": f"Context pill shown near '{result['target_title']}'.",
        "reply": f"Showed a context pill near '{result['target_title']}'.",
        "deterministic": True,
    }


# Registry of all built-in handlers by tool name.
HANDLERS: dict[str, Any] = {
    "get_current_time": get_current_time,
    "calculator": calculator,
    "note_taking": note_taking,
    "web_search": web_search,
    "weather": weather,
    "app_launch": app_launch,
    "app_kill": app_kill,
    "list_windows": list_windows,
    "switch_window": switch_window,
    "get_foreground_window": get_foreground_window,
    "type_text": type_text,
    "press_key": press_key,
    "hotkey": hotkey,
    "focus_snapshot": focus_snapshot,
    "focus_restore": focus_restore,
    "screen_xray": screen_xray,
    "clipboard_read": clipboard_read,
    "run_macro": run_macro,
    "set_volume": set_volume,
    "mute_microphone": mute_microphone,
    "media_control": media_control,
    "add_task": add_task,
    "list_tasks": list_tasks,
    "set_reminder": set_reminder,
    "complete_task": complete_task,
    "delete_task": delete_task,
    "import_ics": import_ics,
    "add_watch_rule": add_watch_rule,
    "remove_watch_rule": remove_watch_rule,
    "list_watch_rules": list_watch_rules,
    "start_timeline": start_timeline,
    "stop_timeline": stop_timeline,
    "search_timeline": search_timeline,
    "show_context_pill": show_context_pill,
}
