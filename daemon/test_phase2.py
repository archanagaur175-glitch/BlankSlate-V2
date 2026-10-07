"""Phase 2 gate test — with GA_ROOT resolution and improved strategies."""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.window_manager import list_windows, switch_window, get_foreground_window
from blankslate.tools.app_manager import app_launch, app_kill


async def run_tests():
    print("=" * 60)
    print("PHASE 2 GATE TESTS — Window Focus Switcher (v2)")
    print("=" * 60)

    # Ensure 3 apps are running
    print("\n--- Setting up: launching 3 apps ---")
    for app in ["notepad", "chrome", "cursor"]:
        r = await app_launch({"app_id": app})
        print(f"  {app}: {r.get('result', r.get('error'))}")
    await asyncio.sleep(5)

    # List all windows (with class info)
    print("\n--- LIST WINDOWS (showing class) ---")
    wl = await list_windows({})
    for w in wl["windows"][:15]:
        print(f"  [{w['hwnd']}] class={w.get('class', '?'):30s} {w['title'][:50]} ({w['process']})")

    results = []

    # Test 1: Switch to Notepad
    print("\n--- TEST 1: Switch to Notepad ---")
    r = await switch_window({"title": "Notepad"})
    print(f"  result: {r.get('result')}")
    print(f"  success: {r.get('success')}")
    print(f"  class: {r.get('class')}")
    print(f"  foreground_title: {r.get('foreground_title')}")
    await asyncio.sleep(0.2)
    fg = await get_foreground_window({})
    print(f"  VERIFY: hwnd={fg['hwnd']}, title='{fg['title']}', process='{fg['process']}'")
    ok1 = "Notepad" in fg["title"]
    print(f"  {'✅' if ok1 else '❌'}")
    results.append(("Notepad", ok1))

    # Test 2: Switch to Chrome
    print("\n--- TEST 2: Switch to Chrome ---")
    r = await switch_window({"title": "Chrome"})
    print(f"  result: {r.get('result')}")
    print(f"  success: {r.get('success')}")
    print(f"  class: {r.get('class')}")
    print(f"  foreground_title: {r.get('foreground_title')}")
    await asyncio.sleep(0.2)
    fg = await get_foreground_window({})
    print(f"  VERIFY: hwnd={fg['hwnd']}, title='{fg['title']}', process='{fg['process']}'")
    ok2 = "Chrome" in fg["title"] or "chrome" in fg["process"].lower()
    print(f"  {'✅' if ok2 else '❌'}")
    results.append(("Chrome", ok2))

    # Test 3: Switch to Cursor
    print("\n--- TEST 3: Switch to Cursor ---")
    r = await switch_window({"title": "Cursor"})
    print(f"  result: {r.get('result')}")
    print(f"  success: {r.get('success')}")
    print(f"  class: {r.get('class')}")
    print(f"  foreground_title: {r.get('foreground_title')}")
    await asyncio.sleep(0.2)
    fg = await get_foreground_window({})
    print(f"  VERIFY: hwnd={fg['hwnd']}, title='{fg['title']}', process='{fg['process']}'")
    ok3 = "Cursor" in fg["title"] or "cursor" in fg["process"].lower()
    print(f"  {'✅' if ok3 else '❌'}")
    results.append(("Cursor", ok3))

    # Test 4: Minimize Notepad, restore + switch
    print("\n--- TEST 4: Restore minimized Notepad ---")
    import win32gui, win32con
    notepad_hwnd = None
    for w in wl["windows"]:
        if "Notepad" in w["title"] and "Untitled" in w["title"]:
            notepad_hwnd = w["hwnd"]
            break
    if notepad_hwnd:
        win32gui.ShowWindow(notepad_hwnd, win32con.SW_MINIMIZE)
        await asyncio.sleep(0.5)
        print(f"  Minimized hwnd={notepad_hwnd}")
        r = await switch_window({"hwnd": notepad_hwnd})
        print(f"  result: {r.get('result')}")
        print(f"  success: {r.get('success')}")
        await asyncio.sleep(0.2)
        fg = await get_foreground_window({})
        print(f"  VERIFY: hwnd={fg['hwnd']}, title='{fg['title']}'")
        ok4 = fg["hwnd"] == notepad_hwnd
        print(f"  {'✅' if ok4 else '❌'}")
        results.append(("Minimized restore", ok4))
    else:
        print("  ❌ no Notepad window found")
        results.append(("Minimized restore", False))

    # Cleanup
    print("\n--- Cleanup ---")
    for app in ["notepad", "chrome", "cursor"]:
        r = await app_kill({"target": f"{app}.exe"})
        print(f"  kill {app}: {r.get('result', r.get('error'))}")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    print(f"PHASE 2 GATE: {passed}/{len(results)} tests passed")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("=" * 60)


asyncio.run(run_tests())
