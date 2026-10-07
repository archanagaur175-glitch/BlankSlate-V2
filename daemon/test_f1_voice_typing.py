"""F1 gate test — Voice-path text_injector arg extraction (BEFORE/AFTER fix).

Exercises the REAL voice path exactly as main.py dispatches it after STT:
    handle_text_input / orchestrate → spec.handler({"query": query})

Run BEFORE the fix (expect T1-T5 to FAIL) and AFTER (expect all PASS).
Ground truth for typing is the clipboard: type_text pastes via clipboard,
so select-all+copy proves the keystrokes actually landed in Notepad.
"""
import asyncio
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pyperclip
import win32gui

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.llm.ollama_client import OllamaClient
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.orchestrator import orchestrate
from blankslate.intent.judge import handle_text_input


def _fg_title() -> str:
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())


async def _structured(registry: ToolRegistry, name: str, args: dict) -> dict:
    spec = registry.get(name)
    return await spec.handler(args)


async def run_tests():
    print("=" * 60)
    print("F1 GATE TESTS — Voice-path text_injector arg extraction")
    print("=" * 60)

    cfg = DaemonConfig()
    object.__setattr__(cfg, "tools_enabled", True)
    ctx = ContextManager(cfg, max_turns=10)
    llm = OllamaClient(cfg)
    registry = ToolRegistry()
    results = []

    # ── SETUP: our OWN scratch file in Notepad — never touch user windows ──
    # Guard: destructive keystrokes only run if the scratch tab is verified
    # foreground; otherwise all keystroke tests are skipped as FAIL.
    print("\n--- SETUP: open scratch file in Notepad, focus, clear document ---")
    import os
    import tempfile
    scratch = os.path.join(tempfile.gettempdir(), "blankslate_f1_gate.txt")
    with open(scratch, "w", encoding="utf-8") as fh:
        fh.write("stale scratch content that must be cleared\n")
    os.startfile(scratch)
    await asyncio.sleep(2.5)
    if "blankslate_f1_gate" not in _fg_title().lower():
        await _structured(registry, "switch_window", {"query": "blankslate_f1_gate"})
        await asyncio.sleep(0.8)
    fg = _fg_title()
    ok_setup = "blankslate_f1_gate" in fg.lower()
    print(f"  foreground: {fg!r}")
    if ok_setup:
        await _structured(registry, "hotkey", {"combo": "ctrl+a"})
        await asyncio.sleep(0.2)
        await _structured(registry, "press_key", {"key": "backspace", "count": 1})
        await asyncio.sleep(0.3)
    else:
        print("  ABORT: scratch window not focused — skipping keystroke tests")
    results.append(("Setup: scratch Notepad focused", ok_setup))
    print(f"  {'OK' if ok_setup else 'FAIL'}")

    if not ok_setup:
        for skipped in [
            "T1 voice type_text (orchestrator)",
            "T2 voice type_text (judge)",
            "T3 voice press_key",
            "T4 voice hotkey",
            "T5 clipboard ground truth",
            "T6 structured-args regression",
        ]:
            results.append((skipped, False))
    else:
        # ── T1: orchestrator path extracts text from raw query ──
        print("\n--- T1: orchestrate('type hello blankslate') -> type_text ---")
        out = await orchestrate(cfg, registry, llm, "type hello blankslate")
        block = out.results_block or ""
        print(f"  tools: {out.used_tools}")
        print(f"  block: {block[:200]!r}")
        ok1 = "type_text" in out.used_tools and "error" not in block.lower()
        results.append(("T1 voice type_text (orchestrator)", ok1))
        print(f"  {'OK' if ok1 else 'FAIL'}")

        # ── T2: full judge voice path (handle_text_input) ──
        print("\n--- T2: handle_text_input('type hello blankslate') ---")
        res = await handle_text_input(
            "type hello blankslate", ctx, llm, cfg=cfg, registry=registry,
        )
        print(f"  intent: {res.intent}")
        print(f"  tools: {res.tools}")
        print(f"  reply: {res.reply[:120]!r}")
        ok2 = "type_text" in res.tools and res.reply.startswith("Typed")
        results.append(("T2 voice type_text (judge)", ok2))
        print(f"  {'OK' if ok2 else 'FAIL'}")

        # ── T3: press_key from raw query ──
        print("\n--- T3: orchestrate('press enter') -> press_key ---")
        out = await orchestrate(cfg, registry, llm, "press enter")
        block = out.results_block or ""
        print(f"  tools: {out.used_tools}")
        print(f"  block: {block[:200]!r}")
        ok3 = (
            "press_key" in out.used_tools
            and "no key provided" not in block.lower()
            and "pressed" in block.lower()
        )
        results.append(("T3 voice press_key", ok3))
        print(f"  {'OK' if ok3 else 'FAIL'}")

        # ── T4: hotkey from raw query (also routes press_key: 'press' keyword) ──
        print("\n--- T4: orchestrate('press ctrl a') -> hotkey ---")
        out = await orchestrate(cfg, registry, llm, "press ctrl a")
        block = out.results_block or ""
        print(f"  tools: {out.used_tools}")
        print(f"  block: {block[:200]!r}")
        ok4 = "hotkey" in out.used_tools and "no key combination" not in block.lower()
        results.append(("T4 voice hotkey", ok4))
        print(f"  {'OK' if ok4 else 'FAIL'}")

        # ── T5: clipboard ground truth — did the text really land in Notepad? ──
        print("\n--- T5: select-all + copy -> clipboard contains typed text ---")
        await _structured(registry, "hotkey", {"combo": "ctrl+c"})
        await asyncio.sleep(0.5)
        clip = pyperclip.paste()
        print(f"  clipboard: {clip!r}")
        ok5 = "hello blankslate" in clip and ("\n" in clip or "\r" in clip)
        results.append(("T5 clipboard ground truth", ok5))
        print(f"  {'OK' if ok5 else 'FAIL'}")

        # ── T6: regression — structured-args path unchanged ──
        print("\n--- T6: regression — type_text({'text': ...}) structured args ---")
        r = await _structured(registry, "type_text", {"text": "structured regression"})
        print(f"  reply: {r.get('reply')!r} error: {r.get('error')!r}")
        ok6 = r.get("error") is None and str(r.get("reply", "")).startswith("Typed")
        results.append(("T6 structured-args regression", ok6))
        print(f"  {'OK' if ok6 else 'FAIL'}")

    # ── T7: regression — normal chat routing unchanged ──
    print("\n--- T7: regression — normal chat 'hello there' ---")
    res = await handle_text_input(
        "hello there", ctx, llm, cfg=cfg, registry=registry,
    )
    print(f"  intent: {res.intent} tools: {res.tools}")
    print(f"  reply: {res.reply[:100]!r}")
    ok7 = res.intent == "chat" and not res.tools
    results.append(("T7 normal chat regression", ok7))
    print(f"  {'OK' if ok7 else 'FAIL'}")

    # ── T8: regression — existing query-fallback tool (calculator) unchanged ──
    print("\n--- T8: regression — orchestrate('calculate 2 + 2') ---")
    out = await orchestrate(cfg, registry, llm, "calculate 2 + 2")
    block = out.results_block or ""
    print(f"  tools: {out.used_tools}")
    print(f"  block: {block[:200]!r}")
    ok8 = "calculator" in out.used_tools and "error" not in block.lower() and "4" in block
    results.append(("T8 calculator fallback regression", ok8))
    print(f"  {'OK' if ok8 else 'FAIL'}")

    # ── Cleanup ──
    await _structured(registry, "app_kill", {"query": "notepad"})

    # ── Summary ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"F1 GATE: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)

    await llm.close()


asyncio.run(run_tests())
