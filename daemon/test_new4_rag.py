"""PHASE 4 GATE — Local RAG (sqlite-vec + judge retrieval hook).

T1  sqlite-vec roundtrip: synthetic 768-d vectors, KNN ranking + threshold
T2  redaction choke point: secret-shaped text never stored raw in vector DB
T3  OllamaClient.embed(): real nomic-embed-text call, correct dims
T4  e2e judge RAG: fact exists ONLY in persisted store -> reply surfaces it
T5  delta ingestion idempotency (cursor advances once, then stable)
R1  regression: normal chat intent/tools unchanged with judge hooked
R2  regression: normal tool routing + deterministic execution unchanged

Ground truth: DB contents (SELECT from sqlite), IntentResult.rag_hits,
and reply text — never handler-registry optimism.
"""

from __future__ import annotations

import asyncio
import io
import math
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.guardrail.store import ContextStore
from blankslate.intent.judge import handle_text_input
from blankslate.llm.ollama_client import OllamaClient
from blankslate.rag import configure as rag_configure
from blankslate.rag import ingest_count, retrieve_memory
from blankslate.rag.store import DEFAULT_MAX_DISTANCE, DIM, VecStore
from blankslate.tools.orchestrator import orchestrate
from blankslate.tools.registry import ToolRegistry

cfg = DaemonConfig()
object.__setattr__(cfg, "tools_enabled", True)
llm = OllamaClient(cfg)
registry = ToolRegistry()

results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'OK' if ok else 'FAIL'}   {name}" + (f" — {note}" if note else ""))


def basis(i: int, dim: int = DIM) -> list[float]:
    vec = [0.0] * dim
    vec[i] = 1.0
    return vec


def cos_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return 1.0 - dot / (na * nb)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bs_rag_gate_"))
    t1_db = tmp / "t1.sqlite3"
    rag_db = tmp / "rag_e2e.sqlite3"
    cstore_dir = tmp / "history"

    try:
        # Isolated RAG DB + isolated conversation store (no real-data pollution)
        rag_configure(rag_db)
        store = ContextStore(cfg, data_dir=cstore_dir)

        # ── T1: sqlite-vec roundtrip (no Ollama) ──
        print("\n--- T1: sqlite-vec KNN roundtrip (synthetic vectors) ---")
        vs = VecStore(t1_db)
        a, b = basis(0), basis(1)
        n = vs.add(["alpha chunk about teal", "beta chunk about rust"], ["t1", "t1"], [a, b])
        hits = vs.search(a, k=2)
        ranked_ok = n == 2 and hits and hits[0].text.startswith("alpha")
        near_ok = bool(hits) and hits[0].distance < 0.01
        within = [h for h in hits if h.distance <= DEFAULT_MAX_DISTANCE]
        thresh_ok = len(within) == 1 and within[0].text.startswith("alpha")
        cursor_ok = (vs.get_cursor() == 0)
        vs.set_cursor(7)
        cursor_ok = cursor_ok and vs.get_cursor() == 7
        print(f"  count={vs.count()} top={hits[0].text!r} d0={hits[0].distance:.4f} "
              f"d1={hits[1].distance:.4f} within_threshold={len(within)}")
        vs.close()  # conn closed first; encrypt-on-close needs no other handles
        probe = VecStore(t1_db)  # decrypt-on-open: fresh connection reads persisted cursor
        cursor_ok = cursor_ok and probe.get_cursor() == 7
        probe.close()  # already an envelope -> no rewrite; releases the lock
        rec("T1 sqlite-vec KNN + threshold + cursor", bool(ranked_ok and near_ok and thresh_ok and cursor_ok))

        # ── T2: redaction choke point ──
        print("\n--- T2: redaction choke point at VecStore.add ---")
        secret = "sk-or-v1-abcdef1234567890abcdef1234567890"
        vs2 = VecStore(t1_db)
        vs2.add([f"config note: key={secret} end"], ["t2"], [basis(2)])
        raw_row = vs2.get_text(3) if vs2.count() >= 3 else None
        raw_row = raw_row or vs2.search(basis(2), k=1)[0].text
        redacted_ok = ("[REDACTED:" in raw_row) and (secret not in raw_row)
        print(f"  stored chunk: {raw_row!r}")
        rec("T2 redact_text applied before storage", redacted_ok)
        vs2.close()

        # ── T3: embed() real nomic call ──
        print("\n--- T3: OllamaClient.embed('hello world') ---")
        vecs = await llm.embed(["hello world"])
        dims_ok = len(vecs) == 1 and len(vecs[0]) == DIM
        nonzero = dims_ok and any(abs(x) > 1e-9 for x in vecs[0])
        norm = math.sqrt(sum(x * x for x in vecs[0])) if dims_ok else 0.0
        print(f"  vectors={len(vecs)} dims={len(vecs[0]) if dims_ok else '?'} norm={norm:.4f}")
        rec("T3 embed() returns 768-dim vector", bool(dims_ok and nonzero))

        # ── T4: e2e judge RAG (memory-only fact) ──
        print("\n--- T4: e2e judge RAG hook — fact lives ONLY in store ---")
        marker = (
            "My favorite color is teal and the secret code word is xylophone-quokka."
        )
        store.append(role="user", content=marker, source="text")
        ctx4 = ContextManager(cfg, max_turns=10)
        res = await handle_text_input(
            "what is my favorite color", ctx4, llm, store=store,
            cfg=cfg, registry=registry,
        )
        reply_has = "teal" in res.reply.lower()
        print(f"  rag_hits={res.rag_hits} intent={res.intent} "
              f"reply={res.reply.strip()!r}")
        rec("T4 rag_hits>=1 AND reply surfaces memory-only fact",
            bool(res.rag_hits >= 1 and reply_has and res.intent == "chat"))
        print(f"  (ingested records: {ingest_count()})")

        # ── T5: delta ingestion idempotency ──
        print("\n--- T5: delta ingestion idempotency ---")
        before = ingest_count()
        await retrieve_memory(store, llm, "probe one")
        mid = ingest_count()
        await retrieve_memory(store, llm, "probe two")
        after = ingest_count()
        print(f"  cursor: before={before} after_first={mid} after_second={after}")
        rec("T5 cursor advanced once, stable on re-run",
            bool(mid > before and after == mid))

        # ── R1: normal chat regression ──
        print("\n--- R1: regression — normal chat 'hello there' ---")
        ctx_r1 = ContextManager(cfg, max_turns=10)
        res1 = await handle_text_input(
            "hello there", ctx_r1, llm, store=store,
            cfg=cfg, registry=registry,
        )
        print(f"  intent={res1.intent} tools={list(res1.tools)} "
              f"rag_hits={res1.rag_hits} reply={res1.reply.strip()!r}")
        rec("R1 chat intent/tools unchanged (rag_hits noted)",
            bool(res1.intent == "chat" and res1.tools == () and res1.reply.strip()))

        # ── R2: tool routing regression ──
        print("\n--- R2: regression — orchestrate('what time is it') ---")
        out = await orchestrate(cfg, registry, llm, "what time is it")
        tools = list(out.used_tools)
        print(f"  tools={tools} block={out.results_block.strip()[:70]!r}")
        rec("R2 tool routing unchanged", tools == ["get_current_time"] and bool(out.results_block.strip()))

    finally:
        rag_configure(None)  # closes temp RAG DB
        shutil.rmtree(tmp, ignore_errors=True)
        await llm.close()

    # ── Summary ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"PHASE 4 GATE: {passed}/{len(results)} tests passed")
    for name, ok, _ in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
