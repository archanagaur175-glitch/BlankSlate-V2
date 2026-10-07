"""Diagnostic test — launch apps, then check switching."""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.window_manager import list_windows, switch_window, get_foreground_window
from blankslate.tools.app_manager import app_launch


async def run():
    # Launch apps
    for app in ["notepad", "chrome", "cursor"]:
        await app_launch({"app_id": app})
    await asyncio.sleep(3)

    # List windows
    wl = await list_windows({})
    chrome_matches = [w for w in wl["windows"] if "chrome" in w["title"].lower() and "restore" not in w["title"].lower()]
    cursor_matches = [w for w in wl["windows"] if "cursor" in w["title"].lower() and "agent" not in w["title"].lower()]
    notepad_matches = [w for w in wl["windows"] if "notepad" in w["title"].lower() and "online" not in w["title"].lower()]

    print("Chrome windows:")
    for w in chrome_matches[:3]:
        print(f"  [{w['hwnd']}] {w['title']} (class={w.get('class')})")
    print(f"  Count: {len(chrome_matches)}")

    print("\nCursor windows:")
    for w in cursor_matches[:3]:
        print(f"  [{w['hwnd']}] {w['title']} (class={w.get('class')})")
    print(f"  Count: {len(cursor_matches)}")

    print("\nNotepad windows:")
    for w in notepad_matches[:3]:
        print(f"  [{w['hwnd']}] {w['title']} (class={w.get('class')})")
    print(f"  Count: {len(notepad_matches)}")

    # Try switching
    for query in ["notepad", "chrome", "cursor"]:
        print(f"\n--- Switch to '{query}' ---")
        r = await switch_window({"title": query})
        print(f"  success={r.get('success')}, strategy={r.get('strategy')}")
        print(f"  class={r.get('class')}, fg={r.get('foreground_title')}")
        await asyncio.sleep(0.3)
        fg = await get_foreground_window({})
        print(f"  VERIFY: title='{fg['title']}', process='{fg['process']}'")


asyncio.run(run())
