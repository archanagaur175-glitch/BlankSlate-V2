"""Test: switch to Chrome after Cursor fix."""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.window_manager import switch_window, get_foreground_window


async def run():
    for query in ["notepad", "cursor", "chrome", "notepad"]:
        r = await switch_window({"title": query})
        print(f"Switch to '{query}': success={r.get('success')}, strategy={r.get('strategy')}, fg={r.get('foreground_title')}")
        await asyncio.sleep(0.3)
        fg = await get_foreground_window({})
        print(f"  VERIFY: title='{fg['title']}', process='{fg['process']}'")


asyncio.run(run())
