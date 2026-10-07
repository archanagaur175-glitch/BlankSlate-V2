import asyncio, sys, io, os, time
sys.path.insert(0, r"C:\Users\archa\Documents\BlankSlate_V2\daemon")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
os.environ["BS_OLLAMA_MODEL"] = "qwen2.5:0.5b"

from blankslate.config import load_config
from blankslate.context.manager import ContextManager
from blankslate.llm.ollama_client import OllamaClient
from blankslate.intent.judge import handle_text_input
from blankslate.tools.registry import ToolRegistry


async def main():
    cfg = load_config()
    llm = OllamaClient(cfg)
    ctx = ContextManager(cfg, max_turns=cfg.context_max_turns)
    reg = ToolRegistry()

    for q in ["hello there, how are you", "calculate 6 times 7", "what is the current time"]:
        t0 = time.perf_counter()
        res = await handle_text_input(q, ctx, llm, persist_enabled=False, cfg=cfg, registry=reg, mcp_clients={}, planner=None)
        dt = time.perf_counter() - t0
        print(f"[{dt:6.2f}s intent={res.intent} tools={list(res.tools)}] {q!r}")
        print(f"   reply={res.reply[:120]!r}")

    await llm.close()


asyncio.run(main())
