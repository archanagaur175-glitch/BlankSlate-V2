"""Phase 1 gate test — Focus Memory (snapshot/restore foreground window).

Covers: new focus_snapshot/focus_restore behavior, the switch_window
auto-snapshot hook (F4), error-boundary behavior for dead/changed windows,
stack cap, and regressions proving normal chat + normal tool routing are
unchanged after the hook landed.

Window safety: never types or deletes; aborts window tests if the scratch
Notepad cannot be verified as foreground (protects user windows).
"""
import asyncio
import sys
import io
import os
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import win32gui

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.focus.memory import STACK, FocusFrame, focus_restore, focus_snapshot
from blankslate.llm.ollama_client import OllamaClient
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.orchestrator import orchestrate
from blankslate.intent.judge import handle_text_input

SCRATCH_PREFIX = "blankslate_focus_a"
PROBE_TITLE = "BlankSlateFocusProbe"
WM_CLOSE = 0x0010


def fg_title() -> str:
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())


def notepad_titles() -> list[str]:
    titles: list[str] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if "notepad" in t.lower():
                titles.append(t)
        return True

    win32gui.EnumWindows(cb, None)
    return titles


async def _h(registry: ToolRegistry, name: str, args: dict) -> dict:
    return await registry.get(name).handler(args)


async def run_tests():
    print("=" * 60)
    print("PHASE 1 GATE TESTS — Focus Memory")
    print("=" * 60)

    cfg = DaemonConfig()
    object.__setattr__(cfg, "tools_enabled", True)
    ctx = ContextManager(cfg, max_turns=10)
    llm = OllamaClient(cfg)
    registry = ToolRegistry()
    results: list[tuple[str, bool]] = []
    setup_ok = False
    tk_root_hwnd: int | None = None

    # ── SETUP A: scratch Notepad (only touch windows that are ours) ──
    print("\n--- SETUP: scratch Notepad window ---")
    existing = notepad_titles()
    user_notepad_open = bool(existing and not all(SCRATCH_PREFIX in t for t in existing))
    if existing and not user_notepad_open:
        await _h(registry, "app_kill", {"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")
    scratch = os.path.join(tempfile.gettempdir(), f"{SCRATCH_PREFIX}.txt")
    with open(scratch, "w", encoding="utf-8") as fh:
        fh.write("focus gate scratch\n")
    os.startfile(scratch)
    await asyncio.sleep(2.5)
    if SCRATCH_PREFIX not in fg_title().lower():
        await _h(registry, "switch_window", {"query": SCRATCH_PREFIX})
        await asyncio.sleep(0.6)
    fg = fg_title()
    scratch_focused = SCRATCH_PREFIX in fg.lower()
    print(f"  foreground: {fg!r} -> {'OK' if scratch_focused else 'FAIL'}")

    # ── SETUP B: Tk probe window (window B for switch/restore) ──
    if scratch_focused:
        import tkinter as tk
        tk_ready = threading.Event()
        tk_err: list[str] = []

        def run_tk():
            try:
                root = tk.Tk()
                root.title(PROBE_TITLE)
                root.geometry("320x120+500+350")
                root.after(15 * 60 * 1000, root.destroy)  # failsafe self-close
                tk_ready.set()
                root.mainloop()
            except Exception as exc:
                tk_err.append(str(exc))
                tk_ready.set()

        threading.Thread(target=run_tk, daemon=True).start()
        tk_ready.wait(10)
        probe_visible = False
        deadline = time.time() + 5
        while time.time() < deadline:
            probe_visible = any(PROBE_TITLE in t for t in _enum_titles())
            if probe_visible:
                break
            await asyncio.sleep(0.2)
        print(f"  probe window: {'OK' if probe_visible and not tk_err else 'FAIL ' + str(tk_err)}")
        if probe_visible and not tk_err:
            # New window stole focus — return to scratch before snapshotting
            await _h(registry, "switch_window", {"title": SCRATCH_PREFIX})
            await asyncio.sleep(0.5)
            fg_back = fg_title()
            scratch_focused = SCRATCH_PREFIX in fg_back.lower()
            print(f"  back to scratch: {fg_back!r} -> {'OK' if scratch_focused else 'FAIL'}")
        setup_ok = scratch_focused and probe_visible and not tk_err
    results.append(("Setup: scratch + probe windows", setup_ok))
    print(f"  {'OK' if setup_ok else 'FAIL'}")

    STACK.clear()

    if not setup_ok:
        for skipped in [
            "T1 focus_snapshot",
            "T2 routing + switch + auto-snapshot hook",
            "T3 focus_restore returns to A",
            "T4 invalid frame (window destroyed)",
        ]:
            results.append((skipped, False))
    else:
        # ── T1: focus_snapshot ──
        print("\n--- T1: focus_snapshot on scratch Notepad ---")
        r = await _h(registry, "focus_snapshot", {})
        print(f"  reply: {r.get('reply')} err: {r.get('error')} depth: {r.get('depth')}")
        ok1 = (
            r.get("error") is None
            and r.get("depth") == 1
            and SCRATCH_PREFIX in str(r.get("title", "")).lower()
        )
        results.append(("T1 focus_snapshot", ok1))
        print(f"  {'OK' if ok1 else 'FAIL'}")

        # ── T2: routing regression + structured switch + auto-snapshot hook ──
        # NOTE: full orchestrator path for switch_window fails on raw NL queries
        # ("switch to X") — pre-existing extraction gap (flagged as F7), same
        # class as F1. Routing is asserted via the router; execution via the
        # structured title arg (how Phase 2 verified switch_window).
        print(f"\n--- T2: route 'switch to {PROBE_TITLE}' + structured switch + hook ---")
        from blankslate.tools.router import ToolRouter
        routed = await ToolRouter(cfg, registry).route(f"switch to {PROBE_TITLE}")
        routed_names = [t.name for t in routed.selected_tools]
        sw = await _h(registry, "switch_window", {"title": PROBE_TITLE})
        await asyncio.sleep(0.3)
        fg2 = fg_title()
        depth2 = STACK.depth()
        print(f"  routed: {routed_names}")
        print(f"  switch err: {sw.get('error')}")
        print(f"  foreground: {fg2!r} depth: {depth2}")
        ok2 = (
            "switch_window" in routed_names
            and sw.get("error") is None
            and PROBE_TITLE.lower() in fg2.lower()
            and depth2 == 2
        )
        results.append(("T2 routing + switch + auto-snapshot hook", ok2))
        print(f"  {'OK' if ok2 else 'FAIL'}")

        # ── T3: focus_restore returns to A ──
        print("\n--- T3: focus_restore -> back to scratch Notepad ---")
        r = await _h(registry, "focus_restore", {})
        await asyncio.sleep(0.3)
        fg3 = fg_title()
        print(f"  reply: {r.get('reply')} err: {r.get('error')} depth: {r.get('depth')}")
        print(f"  foreground: {fg3!r} stack: {STACK.depth()}")
        ok3 = (
            r.get("error") is None
            and SCRATCH_PREFIX in fg3.lower()
            and STACK.depth() == 1
        )
        results.append(("T3 focus_restore returns to A", ok3))
        print(f"  {'OK' if ok3 else 'FAIL'}")

        # ── T4: invalid frame — destroy probe, restore must fail cleanly ──
        print("\n--- T4: snapshot probe, destroy it, restore -> structured error ---")
        sw = await _h(registry, "switch_window", {"query": PROBE_TITLE})
        await asyncio.sleep(0.3)
        snap = await _h(registry, "focus_snapshot", {})
        depth_before = STACK.depth()
        print(f"  switch err: {sw.get('error')} snap depth: {depth_before}")
        ok4_stage1 = sw.get("error") is None and snap.get("error") is None and depth_before == 3
        probe_hwnd = snap.get("hwnd")
        destroyed = False
        if ok4_stage1 and probe_hwnd:
            win32gui.PostMessage(int(probe_hwnd), WM_CLOSE, 0, 0)
            deadline = time.time() + 4
            while time.time() < deadline:
                if not win32gui.IsWindow(int(probe_hwnd)):
                    destroyed = True
                    break
                await asyncio.sleep(0.2)
        print(f"  probe destroyed: {destroyed}")
        r = await _h(registry, "focus_restore", {})
        err = str(r.get("error", ""))
        print(f"  restore error: {err!r} stack: {STACK.depth()}")
        ok4 = (
            ok4_stage1
            and destroyed
            and "no longer exists" in err
            and STACK.depth() == 2
        )
        results.append(("T4 invalid frame (window destroyed)", ok4))
        print(f"  {'OK' if ok4 else 'FAIL'}")

    # ── T5: empty stack -> structured error (window-independent) ──
    print("\n--- T5: focus_restore on empty stack -> structured error ---")
    STACK.clear()
    r = await _h(registry, "focus_restore", {})
    print(f"  error: {r.get('error')!r}")
    ok5 = "no saved focus" in str(r.get("error", ""))
    results.append(("T5 empty stack error", ok5))
    print(f"  {'OK' if ok5 else 'FAIL'}")

    # ── T6: stack cap at MAX_FRAMES=5 ──
    print("\n--- T6: stack cap (push 7 -> depth 5) ---")
    STACK.clear()
    for i in range(7):
        STACK.push(FocusFrame(hwnd=10000 + i, pid=0, process_name="synthetic", title=f"fake-{i}"))
    depth6 = STACK.depth()
    print(f"  depth after 7 pushes: {depth6}")
    ok6 = depth6 == 5
    results.append(("T6 stack cap = 5", ok6))
    print(f"  {'OK' if ok6 else 'FAIL'}")
    STACK.clear()

    # ── R1: regression — normal chat unchanged ──
    print("\n--- R1: regression — normal chat 'hello there' ---")
    res = await handle_text_input(
        "hello there", ctx, llm, cfg=cfg, registry=registry,
    )
    print(f"  intent: {res.intent} tools: {res.tools}")
    print(f"  reply: {res.reply[:100]!r}")
    ok_r1 = res.intent == "chat" and not res.tools
    results.append(("R1 normal chat regression", ok_r1))
    print(f"  {'OK' if ok_r1 else 'FAIL'}")

    # ── R2: regression — normal tool routing unchanged ──
    print("\n--- R2: regression — orchestrate('what time is it') ---")
    out = await orchestrate(cfg, registry, llm, "what time is it")
    block = out.results_block or ""
    print(f"  tools: {out.used_tools}")
    print(f"  block: {block[:160]!r}")
    ok_r2 = "get_current_time" in out.used_tools and "error" not in block.lower()
    results.append(("R2 normal tool routing regression", ok_r2))
    print(f"  {'OK' if ok_r2 else 'FAIL'}")

    # ── R3: regression — switch_window error path still structured ──
    print("\n--- R3: regression — switch to nonexistent window ---")
    out = await orchestrate(cfg, registry, llm, "switch to no_such_window_zzz")
    block = out.results_block or ""
    print(f"  tools: {out.used_tools}")
    print(f"  block: {block[:160]!r}")
    ok_r3 = (
        "switch_window" in out.used_tools
        and "error" in block.lower()
        and "no window found" in block.lower()
    )
    results.append(("R3 switch error path regression", ok_r3))
    print(f"  {'OK' if ok_r3 else 'FAIL'}")

    # ── Cleanup ──
    STACK.clear()
    for hwnd_str in _window_hwnds_by_title(PROBE_TITLE):
        try:
            win32gui.PostMessage(hwnd_str, WM_CLOSE, 0, 0)
        except Exception:
            pass
    nt = notepad_titles()
    if nt and not user_notepad_open and all(SCRATCH_PREFIX in t for t in nt):
        await _h(registry, "app_kill", {"query": "notepad"})
    elif nt and user_notepad_open:
        print(f"  note: user Notepad left open, not killed: {nt}")

    # ── Summary ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"PHASE 1 GATE: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)

    await llm.close()


def _enum_titles() -> list[str]:
    titles: list[str] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if t:
                titles.append(t)
        return True

    win32gui.EnumWindows(cb, None)
    return titles


def _window_hwnds_by_title(needle: str) -> list[int]:
    found: list[int] = []

    def cb(hwnd, _):
        if needle in win32gui.GetWindowText(hwnd):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    return found


asyncio.run(run_tests())
