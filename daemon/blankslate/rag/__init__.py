"""Local RAG: sqlite-vec conversation memory (Phase 4).

F4-approved pieces: OllamaClient.embed() + judge.py retrieval hook.
"""

from blankslate.rag.service import configure, ingest_count, retrieve_memory
from blankslate.rag.store import DEFAULT_MAX_DISTANCE, DIM, Chunk, VecStore

__all__ = [
    "DEFAULT_MAX_DISTANCE",
    "DIM",
    "Chunk",
    "VecStore",
    "configure",
    "ingest_count",
    "retrieve_memory",
]
