"""Digest / summarization of long context before it hits the primary model.

If the context buffer exceeds a token threshold, we summarize the older
turns into a compact digest and only pass the recent turns + digest
to the model — keeping num_ctx from thrashing.
"""

from __future__ import annotations

import logging

from blankslate.config import DaemonConfig
from blankslate.guardrail.redact import redact_text
from blankslate.llm.ollama_client import ChatMessage, OllamaClient

logger = logging.getLogger("blankslate.guardrail.digest")

# Rough token estimate: ~4 chars per token
_CHARS_PER_TOKEN = 4


class ContextDigester:
    """Compresses long context via a digest pass.

    If the messages exceed CHARS_TOKENS threshold, we summarize the
    oldest portion and keep only recent turns at full fidelity.
    """

    def __init__(self, cfg: DaemonConfig, llm: OllamaClient) -> None:
        self._cfg = cfg
        self._llm = llm
        self._max_raw_chars = int(cfg.num_ctx * _CHARS_PER_TOKEN * 0.7)  # 70% of context
        self._keep_recent = 6  # keep last 6 turns full-fidelity

    @property
    def max_raw_chars(self) -> int:
        return self._max_raw_chars

    async def prepare_messages(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Return messages ready for the primary model.

        If under threshold → unchanged. Over threshold → older turns
        replaced with a digest.
        """
        # Don't count system prompt
        content_messages = messages[1:] if messages and messages[0].role == "system" else messages
        system = messages[0] if messages and messages[0].role == "system" else None

        total_chars = sum(len(m.content) for m in content_messages)
        if total_chars <= self._max_raw_chars:
            return messages

        logger.info("Context over digest threshold (%d chars > %d), summarizing older turns",
                    total_chars, self._max_raw_chars)

        recent = content_messages[-self._keep_recent:]
        older = content_messages[:-self._keep_recent]

        digest_text = await self._summarize(older)
        digest_msg = ChatMessage(
            role="system",
            content=(
                "SUMMARY OF EARLIER CONVERSATION (referential only):\n"
                f"{digest_text}"
            ),
        )

        result = []
        if system:
            result.append(system)
        result.append(digest_msg)
        result.extend(recent)
        return result

    async def _summarize(self, older_messages: list[ChatMessage]) -> str:
        """Summarize older turns using the LLM."""
        transcript = "\n".join(
            f"{m.role.upper()}: {m.content}" for m in older_messages
        )
        prompt = (
            "Summarize the following conversation concisely, preserving "
            "key facts, names, and decisions. 100 words max.\n\n"
            f"{transcript}"
        )
        resp = await self._llm.generate(prompt)
        # Redact any secrets that slipped into the summary
        return redact_text(resp.content)
