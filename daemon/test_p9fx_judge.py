import asyncio, sys, io
sys.path.insert(0, r"C:\Users\archa\Documents\BlankSlate_V2\daemon")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from blankslate.config import load_config
from blankslate.context.manager import ContextManager
from blankslate.tools.registry import ToolRegistry
from blankslate.intent.judge import handle_text_input


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.last_messages = None

    async def chat(self, messages):
        self.calls += 1
        self.last_messages = messages
        from blankslate.llm.ollama_client import ChatResponse
        user = next((m.content for m in messages if m.role == "user"), "")
        return ChatResponse(content=f"[fake-llm reply to: {user[:40]}]", model="fake", total_duration_ms=1)

    async def close(self):
        pass


async def main():
    cfg = load_config()
    reg = ToolRegistry()
    llm = FakeLLM()
    ctx = ContextManager(cfg, max_turns=20)

    cases = [
        ("calculate 6 times 7", "tool", False),      # deterministic -> NO llm
        ("what is the current time", "tool", False), # deterministic -> NO llm
        ("search the web for news", "tool", False),  # stub deterministic -> NO llm
        ("hello there", "chat", True),               # chat -> llm yes
        ("what is the weather like", "tool", False), # stub deterministic -> NO llm
    ]
    for q, exp_intent, exp_llm in cases:
        llm.calls = 0
        res = await handle_text_input(
            q, ctx, llm, persist_enabled=False, cfg=cfg, registry=reg,
            mcp_clients={}, planner=None,
        )
        llm_called = llm.calls > 0
        ok_intent = res.intent == exp_intent
        ok_llm = llm_called == exp_llm
        status = "PASS" if (ok_intent and ok_llm) else "FAIL"
        print(f"[{status}] {q!r:30} intent={res.intent} llm_called={llm_called} tools={list(res.tools)} reply={res.reply[:70]!r}")


asyncio.run(main())
