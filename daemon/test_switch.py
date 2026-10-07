"""Test switch_window from same process context."""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.window_manager import switch_window, get_foreground_window


async def test():
    # Switch to Notepad
    r = await switch_window({"title": "Notepad"})
    fg = await get_foreground_window({})
    ok1 = "Notepad" in fg.get("title", "")
    print(f"Notepad: success={r.get('success')}, fg={fg.get('title')}, match={ok1}")

    # Switch to Chrome
    r = await switch_window({"title": "Chrome"})
    fg = await get_foreground_window({})
    ok2 = "Chrome" in fg.get("title", "") or "chrome" in fg.get("process", "").lower()
    print(f"Chrome: success={r.get('success')}, fg={fg.get('title')}, match={ok2}")

    # Switch to Cursor
    r = await switch_window({"title": "Cursor"})
    fg = await get_foreground_window({})
    ok3 = "Cursor" in fg.get("title", "") or "cursor" in fg.get("process", "").lower()
    print(f"Cursor: success={r.get('success')}, fg={fg.get('title')}, match={ok3}")

    print(f"\nResults: {sum([ok1, ok2, ok3])}/3")


asyncio.run(test())
