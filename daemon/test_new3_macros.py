"""Phase 3 gate test — Voice Macros (JSON multi-step sequences).

Covers: macro JSON load + F2/F3 macro-level validation (structure, required
args, recursion/path guards — validation code only, core tools untouched),
sequential execution with abort-on-first-failure via registry handler reuse,
F6-class back-to-back type_text steps (targeted runner retry), the judge
macro hook (F4), run_macro query-name extraction, and regressions proving
normal chat + normal tool routing are unchanged after the judge hook landed.

Ground truth is DISK-BASED (Ctrl+S then read the scratch file) — clipboard
reads are unreliable while the user is concurrently copying things (live
F6-class ownership contention observed during review sessions).

Safety: types only into our scratch Notepad (foreground-verified before
every keystroke test, foreground-steal detected around each macro run),
unique scratch filename per run (avoids Notepad session-restore buffers),
kills Notepad only when every Notepad window is ours, removes the gate's
fixture macros. No prereqs needed (stdlib json).
"""
import asyncio
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import win32gui

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.intent.judge import handle_text_input
from blankslate.llm.ollama_client import OllamaClient
from blankslate.macros.runner import run_macro
from blankslate.macros.store import extract_macro_name, find_macro, load_macro, macros_dir, validate_macro
from blankslate.tools.orchestrator import orchestrate
from blankslate.tools.registry import ToolRegistry

SCRATCH_PREFIX = "blankslate_macro_c"
FIXTURES = ("gate_ok", "gate_bad_arg", "gate_runtime", "gate_hook")


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


def write_fixtures() -> None:
    d = macros_dir()
    (d / "gate_ok.json").write_text(
        json.dumps(
            {
                "name": "gate_ok",
                "steps": [
                    {"tool": "type_text", "args": {"text": "MACRO_A "}},
                    {"tool": "type_text", "args": {"text": "MACRO_B"}},
                    {"tool": "hotkey", "args": {"combo": "ctrl+a"}},
                    {"tool": "hotkey", "args": {"combo": "ctrl+c"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (d / "gate_bad_arg.json").write_text(
        json.dumps(
            {
                "name": "gate_bad_arg",
                "steps": [
                    {"tool": "type_text", "args": {"text": "SHOULD_NOT_TYPE "}},
                    {"tool": "type_text", "args": {"text": ""}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (d / "gate_runtime.json").write_text(
        json.dumps(
            {
                "name": "gate_runtime",
                "steps": [
                    {"tool": "switch_window", "args": {"title": "No_Such_Window_Zzz"}},
                    {"tool": "type_text", "args": {"text": "RUNTIME_SHOULD_NOT_TYPE"}},
                ],
            }
        ),
        encoding="utf-8",
    )
    (d / "gate_hook.json").write_text(
        json.dumps(
            {
                "name": "gate_hook",
                "steps": [
                    {"tool": "type_text", "args": {"text": "HOOK_MARK "}},
                    {"tool": "hotkey", "args": {"combo": "ctrl+s"}},
                ],
            }
        ),
        encoding="utf-8",
    )


async def _h(registry: ToolRegistry, name: str, args: dict) -> dict:
    return await registry.get(name).handler(args)


async def run_tests():
    print("=" * 60)
    print("PHASE 3 GATE TESTS — Voice Macros")
    print("=" * 60)

    cfg = DaemonConfig()
    object.__setattr__(cfg, "tools_enabled", True)
    ctx = ContextManager(cfg, max_turns=10)
    llm = OllamaClient(cfg)
    registry = ToolRegistry()
    results: list[tuple[str, bool]] = []

    # ── SETUP: fixtures + fresh scratch Notepad (unique name per run) ──
    print("\n--- SETUP: fixture macros + scratch Notepad ---")
    write_fixtures()
    existing = notepad_titles()
    user_notepad_open = bool(existing and not all(SCRATCH_PREFIX in t for t in existing))
    if existing and not user_notepad_open:
        await _h(registry, "app_kill", {"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")
    scratch = os.path.join(
        tempfile.gettempdir(), f"{SCRATCH_PREFIX}_{os.getpid()}.txt"
    )
    with open(scratch, "w", encoding="utf-8") as fh:
        fh.write("macro gate scratch\n")
    os.startfile(scratch)
    await asyncio.sleep(2.5)

    async def ensure_fg() -> bool:
        """Refocus the scratch window right before keystroke tests."""
        if SCRATCH_PREFIX in fg_title().lower():
            return True
        await _h(registry, "switch_window", {"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.7)
        ok = SCRATCH_PREFIX in fg_title().lower()
        print(f"  fg re-guard: {fg_title()!r} -> {'OK' if ok else 'FAIL'}")
        return ok

    async def save_and_read() -> str:
        """Disk ground truth: Ctrl+S the scratch Notepad, read the file."""
        if not await ensure_fg():
            return ""
        await _h(registry, "hotkey", {"combo": "ctrl+s"})
        await asyncio.sleep(0.7)
        try:
            with open(scratch, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            print(f"  disk read failed: {exc}")
            return ""

    fg0 = fg_title()
    scratch_focused = SCRATCH_PREFIX in fg0.lower()
    if not scratch_focused:
        scratch_focused = await ensure_fg()
    print(f"  foreground: {fg0!r} -> {'OK' if scratch_focused else 'FAIL'}")
    results.append(("Setup: fixtures + scratch", scratch_focused))

    # ── T1: load a good macro ──
    print("\n--- T1: load_macro('gate_ok') ---")
    data, errors = load_macro("gate_ok")
    print(f"  errors: {errors}")
    print(f"  steps: {len(data['steps']) if data else None}")
    ok1 = not errors and data is not None and len(data.get("steps", [])) == 4
    results.append(("T1 load good macro", ok1))
    print(f"  {'OK' if ok1 else 'FAIL'}")

    # ── T2: F2/F3 validation suite (pure, no execution) ──
    print("\n--- T2: validation suite (F2/F3) ---")
    checks: list[tuple[str, bool]] = []

    def chk(label: str, errs: list[str], needle: str) -> None:
        ok = bool(errs) and any(needle in e for e in errs)
        checks.append((label, ok))
        print(f"  {'OK ' if ok else 'FAIL'} {label}: {errs}")

    chk("not-an-object", validate_macro("nope"), "JSON object")
    chk("empty steps", validate_macro({"steps": []}), "non-empty array")
    chk("unknown tool", validate_macro({"steps": [{"tool": "frobnicate"}]}), "unknown tool")
    chk(
        "missing required arg (F2)",
        validate_macro({"steps": [{"tool": "type_text", "args": {"text": ""}}]}),
        "needs one of",
    )
    chk(
        "args not an object",
        validate_macro({"steps": [{"tool": "type_text", "args": "hi"}]}),
        "'args' must be an object",
    )
    chk(
        "recursion guard",
        validate_macro({"steps": [{"tool": "run_macro", "args": {"name": "x"}}]}),
        "recursion",
    )
    chk(
        "unknown step keys",
        validate_macro({"steps": [{"tool": "type_text", "args": {"text": "x"}, "extra": 1}]}),
        "unknown keys",
    )
    _d, path_errs = load_macro("../evil")
    chk("path traversal name", path_errs, "invalid macro name")
    _d, bad_arg_errs = load_macro("gate_bad_arg")
    chk("file-based F2 (step index + tool)", bad_arg_errs, "step 1 (type_text)")
    ok2 = all(ok for _, ok in checks)
    results.append(("T2 validation suite (F2/F3)", ok2))
    print(f"  {'OK' if ok2 else 'FAIL'}")

    # ── T3: run macro via query-name extraction; disk ground truth ──
    # Retries on foreground steal (user activity), not on content: the file
    # must contain both markers written by back-to-back type_text steps.
    print("\n--- T3: run 'run macro gate_ok' via registry handler ---")
    ok3 = False
    extracted = extract_macro_name("run macro gate_ok")
    for attempt in (1, 2, 3):
        if not await ensure_fg():
            continue
        hwnd_before = win32gui.GetForegroundWindow()
        r = await _h(registry, "run_macro", {"query": "run macro gate_ok"})
        await asyncio.sleep(0.5)
        stolen = win32gui.GetForegroundWindow() != hwnd_before
        content = await save_and_read()
        print(
            f"  attempt {attempt}: steps_run={r.get('steps_run')} "
            f"fg_stolen={stolen} markers="
            f"{'MACRO_A' in content and 'MACRO_B' in content}"
        )
        ok3 = (
            not stolen
            and extracted == "gate_ok"
            and r.get("error") is None
            and r.get("deterministic") is True
            and r.get("steps_run") == 4
            and "MACRO_A" in content
            and "MACRO_B" in content
        )
        if ok3:
            break
        await asyncio.sleep(0.4)
    print(f"  extracted name: {extracted!r}")
    results.append(("T3 run macro (F6 back-to-back typing)", ok3))
    print(f"  {'OK' if ok3 else 'FAIL'}")

    # ── T4: pre-validation abort (F2) — nothing runs ──
    print("\n--- T4: gate_bad_arg -> validated BEFORE execution ---")
    r = await _h(registry, "run_macro", {"name": "gate_bad_arg"})
    content4 = await save_and_read()
    print(f"  error: {r.get('error')}")
    print(f"  steps_run: {r.get('steps_run')}")
    ok4 = (
        r.get("result") is None
        and r.get("steps_run") == 0
        and "step 1 (type_text)" in str(r.get("error", ""))
        and "SHOULD_NOT_TYPE" not in content4
    )
    results.append(("T4 pre-validation abort (F2)", ok4))
    print(f"  {'OK' if ok4 else 'FAIL'}")

    # ── T5: runtime abort — step 0 fails, step 1 never runs ──
    print("\n--- T5: gate_runtime -> abort at step 0, step 1 skipped ---")
    r = await _h(registry, "run_macro", {"name": "gate_runtime"})
    err5 = str(r.get("error", ""))
    print(f"  error: {err5}")
    print(f"  steps_run: {r.get('steps_run')} step_log: {r.get('step_log')}")
    content5 = await save_and_read()
    marker_absent = "RUNTIME_SHOULD_NOT_TYPE" not in content5
    print(f"  marker absent from notepad text: {marker_absent}")
    ok5 = (
        r.get("result") is None
        and r.get("steps_run") == 0
        and "step 0" in err5
        and "no window found" in err5
        and marker_absent
    )
    results.append(("T5 runtime abort at step 0", ok5))
    print(f"  {'OK' if ok5 else 'FAIL'}")

    # ── T6: judge macro hook ('run my gate hook') — no LLM ──
    print("\n--- T6: judge hook: handle_text_input('run my gate hook') ---")
    matched = find_macro("run my gate hook")
    ok6 = False
    res = None
    for attempt in (1, 2, 3):
        if not await ensure_fg():
            continue
        hwnd_before = win32gui.GetForegroundWindow()
        res = await handle_text_input(
            "run my gate hook", ctx, llm, cfg=cfg, registry=registry,
        )
        await asyncio.sleep(0.5)
        stolen = win32gui.GetForegroundWindow() != hwnd_before
        content = await save_and_read()
        reply_ok = bool(
            res and res.reply.startswith("Ran macro 'gate_hook'")
        )
        print(
            f"  attempt {attempt}: fg_stolen={stolen} "
            f"marker={'HOOK_MARK' in content} reply_ok={reply_ok}"
        )
        ok6 = (
            not stolen
            and matched == "gate_hook"
            and res.intent == "tool"
            and res.tools == ("run_macro",)
            and reply_ok
            and "HOOK_MARK" in content
        )
        if ok6:
            break
        await asyncio.sleep(0.4)
    print(f"  find_macro -> {matched!r}")
    if res is not None:
        print(f"  intent: {res.intent} tools: {res.tools}")
        print(f"  reply: {res.reply!r}")
    results.append(("T6 judge macro hook (F4)", ok6))
    print(f"  {'OK' if ok6 else 'FAIL'}")

    # ── R1: regression — normal chat unchanged (judge hook added) ──
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

    # ── R3: run_macro routes (tool path exists alongside the hook) ──
    print("\n--- R3: routing — 'check my macros' selects run_macro ---")
    from blankslate.tools.router import ToolRouter

    routed = await ToolRouter(cfg, registry).route("check my macros")
    routed_names = [t.name for t in routed.selected_tools]
    print(f"  routed: {routed_names}")
    ok_r3 = "run_macro" in routed_names
    results.append(("R3 run_macro routing", ok_r3))
    print(f"  {'OK' if ok_r3 else 'FAIL'}")

    # ── Cleanup ──
    for stem in FIXTURES:
        try:
            (macros_dir() / f"{stem}.json").unlink()
        except OSError:
            pass
    nt = notepad_titles()
    if nt and not user_notepad_open and all(SCRATCH_PREFIX in t for t in nt):
        await _h(registry, "app_kill", {"query": "notepad"})
        await asyncio.sleep(0.8)
    elif nt and user_notepad_open:
        print(f"  note: user Notepad left open, not killed: {nt}")
    if os.path.exists(scratch):
        try:
            os.remove(scratch)
        except OSError:
            pass

    # ── Summary ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"PHASE 3 GATE: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)

    await llm.close()
    sys.exit(0 if passed == total else 1)


asyncio.run(run_tests())
