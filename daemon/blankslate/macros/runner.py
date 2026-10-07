"""Sequential macro execution — abort on first failure, reuse the registry.

Reuse rule: steps execute via registry.get(name).handler(...) when a registry
is given (judge/tool paths pass the same callables the orchestrator uses);
HANDLERS[tool] fallback is the identical function object for callers without
a registry instance.

Resilience (macro-level only): one targeted retry when a step error looks
like transient clipboard contention (F6-class OpenClipboard error 5 between
back-to-back type_text steps). text_injector itself is NOT modified — F6
remains a separate pending decision for single-shot voice typing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from blankslate.macros.store import extract_macro_name, load_macro

logger = logging.getLogger("blankslate.macros")

_TRANSIENT_RE = re.compile(
    r"OpenClipboard|clipboard busy|Access is denied|\berror 5\b", re.IGNORECASE
)
_RETRY_DELAY_S = 0.15


def _handler_for(tool: str, registry: Any):
    if registry is not None:
        spec = registry.get(tool)
        if spec is not None and spec.handler is not None:
            return spec.handler
    from blankslate.tools.handlers import HANDLERS  # lazy: handlers imports this module

    return HANDLERS.get(tool)


def _abort(reply: str, step_log: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tool": "run_macro",
        "error": reply,
        "result": None,
        "deterministic": False,
        "steps_run": len(step_log),
        "step_log": step_log,
    }


async def run_macro(
    args: dict[str, Any], registry: Any = None
) -> dict[str, Any]:
    """Run a macro. args: structured {"name"} or NL {"query"} (prefix-stripped)."""
    name = str(args.get("name") or "").strip() or extract_macro_name(
        str(args.get("query") or "")
    )
    data, errors = load_macro(name)
    if errors:
        return _abort("; ".join(errors), [])

    assert data is not None  # errors==[] implies data
    steps: list[dict[str, Any]] = data["steps"]
    display = str(data.get("name") or name).strip() or name
    step_log: list[dict[str, Any]] = []

    for i, step in enumerate(steps):
        tool = str(step["tool"]).strip()
        step_args = dict(step.get("args") or {})
        handler = _handler_for(tool, registry)
        if handler is None:
            return _abort(f"Macro failed at step {i}: tool '{tool}' unavailable", step_log)

        result: dict[str, Any] | None = None
        for attempt in (1, 2):
            try:
                result = await handler(step_args)
            except Exception as exc:
                logger.exception("Macro step %d (%s) raised", i, tool)
                if attempt == 1 and _TRANSIENT_RE.search(str(exc)):
                    await asyncio.sleep(_RETRY_DELAY_S)
                    continue
                return _abort(
                    f"Macro failed at step {i}: {tool} raised: {exc}", step_log
                )
            err = str(result.get("error") or "")
            if err and attempt == 1 and _TRANSIENT_RE.search(err):
                await asyncio.sleep(_RETRY_DELAY_S)
                continue
            break
        if result is None or result.get("error"):
            err = str((result or {}).get("error") or "no result")
            return _abort(f"Macro failed at step {i}: {tool}: {err}", step_log)

        step_log.append({"step": i, "tool": tool, "ok": True})

    reply = f"Ran macro '{display}': {len(steps)} steps completed."
    logger.info("Macro '%s' completed (%d steps)", display, len(steps))
    return {
        "tool": "run_macro",
        "reply": reply,
        "result": reply,
        "deterministic": True,
        "steps_run": len(steps),
        "step_log": step_log,
    }
