"""Tool router — selects only the relevant tools for a query.

Uses two signals:
  1. Embedding similarity (via nomic-embed-text on Ollama)
  2. Keyword overlap (fast lexical fallback)

Returns only tools whose relevance exceeds a threshold — never the
full catalog dumped into context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from blankslate.config import DaemonConfig
from blankslate.tools.registry import ToolRegistry, ToolSpec

logger = logging.getLogger("blankslate.tools.router")


@dataclass
class RoutingResult:
    selected_tools: list[ToolSpec]
    by_embedding: list[str]
    by_keyword: list[str]
    query: str


class ToolRouter:
    # Tools with no local engine. They only trigger on explicit keyword intent,
    # never on loose embedding similarity — avoids spurious routing and keeps
    # the tiny LLM's embedding noise from surfacing non-functional stubs.
    _NETWORK_STUBS = frozenset({"web_search", "weather"})

    def __init__(self, cfg: DaemonConfig, registry: ToolRegistry) -> None:
        self._cfg = cfg
        self._registry = registry
        self._embedding_cache: dict[str, list[float]] = {}
        self._min_keyword_score = 1  # at least 1 keyword hit to select

    async def route(self, query: str) -> RoutingResult:
        """Route a query to the relevant tool subset.

        Keyword routing is the fast, free path (no LLM round trip). The
        embedding pass is only invoked when keywords find nothing, so common
        tool queries stay low-latency even on a slow local model.
        """
        by_keyword = self._keyword_route(query)
        by_embedding: list[str] = []
        if not by_keyword and self._cfg.router_embedding:
            # Optional LLM pass for non-keyword queries; off by default to keep
            # latency low on local models. Without it, non-keyword queries
            # simply become pure chat.
            by_embedding = await self._embedding_route(query)

        # Combine: union of keyword and embedding hits, dedup preserving registry order.
        # Network-only stubs are kept only on explicit keyword intent.
        selected_names: list[str] = []
        for name in self._registry.names():
            kw = name in by_keyword
            emb = name in by_embedding
            if name in self._NETWORK_STUBS and not kw:
                emb = False  # embedding similarity alone must not trigger stubs
            if kw or emb:
                selected_names.append(name)

        selected = [self._registry.get(n) for n in selected_names if self._registry.get(n)]
        logger.info(
            "Router: query='%s' → %d/%d tools selected (kw=%s, emb=%s)",
            query,
            len(selected),
            self._registry.count,
            by_keyword,
            by_embedding,
        )
        return RoutingResult(
            selected_tools=selected,
            by_embedding=by_embedding,
            by_keyword=by_keyword,
            query=query,
        )

    def _keyword_route(self, query: str) -> list[str]:
        """Lexical keyword matching (word-boundary aware).

        Uses `\\b` anchors so partial-word hits don't trigger tools: e.g. the
        word "times" must NOT match the clock tool's "time" keyword.
        Multi-word keywords (e.g. "when is it") are handled by collapsing
        whitespace first.
        """
        import re

        q = re.sub(r"\s+", " ", query.lower()).strip()
        matches = []
        for spec in self._registry.all():
            score = 0
            for kw in spec.keywords:
                pattern = r"\b" + re.escape(kw.strip()) + r"\b"
                if re.search(pattern, q):
                    score += 1
            if score >= self._min_keyword_score:
                matches.append(spec.name)
        return matches

    async def _embedding_route(self, query: str) -> list[str]:
        """Embedding-based routing (best-effort; falls back to keyword on failure)."""
        try:
            from blankslate.llm.ollama_client import OllamaClient, ChatMessage

            llm = OllamaClient(self._cfg)
            # Build a matching prompt against tool descriptions
            tool_list = "\n".join(
                f"- {s.name}: {s.description} (keywords: {', '.join(s.keywords)})"
                for s in self._registry.all()
            )
            prompt = (
                "Given the user query below, which tools are relevant? "
                "Return ONLY a comma-separated list of tool names. If none, return NONE.\n\n"
                f"Available tools:\n{tool_list}\n\nUser query: {query}\n\nTools:"
            )
            resp = await llm.generate(prompt, num_ctx=512)
            response_text = resp.content.strip()
            await llm.close()

            if response_text.upper() == "NONE" or not response_text:
                return []

            # Parse tool names from response
            selected = []
            for part in response_text.replace("\n", ",").split(","):
                name = part.strip().strip(".").strip("`").lower()
                if name and name not in selected and self._registry.get(name):
                    selected.append(name)
            return selected

        except Exception as exc:
            logger.warning("Embedding routing failed (%s), using keyword only", exc)
            return []


async def route_tools(cfg: DaemonConfig, registry: ToolRegistry, query: str) -> RoutingResult:
    """Convenience wrapper."""
    router = ToolRouter(cfg, registry)
    return await router.route(query)
