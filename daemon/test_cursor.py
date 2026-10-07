"""Test: enumerate then immediately switch to Cursor, checking handle consistency."""
import asyncio
import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.window_manager import _enum_windows_blocking, _switch_to_window_blocking, _get_foreground_window_blocking
import win32gui


async def run():
    loop = asyncio.get_running_loop()

    # Enumerate
    windows = await loop.run_in_executor(None, _enum_windows_blocking)
    cursor_wins = [w for w in windows if "cursor" in w["title"].lower() and "agent" not in w["title"].lower()]
    print(f"Cursor windows found: {len(cursor_wins)}")
    for w in cursor_wins:
        print(f"  hwnd={w['hwnd']}, title='{w['title']}', class={w.get('class')}")

    if not cursor_wins:
        print("No Cursor windows to test")
        return

    target = cursor_wins[0]
    hwnd = target["hwnd"]

    # Check if window still exists right before switch
    exists_before = bool(win32gui.IsWindow(hwnd))
    title_before = win32gui.GetWindowText(hwnd)
    print(f"\nBefore switch: hwnd={hwnd}, exists={exists_before}, title='{title_before}'")

    # Switch
    info = await loop.run_in_executor(None, _switch_to_window_blocking, hwnd)
    print(f"Switch result: success={info.get('success')}, strategy={info.get('strategy')}")
    print(f"  class={info.get('class')}, foreground_title={info.get('foreground_title')}")

    # Check foreground after
    await asyncio.sleep(0.3)
    fg = await loop.run_in_executor(None, _get_foreground_window_blocking)
    print(f"After: foreground hwnd={fg['hwnd']}, title='{fg['title']}', process='{fg['process']}'")
    print(f"Match: {fg['hwnd'] == hwnd}")


asyncio.run(run())
