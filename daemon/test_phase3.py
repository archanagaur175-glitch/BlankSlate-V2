"""Phase 3 gate test — Safe Text Injection."""
import asyncio
import sys
import io
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.text_injector import type_text, press_key, hotkey
from blankslate.tools.window_manager import switch_window, get_foreground_window
from blankslate.tools.app_manager import app_launch, app_kill


async def run_tests():
    print("=" * 60)
    print("PHASE 3 GATE TESTS — Safe Text Injection")
    print("=" * 60)

    # Setup: launch Notepad
    print("\n--- Setting up: launching Notepad ---")
    await app_launch({"app_id": "notepad"})
    await asyncio.sleep(2)

    # Switch to Notepad
    await switch_window({"title": "Notepad"})
    await asyncio.sleep(0.5)
    fg = await get_foreground_window({})
    print(f"  Foreground: {fg['title']} ({fg['process']})")

    results = []

    # Test 1: Type simple text
    print("\n--- TEST 1: Type simple text ---")
    r = await type_text({"text": "Hello BlankSlate!"})
    print(f"  result: {r.get('result')}")
    print(f"  typed: {r.get('typed')}")
    await asyncio.sleep(0.5)
    ok1 = r.get("typed", 0) > 0
    print(f"  {'✅' if ok1 else '❌'}")
    results.append(("Type simple text", ok1))

    # Test 2: Press Enter
    print("\n--- TEST 2: Press Enter ---")
    r = await press_key({"key": "enter"})
    print(f"  result: {r.get('result')}")
    await asyncio.sleep(0.3)
    ok2 = r.get("key") == "enter"
    print(f"  {'✅' if ok2 else '❌'}")
    results.append(("Press Enter", ok2))

    # Test 3: Type on new line
    print("\n--- TEST 3: Type on new line ---")
    r = await type_text({"text": "Phase 3 test line"})
    print(f"  result: {r.get('result')}")
    await asyncio.sleep(0.5)
    ok3 = r.get("typed", 0) > 0
    print(f"  {'✅' if ok3 else '❌'}")
    results.append(("Type on new line", ok3))

    # Test 4: Hotkey Ctrl+A (select all)
    print("\n--- TEST 4: Hotkey Ctrl+A ---")
    r = await hotkey({"combo": "Ctrl+A"})
    print(f"  result: {r.get('result')}")
    await asyncio.sleep(0.3)
    ok4 = r.get("combo") == "Ctrl+A"
    print(f"  {'✅' if ok4 else '❌'}")
    results.append(("Hotkey Ctrl+A", ok4))

    # Test 5: Unicode text
    print("\n--- TEST 5: Unicode text ---")
    await press_key({"key": "enter"})
    await asyncio.sleep(0.2)
    r = await type_text({"text": "UTF-8: 你好世界 🌍"})
    print(f"  result: {r.get('result')}")
    await asyncio.sleep(0.5)
    ok5 = r.get("typed", 0) > 0
    print(f"  {'✅' if ok5 else '❌'}")
    results.append(("Unicode text", ok5))

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"PHASE 3 GATE: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("=" * 60)


asyncio.run(run_tests())
