"""Tool orchestrator — ties router, planner, built-in handlers, and MCP together.

Runs tool selection and execution as part of the intent pipeline. Returns
the executed tool results as a compact text block the LLM can ground its
answer in, plus the list of tool names for the HUD chips.

Design: keyword routing is the fast free path (no LLM round trip). The LLM
planner is optional and only invoked when enabled, to classify deeper
intent or disambiguate tool choice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from blankslate.config import DaemonConfig
from blankslate.llm.ollama_client import OllamaClient
from blankslate.tools.registry import ToolRegistry

logger = logging.getLogger("blankslate.tools.orchestrator")


@dataclass
class ToolOutcome:
    """Result of the orchestration pass for one user turn."""

    used_tools: list[str] = field(default_factory=list)
    results_block: str = ""  # formatted text fed back to the LLM ("" if none)
    note: str = ""
    # Deterministic status: when every executed tool returned a self-contained
    # `reply` (no LLM rephrasing needed), these fragments can be joined into the
    # final reply and the slow LLM round-trip can be skipped entirely.
    deterministic: bool = False
    reply_parts: list[str] = field(default_factory=list)


async def orchestrate(
    cfg: DaemonConfig,
    registry: ToolRegistry,
    llm: OllamaClient,
    query: str,
    mcp_clients: dict[str, object] | None = None,
    planner: object | None = None,
) -> ToolOutcome:
    """Select and execute tools for a query. Returns results for the LLM."""
    if not cfg.tools_enabled:
        return ToolOutcome()

    from blankslate.tools.router import ToolRouter

    router = ToolRouter(cfg, registry)
    routed = await router.route(query)
    selected = [t.name for t in routed.selected_tools]

    # If the LLM planner is enabled, merge its suggestion with keyword routing.
    planner_tools: list[str] = []
    if planner is not None:
        try:
            plan = await planner(query)
            planner_tools = plan.tools or []
        except Exception as exc:
            logger.warning("Planner failed during orchestration: %s", exc)

    tools_to_run = list(dict.fromkeys(selected + planner_tools))

    # When online and no tools matched, try web_search as fallback
    if not tools_to_run:
        from blankslate.tools.handlers import get_online_mode
        if get_online_mode():
            spec = registry.get("web_search")
            if spec:
                tools_to_run = ["web_search"]
                logger.info("No keyword match; falling back to web_search (online mode)")

    if not tools_to_run:
        return ToolOutcome()

    executed: list[str] = []
    blocks: list[str] = []
    reply_parts: list[str] = []
    all_deterministic = True
    for name in tools_to_run:
        block, det, reply_part = await _execute_one(cfg, registry, mcp_clients, name, query)
        if block is None:
            continue
        executed.append(name)
        blocks.append(block)
        if reply_part:
            reply_parts.append(reply_part)
        if not det:
            all_deterministic = False

    if not executed:
        return ToolOutcome()

    logger.info("Orchestrator: ran tools %s (deterministic=%s)", executed, all_deterministic)
    return ToolOutcome(
        used_tools=executed,
        results_block="\n\n".join(blocks),
        deterministic=bool(reply_parts) and all_deterministic,
        reply_parts=reply_parts,
    )


async def _execute_one(
    cfg: DaemonConfig,
    registry: ToolRegistry,
    mcp_clients: dict[str, object] | None,
    name: str,
    query: str,
) -> tuple[str | None, bool, str]:
    """Execute a single tool.

    Returns (block, deterministic, reply_part):
      - block:       formatted result block for the LLM (or None if no output)
      - deterministic: True if this tool produced a self-contained `reply`
      - reply_part:  the deterministic reply string ("" if none)
    """
    prefix = cfg.mcp_tool_prefix
    if mcp_clients and name.startswith(prefix):
        # MCP tools are treated as non-deterministic (free-form results).
        block = await _call_mcp(mcp_clients, name, query)
        return block, False, ""

    spec = registry.get(name)
    if spec is None or spec.handler is None:
        logger.info("Tool %s has no handler; skipping", name)
        return None, False, ""

    try:
        result = await spec.handler({"query": query})
    except Exception as exc:
        logger.exception("Tool %s handler raised", name)
        result = {"tool": name, "error": f"handler failed: {exc}"}

    # Deterministic short-circuit: handler gave a self-contained reply.
    det = bool(result.get("deterministic"))
    reply_part = str(result["reply"]) if det and result.get("reply") else ""

    if result.get("result") is None and not result.get("error"):
        # No "result" key: build the block from the remaining payload fields
        # (e.g. note_taking returns `saved`/`note`/`action`).
        rest = {k: v for k, v in result.items() if k not in ("tool", "reply", "deterministic")}
        if not rest:
            return None, det, reply_part
        return f"[{name}] {rest}", det, reply_part

    if result.get("error"):
        return f"[{name}] error: {result['error']}", False, ""

    # Network-only stubs: keep it to a short honest one-liner.
    if result.get("local_only"):
        return f"[{name}] unavailable (local-first build has no remote engine)", det, reply_part

    payload = result.get("result")
    if isinstance(payload, dict):
        payload = str(payload)
    return f"[{name}] {payload}", det, reply_part


async def _call_mcp(
    mcp_clients: dict[str, object], name: str, query: str
) -> str | None:
    """Call an MCP tool on the first available client (single-server default)."""
    for client in mcp_clients.values():
        try:
            resp = await client.call_tool(name, {"query": query})
        except Exception as exc:
            logger.exception("MCP call %s failed", name)
            return f"[{name}] error: {exc}"
        if "error" in resp:
            return f"[{name}] error: {resp['error']}"
        return f"[{name}] {resp.get('result', resp)}"
    return f"[{name}] error: no MCP client available"
