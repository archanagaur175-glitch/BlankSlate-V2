"""Macro storage: JSON files in ./macros (cwd), validated at load.

F2/F3 = macro-level validation ONLY (approved directive): structure checks,
per-tool required-argument checks, recursion and path-traversal guards.
No changes to app_manager.py / text_injector.py core logic.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("blankslate.macros")

# Tools a macro may contain, with the args that must be non-empty (at least
# one of the listed keys). Read-only tools need nothing. run_macro is
# deliberately absent — it is rejected explicitly (recursion guard).
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "type_text": ("text", "query"),
    "press_key": ("key", "query"),
    "hotkey": ("combo", "keys", "query"),
    "app_launch": ("app_id", "app", "query"),
    "app_kill": ("target", "process", "query"),
    "switch_window": ("title", "hwnd", "query"),
    "web_search": ("query",),
    "calculator": ("expression", "query"),
    "weather": ("location", "city", "query"),
}

_ALLOWED_STEP_KEYS = {"tool", "args"}

_PREFIX_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+|i want to\s+|let'?s\s+)*"
    r"(?:run|execute|do|start|play|invoke)\s+"
    r"(?:the\s+|my\s+|a\s+|an\s+|that\s+)*"
    r"(?:macro\s+|routine\s+)?",
    re.IGNORECASE,
)


def macros_dir() -> Path:
    d = Path.cwd() / "macros"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_macros() -> list[str]:
    try:
        return sorted(p.stem for p in macros_dir().glob("*.json"))
    except OSError as exc:
        logger.exception("list_macros failed")
        raise RuntimeError(f"could not list macros: {exc}") from exc


def _norm(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().strip("'\"").lower())


def extract_macro_name(query: str) -> str:
    """Strip polite/verb prefixes: 'run my morning routine' -> 'morning routine'."""
    q = (query or "").strip().strip(" .!?,\"'")
    q = _PREFIX_RE.sub("", q, count=1)
    return q.strip().strip("'\"")


def find_macro(query: str) -> str | None:
    """Match a user phrase to a saved macro stem, or None (never raises)."""
    try:
        stems = list_macros()
    except Exception:
        return None
    if not stems:
        return None
    candidates = {_norm(extract_macro_name(query)), _norm(query)}
    candidates.discard("")
    by_norm = {_norm(s): s for s in stems}
    for cand in candidates:
        if cand in by_norm:
            return by_norm[cand]
    return None


def validate_macro(data: Any) -> list[str]:
    """F2/F3 validation. Returns [] when the macro is runnable.

    F3 (structure): object with a non-empty 'steps' array; every step is an
      object with a known 'tool' (registry-backed) and only tool/args keys.
    F2 (arguments): required args present per tool BEFORE anything runs.
    Guards: run_macro-inside-macro (recursion), args must be an object.
    """
    if not isinstance(data, dict):
        return ["macro must be a JSON object"]
    errors: list[str] = []

    name = data.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        errors.append("'name' must be a non-empty string when present")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("'steps' must be a non-empty array")
        return errors

    # Lazy import: handlers imports this package at module level.
    from blankslate.tools.handlers import HANDLERS

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {i}: must be an object")
            continue
        extra = set(step) - _ALLOWED_STEP_KEYS
        if extra:
            errors.append(f"step {i}: unknown keys {sorted(extra)}")
            continue
        tool = step.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            errors.append(f"step {i}: missing 'tool'")
            continue
        tool = tool.strip()
        if tool == "run_macro":
            errors.append(f"step {i}: 'run_macro' not allowed inside macros (recursion)")
            continue
        if tool not in HANDLERS:
            errors.append(f"step {i}: unknown tool '{tool}'")
            continue
        args = step.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            errors.append(f"step {i} ({tool}): 'args' must be an object")
            continue
        required = _REQUIRED_ARGS.get(tool)
        if required and not any(str(args.get(k, "") or "").strip() for k in required):
            errors.append(f"step {i} ({tool}): needs one of {required}")
    return errors


def load_macro(name: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Load + validate a macro by name. Returns (data, errors); errors non-empty on failure."""
    clean = (name or "").strip()
    if not clean:
        return None, ["no macro name given (say 'run <macro name>')"]
    if any(sep in clean for sep in ("..", "/", "\\")) or not re.fullmatch(r"[\w \-]+", clean):
        return None, [f"invalid macro name {clean!r}"]
    path = macros_dir() / f"{clean}.json"
    if not path.exists():
        available = ", ".join(list_macros()) or "(none)"
        return None, [f"macro '{clean}' not found; available: {available}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"macro '{clean}' is not valid JSON: {exc}"]
    except OSError as exc:
        return None, [f"macro '{clean}' could not be read: {exc}"]
    errors = validate_macro(data)
    if errors:
        return data, [f"macro '{clean}' invalid — {e}" for e in errors]
    return data, []
