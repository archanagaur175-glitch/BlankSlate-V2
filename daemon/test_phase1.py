"""Phase 1 gate tests — app launcher and killer."""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.tools.app_manager import app_launch, app_kill


async def run_tests():
    print("=" * 60)
    print("PHASE 1 GATE TESTS — App Launcher / Killer")
    print("=" * 60)

    # Test (a): Launch two different allowlisted apps
    print("\n--- TEST (a): Launch two allowlisted apps ---")
    r1 = await app_launch({"app_id": "notepad"})
    print(f"  notepad: {r1}")
    r2 = await app_launch({"app_id": "chrome"})
    print(f"  chrome:  {r2}")

    # Wait for apps to start
    await asyncio.sleep(2)

    # Test (b): Rejected launch for non-allowlisted app
    print("\n--- TEST (b): Rejected non-allowlisted app ---")
    r3 = await app_launch({"app_id": "spotify"})
    print(f"  spotify: {r3}")

    # Test (c): Kill a launched app
    print("\n--- TEST (c): Kill launched app ---")
    r4 = await app_kill({"target": "notepad.exe"})
    print(f"  kill notepad: {r4}")

    # Test (d): Attempted kill of blocklisted process
    print("\n--- TEST (d): Blocked kill of system process ---")
    r5 = await app_kill({"target": "explorer.exe"})
    print(f"  kill explorer: {r5}")

    r6 = await app_kill({"target": "csrss.exe"})
    print(f"  kill csrss:    {r6}")

    print("\n" + "=" * 60)
    print("PHASE 1 GATE TESTS COMPLETE")
    print("=" * 60)


asyncio.run(run_tests())
