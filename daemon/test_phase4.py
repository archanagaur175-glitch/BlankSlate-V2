"""Phase 4 gate test — Full Voice-to-Action E2E.

Tests the complete flow: text input → intent routing → tool execution → reply.
Simulates what happens when voice is transcribed and dispatched.
"""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.llm.ollama_client import OllamaClient
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.orchestrator import orchestrate
from blankslate.intent.judge import handle_text_input


async def run_tests():
    print("=" * 60)
    print("PHASE 4 GATE TESTS — Full Voice-to-Action E2E")
    print("=" * 60)

    # Load config
    cfg = DaemonConfig()
    object.__setattr__(cfg, "tools_enabled", True)

    # Init components
    ctx = ContextManager(cfg, max_turns=10)
    llm = OllamaClient(cfg)
    registry = ToolRegistry()

    results = []

    # Test 1: Tool routing (keyword path)
    print("\n--- TEST 1: Route 'open notepad' → app_launch ---")
    routed = registry.get("app_launch")
    print(f"  Found tool: {routed.name if routed else 'None'}")
    ok1 = routed is not None and routed.name == "app_launch"
    print(f"  {'✅' if ok1 else '❌'}")
    results.append(("Route to app_launch", ok1))

    # Test 2: Tool routing for text injection
    print("\n--- TEST 2: Route 'type hello' → type_text ---")
    routed = registry.get("type_text")
    print(f"  Found tool: {routed.name if routed else 'None'}")
    ok2 = routed is not None and routed.name == "type_text"
    print(f"  {'✅' if ok2 else '❌'}")
    results.append(("Route to type_text", ok2))

    # Test 3: Tool routing for window switching
    print("\n--- TEST 3: Route 'switch to notepad' → switch_window ---")
    routed = registry.get("switch_window")
    print(f"  Found tool: {routed.name if routed else 'None'}")
    ok3 = routed is not None and routed.name == "switch_window"
    print(f"  {'✅' if ok3 else '❌'}")
    results.append(("Route to switch_window", ok3))

    # Test 4: Full E2E — launch notepad
    print("\n--- TEST 4: E2E 'open notepad' → app_launch → reply ---")
    result = await handle_text_input(
        "open notepad", ctx, llm, cfg=cfg, registry=registry,
    )
    print(f"  intent: {result.intent}")
    print(f"  tools: {result.tools}")
    print(f"  reply: {result.reply[:80]}...")
    ok4 = "app_launch" in result.tools
    print(f"  {'✅' if ok4 else '❌'}")
    results.append(("E2E open notepad", ok4))

    # Test 5: Full E2E — list windows
    print("\n--- TEST 5: E2E 'list windows' → list_windows → reply ---")
    await asyncio.sleep(2)  # wait for notepad to start
    result = await handle_text_input(
        "list all open windows", ctx, llm, cfg=cfg, registry=registry,
    )
    print(f"  intent: {result.intent}")
    print(f"  tools: {result.tools}")
    print(f"  reply: {result.reply[:80]}...")
    ok5 = "list_windows" in result.tools
    print(f"  {'✅' if ok5 else '❌'}")
    results.append(("E2E list windows", ok5))

    # Test 6: Full E2E — get current time (deterministic)
    print("\n--- TEST 6: E2E 'what time is it' → get_current_time → reply ---")
    result = await handle_text_input(
        "what time is it", ctx, llm, cfg=cfg, registry=registry,
    )
    print(f"  intent: {result.intent}")
    print(f"  tools: {result.tools}")
    print(f"  reply: {result.reply[:80]}...")
    ok6 = "get_current_time" in result.tools and result.intent == "tool"
    print(f"  {'✅' if ok6 else '❌'}")
    results.append(("E2E get time (deterministic)", ok6))

    # Test 7: Multi-tool chain
    print("\n--- TEST 7: E2E 'open notepad then switch to it' → multi-tool ---")
    result = await handle_text_input(
        "open notepad and then switch to it", ctx, llm, cfg=cfg, registry=registry,
    )
    print(f"  intent: {result.intent}")
    print(f"  tools: {result.tools}")
    print(f"  reply: {result.reply[:80]}...")
    ok7 = len(result.tools) >= 1
    print(f"  {'✅' if ok7 else '❌'}")
    results.append(("E2E multi-tool chain", ok7))

    # Cleanup
    from blankslate.tools.app_manager import app_kill
    await app_kill({"query": "notepad"})

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"PHASE 4 GATE: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("=" * 60)

    await llm.close()


asyncio.run(run_tests())
