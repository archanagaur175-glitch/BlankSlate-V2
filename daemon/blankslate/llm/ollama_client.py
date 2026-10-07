"""Ollama HTTP client with bounded retry on transient failures.

Single source of truth for num_ctx, timeouts, model — all from config.py.
No getattr(cfg, X, fallback) with a different default anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from blankslate.config import DaemonConfig

logger = logging.getLogger("blankslate.llm")


class OllamaError(Exception):
    """Raised when Ollama returns an error or is unreachable after retries."""


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ChatResponse:
    content: str
    model: str
    total_duration_ms: float
    eval_count: int = 0


class OllamaClient:
    """Async Ollama client with transient-failure retry.

    All numeric parameters come from DaemonConfig — no duplicates.
    """

    def __init__(self, cfg: DaemonConfig) -> None:
        self._cfg = cfg
        self._base_url = cfg.ollama_url
        self._model = cfg.ollama_model
        self._num_ctx = cfg.num_ctx
        self._chat_timeout = cfg.chat_timeout
        self._retry_attempts = cfg.ollama_retry_attempts
        self._retry_base_delay = cfg.ollama_retry_base_delay
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self._chat_timeout,
                    write=10.0,
                    pool=10.0,
                ),
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        num_ctx: int | None = None,
    ) -> ChatResponse:
        """Send a chat completion request with bounded retry.

        Retries on transient connection errors (e.g. Ollama cold-start GPU drop).
        Raises OllamaError after all retries exhausted.
        """
        ctx = num_ctx if num_ctx is not None else self._num_ctx
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                "num_ctx": ctx,
            },
        }

        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                t0 = time.perf_counter()
                client = await self._get_client()
                resp = await client.post("/api/chat", json=payload)
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - t0) * 1000

                data = resp.json()
                message = data.get("message", {})
                content = message.get("content", "")
                total_dur = data.get("total_duration", 0) / 1e6  # ns → ms
                eval_count = data.get("eval_count", 0)

                logger.info(
                    "Ollama chat ok: model=%s eval=%d latency=%.0fms",
                    self._model,
                    eval_count,
                    elapsed_ms,
                )
                return ChatResponse(
                    content=content,
                    model=self._model,
                    total_duration_ms=total_dur,
                    eval_count=eval_count,
                )

            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
                last_error = exc
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Ollama attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt,
                    self._retry_attempts,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

            except httpx.HTTPStatusError as exc:
                raise OllamaError(
                    f"Ollama HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise OllamaError(
            f"Ollama unreachable after {self._retry_attempts} attempts: {last_error}"
        )

    async def vision(
        self,
        question: str,
        image_b64: str,
        *,
        model: str = "moondream",
        keep_alive: str = "5m",
        num_ctx: int | None = None,
    ) -> ChatResponse:
        """Local vision Q&A: one image + question, bounded retry (Phase 2 / F4 hook).

        Self-contained retry loop — chat() untouched. keep_alive controls how long
        the vision model stays resident in VRAM after the call ("5m" warm,
        "0" unload immediately) — see xray.benchmark_vision_vram().
        Raises OllamaError after all retries exhausted.
        """
        ctx = num_ctx if num_ctx is not None else self._num_ctx
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": question, "images": [image_b64]}
            ],
            "stream": False,
            "keep_alive": keep_alive,
            "options": {"num_ctx": ctx},
        }

        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                t0 = time.perf_counter()
                client = await self._get_client()
                resp = await client.post("/api/chat", json=payload)
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - t0) * 1000

                data = resp.json()
                message = data.get("message", {})
                content = message.get("content", "")
                total_dur = data.get("total_duration", 0) / 1e6
                eval_count = data.get("eval_count", 0)

                logger.info(
                    "Ollama vision ok: model=%s eval=%d latency=%.0fms",
                    model,
                    eval_count,
                    elapsed_ms,
                )
                return ChatResponse(
                    content=content,
                    model=model,
                    total_duration_ms=total_dur,
                    eval_count=eval_count,
                )

            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
                last_error = exc
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Ollama vision attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt,
                    self._retry_attempts,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

            except httpx.HTTPStatusError as exc:
                raise OllamaError(
                    f"Ollama HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise OllamaError(
            f"Ollama vision unreachable after {self._retry_attempts} attempts: {last_error}"
        )

    async def embed(
        self,
        texts: list[str],
        *,
        model: str = "nomic-embed-text",
    ) -> list[list[float]]:
        """Embed a batch of texts locally (Phase 4 / F4 hook).

        Self-contained retry loop — chat() untouched. Returns one vector
        per input text (nomic-embed-text: 768 dims).
        Raises OllamaError after all retries exhausted.
        """
        if not texts:
            return []
        payload = {"model": model, "input": [str(t) for t in texts]}

        last_error: Exception | None = None
        for attempt in range(1, self._retry_attempts + 1):
            try:
                t0 = time.perf_counter()
                client = await self._get_client()
                resp = await client.post("/api/embed", json=payload)
                resp.raise_for_status()
                elapsed_ms = (time.perf_counter() - t0) * 1000

                data = resp.json()
                embeddings = data.get("embeddings") or []
                if len(embeddings) != len(texts):
                    raise OllamaError(
                        f"Ollama embed returned {len(embeddings)} vectors "
                        f"for {len(texts)} inputs"
                    )
                vectors = [[float(x) for x in vec] for vec in embeddings]

                logger.info(
                    "Ollama embed ok: model=%s count=%d latency=%.0fms",
                    model,
                    len(vectors),
                    elapsed_ms,
                )
                return vectors

            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as exc:
                last_error = exc
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Ollama embed attempt %d/%d failed (%s), retrying in %.1fs",
                    attempt,
                    self._retry_attempts,
                    type(exc).__name__,
                    delay,
                )
                await asyncio.sleep(delay)

            except httpx.HTTPStatusError as exc:
                raise OllamaError(
                    f"Ollama HTTP {exc.response.status_code}: {exc.response.text}"
                ) from exc

        raise OllamaError(
            f"Ollama embed unreachable after {self._retry_attempts} attempts: {last_error}"
        )

    async def generate(self, prompt: str, *, num_ctx: int | None = None) -> ChatResponse:
        """Single-turn generate convenience wrapper."""
        return await self.chat([ChatMessage(role="user", content=prompt)], num_ctx=num_ctx)

    async def health(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            client = await self._get_client()
            resp = await client.get("/api/tags")
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.ReadTimeout):
            return False
