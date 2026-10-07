"""Rolling short-term context buffer.

Stores conversation turns in memory. Provides messages formatted
for the LLM. Max turns configurable via DaemonConfig.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from blankslate.config import DaemonConfig
from blankslate.llm.ollama_client import ChatMessage

logger = logging.getLogger("blankslate.context")

SYSTEM_PROMPT = (
    "You are BlankSlate, a concise local-first desktop AI assistant. "
    "Be brief and direct. Do not use markdown formatting. "
    "You have access to tools: time, calculator, notes, apps, windows, text input, "
    "and web search (when online mode is enabled). "
    "If tool results are provided in the conversation, use them to answer — "
    "do not make up information that tools could verify."
)


@dataclass
class Turn:
    role: str
    content: str
    timestamp: float


class ContextManager:
    """Rolling conversation context.

    Holds up to `max_turns` exchanges. Provides:
    - get_messages() → list[ChatMessage] with system prompt + history
    - add_user_turn(content)
    - add_assistant_turn(content)
    - clear()
    """

    def __init__(self, cfg: DaemonConfig, max_turns: int = 20) -> None:
        self._max_turns = max_turns
        self._system = ChatMessage(role="system", content=SYSTEM_PROMPT)
        self._turns: list[Turn] = []

    def add_user_turn(self, content: str) -> None:
        self._turns.append(Turn(role="user", content=content, timestamp=time.time()))
        self._trim()

    def add_assistant_turn(self, content: str) -> None:
        self._turns.append(Turn(role="assistant", content=content, timestamp=time.time()))
        self._trim()

    def get_messages(self) -> list[ChatMessage]:
        msgs = [self._system]
        for t in self._turns:
            msgs.append(ChatMessage(role=t.role, content=t.content))
        return msgs

    def clear(self) -> None:
        self._turns.clear()
        logger.info("Context cleared.")

    def _trim(self) -> None:
        while len(self._turns) > self._max_turns:
            self._turns.pop(0)

    @property
    def turn_count(self) -> int:
        return len(self._turns)
