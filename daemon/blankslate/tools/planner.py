"""Pre-loop task planner — decides whether a query needs tools and plans steps.

A lightweight LLM-driven planner that classifies a user query:
  - intent: chat | tool | multi_tool
  - selected_tools: which tools (from registry)
  - needs_followup: whether clarification is needed

The planner runs BEFORE the main loop, so the task loop can execute the
relevant tools and then compose a final answer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from blankslate.config import DaemonConfig
from blankslate.llm.ollama_client import ChatMessage, OllamaClient
from blankslate.tools.registry import ToolRegistry

logger = logging.getLogger("blankslate.tools.planner")


@dataclass
class Plan:
    intent: str  # "chat" | "tool" | "multi_tool"
    tools: list[str]
    reasoning: str
    raw: str = ""
    needs_followup: bool = False

    @property
    def uses_tools(self) -> bool:
        return len(self.tools) > 0


async def plan_task(
    query: str,
    llm: OllamaClient,
    registry: ToolRegistry,
) -> Plan:
    """Plan a task BEFORE the main loop executes."""
    tool_catalog = ", ".join(registry.names())

    prompt = (
        "You are a task planner. Decide whether the user query needs tool use.\n"
        f"Available tools: {tool_catalog}\n"
        "Respond with JSON: {\"intent\": \"chat\"|\"tool\"|\"multi_tool\", "
        "\"tools\": [list of tool names to use, empty if none], "
        "\"needs_followup\": bool}\n\n"
        f"User query: {query}\n\nJSON:"
    )

    messages = [ChatMessage(role="user", content=prompt)]
    resp = await llm.chat(messages, num_ctx=512)

    try:
        # Extract JSON from response (model may wrap it)
        text = resp.content.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
        else:
            data = {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("Planner returned non-JSON, defaulting to chat: %s", resp.content[:80])
        return Plan(intent="chat", tools=[], reasoning="planner parse failure", raw=resp.content)

    intent = data.get("intent", "chat")
    tools = [t for t in data.get("tools", []) if registry.get(t)]
    needs_followup = bool(data.get("needs_followup", False))

    logger.info("Planner: query='%s' → intent=%s tools=%s", query, intent, tools)
    return Plan(
        intent=intent,
        tools=tools,
        reasoning=data.get("reasoning", ""),
        raw=resp.content,
        needs_followup=needs_followup,
    )
