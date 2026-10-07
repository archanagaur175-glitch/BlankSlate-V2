"""Intent judge — routes text input through intent → context → tools → LLM → reply.

No directedness-gating, no keyword heuristics. Any input that reaches
this handler IS a deliberate user turn (non-None text).

Phase 5: wires in redaction-persistent store and context digesting.
Phase 8: wires tool orchestration (router + planner + handlers + MCP) so
queries can trigger real local tool execution before the LLM composes a
grounded reply. Tool names surface in `IntentResult.tools` for the HUD.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.guardrail.digest import ContextDigester
from blankslate.guardrail.redact import redact_text
from blankslate.llm.ollama_client import ChatMessage, OllamaClient
from blankslate.voice.stress import apply_stress_bias

logger = logging.getLogger("blankslate.intent")


@dataclass(frozen=True)
class IntentResult:
    reply: str
    intent: str  # "chat" | "empty" | "tool" | "multi_tool" | "command" | ...
    redacted_input: str = field(default="")
    tools: tuple[str, ...] = field(default=())
    rag_hits: int = field(default=0)


@dataclass
class IntentHandlers:
    ctx: ContextManager
    llm: OllamaClient
    store: Any = None          # optional ContextStore
    digester: Any = None       # optional ContextDigester
    persist_enabled: bool = True
    cfg: DaemonConfig | None = None  # for tool orchestration
    registry: Any = None              # optional ToolRegistry
    mcp_clients: Any = None           # optional dict[str, MCPClient]
    planner: Any = None               # optional plan_task callable


async def handle_text_input(
    text: str,
    ctx: ContextManager,
    llm: OllamaClient,
    store: Any = None,
    digester: Any = None,
    persist_enabled: bool = True,
    cfg: DaemonConfig | None = None,
    registry: Any = None,
    mcp_clients: Any = None,
    planner: Any = None,
    stress: dict[str, Any] | None = None,
    stress_enabled: bool = True,
) -> IntentResult:
    """Process a user text turn end to end.

    NO directedness gate — any non-None text is a user turn.
    Empty/whitespace-only → graceful reply (not silent drop).
    Tool orchestration runs when a cfg with tools_enabled is provided.

    Phase 3 (voice-stress): `stress` is the per-utterance reading computed
    on the VOICE path only; text turns never pass it (zero delta). The
    advisory suffix is appended in apply_stress_bias — enabled, urgency
    threshold and the explicit-detail keyword guard all live there.
    """
    stripped = text.strip()

    if not stripped:
        reply = "I didn't receive any text. Could you try again?"
        logger.info("Empty input handled gracefully.")
        return IntentResult(reply=reply, intent="empty")

    # Redact input for persistence BEFORE persisting anything
    redacted_input = redact_text(stripped)

    # Persist the user turn (redacted) BEFORE processing
    if store is not None and persist_enabled:
        try:
            store.append(role="user", content=redacted_input, source="text")
        except Exception:
            logger.exception("Failed to persist user turn — continuing anyway")

    # Add user turn to short-term context
    ctx.add_user_turn(stripped)
    logger.info("User turn added (total turns: %d)", ctx.turn_count)

    # ── Macro hook (Phase 3, F4-approved) ──
    # Deterministic exact match after prefix strip ("run my <name>").
    # Runs through the same macro runner the tool path uses; any failure
    # logs and falls through to normal intent handling.
    if registry is not None:
        try:
            from blankslate.macros.runner import run_macro
            from blankslate.macros.store import find_macro

            macro_name = find_macro(stripped)
            if macro_name:
                outcome = await run_macro({"name": macro_name}, registry)
                reply = str(outcome.get("error") or outcome.get("reply") or "Done.")
                ctx.add_assistant_turn(reply)
                if store is not None and persist_enabled:
                    try:
                        store.append(
                            role="assistant", content=redact_text(reply), source="tool"
                        )
                    except Exception:
                        logger.exception("Failed to persist macro turn")
                logger.info(
                    "Macro hook executed '%s' (no LLM, error=%s)",
                    macro_name,
                    bool(outcome.get("error")),
                )
                return IntentResult(
                    reply=reply,
                    intent="tool",
                    redacted_input=redacted_input,
                    tools=("run_macro",),
                )
        except Exception:
            logger.exception("Macro hook failed — continuing to normal intent")

    # ── Tool orchestration (Phase 8) ──
    used_tools: tuple[str, ...] = ()
    tool_results: str = ""
    outcome = None
    tool_cfg = cfg if (cfg is not None and cfg.tools_enabled and registry is not None) else None
    if tool_cfg is not None:
        try:
            from blankslate.tools.orchestrator import orchestrate

            outcome = await orchestrate(
                tool_cfg, registry, llm, stripped,
                mcp_clients=mcp_clients, planner=planner,
            )
            used_tools = tuple(outcome.used_tools)
            tool_results = outcome.results_block
        except Exception:
            logger.exception("Tool orchestration failed — continuing without tools")

    # ── Deterministic short-circuit (Phase 9 fixes) ──
    # When every executed tool returned a self-contained reply, compose the
    # answer directly from real tool output. This removes the slow LLM round
    # trip (latency) and avoids the small model echoing/paraphrasing tool
    # output incorrectly (reply quality).
    if used_tools and outcome.deterministic:
        reply = " ".join(part for part in outcome.reply_parts if part)
        if not reply:
            reply = tool_results or "Done."
        intent = "multi_tool" if len(used_tools) > 1 else "tool"

        # Persist user + assistant turns for continuity.
        ctx.add_assistant_turn(reply)
        if store is not None and persist_enabled:
            try:
                store.append(role="assistant", content=redact_text(reply), source="tool")
            except Exception:
                logger.exception("Failed to persist assistant tool turn")

        logger.info("Tool turn answered deterministically (no LLM): tools=%s", used_tools)
        return IntentResult(
            reply=reply,
            intent=intent,
            redacted_input=redacted_input,
            tools=used_tools,
        )

    # ── RAG hook (Phase 4, F4-approved) ──
    # LLM-bound path only: ingest closed turns into the vector store, then
    # retrieve top-k past-conversation chunks for THIS query. Any failure
    # logs and falls through — memory must never break normal chat.
    rag_hits = 0
    rag_snippets: list[str] = []
    if store is not None and persist_enabled:
        try:
            from blankslate.rag.service import retrieve_memory

            rag_snippets = await retrieve_memory(store, llm, stripped)
            rag_hits = len(rag_snippets)
        except Exception:
            logger.exception("RAG hook failed — continuing without memory")

    # Build messages, applying digest if context is too long
    messages = ctx.get_messages()
    if digester is not None:
        try:
            messages = await digester.prepare_messages(messages)
        except Exception:
            logger.exception("Digest pass failed — sending full context")

    # Inject retrieved long-term memory as system context (Phase 4).
    if rag_snippets:
        memory_message = ChatMessage(
            role="system",
            content=(
                "Relevant past conversation memory (may be stale). Use it to "
                "answer only if it actually helps this question:\n"
                + "\n".join(f"- {s}" for s in rag_snippets)
            ),
        )
        messages = list(messages) + [memory_message]
        logger.info("RAG: injected %d memory chunk(s)", len(rag_snippets))

    # Feed tool results back to the LLM so the answer is grounded in them.
    if tool_results:
        tool_message = ChatMessage(
            role="user",
            content=(
                "Use the following tool outputs to answer the user's question. "
                "Report them to the user naturally; if a tool reported it is "
                "not configured, say so honestly.\n\n"
                f"Tool results:\n{tool_results}"
            ),
        )
        messages = list(messages) + [tool_message]

    # Voice-stress bias (Phase 3): advisory brevity suffix, appended LAST so
    # digest/memory/tool messages are untouched. Byte-identical assembly in
    # every non-applying case (disabled / calm / explicit detail request).
    n_before = len(messages)
    messages = apply_stress_bias(
        messages, stress, enabled=stress_enabled, text=stripped
    )
    if len(messages) > n_before:
        logger.info(
            "stress bias applied: urgency=%.2f band=%s",
            float(stress.get("urgency", 0.0)) if stress else 0.0,
            str((stress or {}).get("band", "?")),
        )

    # Send to LLM
    response = await llm.chat(messages)

    # Store assistant reply in context + persist
    ctx.add_assistant_turn(response.content)
    if store is not None and persist_enabled:
        try:
            redacted_reply = redact_text(response.content)
            store.append(role="assistant", content=redacted_reply, source="text")
        except Exception:
            logger.exception("Failed to persist assistant turn")

    logger.info("Assistant turn added (total turns: %d)", ctx.turn_count)

    intent = "multi_tool" if len(used_tools) > 1 else ("tool" if used_tools else "chat")
    return IntentResult(
        reply=response.content,
        intent=intent,
        redacted_input=redacted_input,
        tools=used_tools,
        rag_hits=rag_hits,
    )
