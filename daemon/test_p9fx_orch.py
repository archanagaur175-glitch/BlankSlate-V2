import asyncio, sys, io
sys.path.insert(0, r"C:\Users\archa\Documents\BlankSlate_V2\daemon")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from blankslate.config import load_config
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.orchestrator import orchestrate


class FakeLLM:
    async def close(self):
        pass


async def main():
    cfg = load_config()
    reg = ToolRegistry()

    cases = [
        ("calculate 6 times 7", ["calculator"], True),
        ("what is the current time", ["get_current_time"], True),
        ("remember that my name is Ned", ["note_taking"], True),
        ("search the web for news", ["web_search"], True),
        ("what is the weather today", ["weather"], True),
        ("hello there", [], False),
    ]
    for q, expected, expect_det in cases:
        out = await orchestrate(cfg, reg, FakeLLM(), q)
        got = out.used_tools
        det = out.deterministic
        status = "PASS" if sorted(got) == sorted(expected) and det == expect_det else "FAIL"
        print(f"[{status}] {q!r:32} -> tools={got} det={det} reply='{' '.join(out.reply_parts)}'")


asyncio.run(main())
