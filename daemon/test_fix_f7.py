"""F7 GATE — natural-language extraction for switch_window.

T1  _extract_window_target unit table (prefixes, word-boundary guard,
    leading/trailing filler, no-op inputs)
T2  live NL switch: structured window on target B, then raw query
    'switch to my <A>' -> foreground MUST become A (disk-free, window-title
    ground truth via GetForegroundWindow), 3 attempts on focus steal
T3  structured regression: args['title'] path still switches B <- A
    (proves the explicit-title path is untouched and the switch is real)
T4  negative NL: unknown target -> structured error quoting the RAW query
    plus available-window list
T5  routing regression: 'switch to my <A>' still routes to switch_window

Ground truth: extractor outputs, live foreground window title, router output.
Target windows are own Tk processes (unique per-run titles) — no user window
is disturbed; cleanup terminates only our own processes.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import time

import win32gui

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

from blankslate.config import DaemonConfig
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.router import ToolRouter
from blankslate.tools.window_manager import _extract_window_target, switch_window

registry = ToolRegistry()
results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'OK' if ok else 'FAIL'}   {name}" + (f" — {note}" if note else ""))


def fg_title() -> str:
    return win32gui.GetWindowText(win32gui.GetForegroundWindow())


def spawn_title_window(title: str) -> subprocess.Popen:
    code = (
        "import tkinter as tk; "
        "r = tk.Tk(); "
        f"r.title({title!r}); "
        "r.geometry('180x60'); "
        "r.after(60000, r.destroy); "
        "r.mainloop()"
    )
    return subprocess.Popen([sys.executable, "-c", code])


async def switch_and_verify(call, needle: str, attempts: int = 3) -> tuple[bool, dict | None, str]:
    res = None
    for i in range(attempts):
        res = await call()
        await asyncio.sleep(0.6)
        fg = fg_title()
        if needle.lower() in fg.lower():
            return True, res, f"attempt {i + 1}, fg={fg!r}"
        await asyncio.sleep(0.4)
    return False, res, f"fg={fg_title()!r} after {attempts} attempts"


async def run_tests():
    print("=" * 60)
    print("F7 GATE TESTS — switch_window NL extraction")
    print("=" * 60)

    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        rec("F7 gate prereq (tkinter)", False, f"tkinter unavailable: {exc}")
        print("=" * 60)
        return 1

    pid = os.getpid()
    w_a = f"j7a_{pid}"
    w_b = f"j7b_{pid}"
    procs: list[subprocess.Popen] = []

    try:
        # ── T1: extractor unit table ──
        print("\n--- T1: _extract_window_target unit table ---")
        cases = [
            ("switch to Brave", "Brave"),
            ("switch to my Notepad", "Notepad"),
            ("bring up the calculator", "calculator"),
            ("focus on Visual Studio Code", "Visual Studio Code"),
            ("go to my email", "email"),
            ("Switch to Edge", "Edge"),
            ("bring me to the terminal", "terminal"),
            ("open my notes app", "notes"),
            ("switch to my j7a window", "j7a"),   # leading + trailing filler
            ("switched to something", "switched to something"),  # word-boundary guard
            ("brave", "brave"),                   # no prefix -> unchanged
            ("switch to", ""),                     # nothing after prefix
            ("focus", ""),
        ]
        fails = []
        for query, expected in cases:
            got = _extract_window_target(query)
            mark = "ok" if got == expected else "FAIL"
            if got != expected:
                fails.append((query, expected, got))
            print(f"    {mark}: {query!r} -> {got!r}")
        rec("T1 extractor unit table (13 cases)", not fails, str(fails))

        # ── T2/T3: live switching between our two windows ──
        print("\n--- T2/T3: live NL + structured switching ---")
        procs.append(spawn_title_window(w_a))
        procs.append(spawn_title_window(w_b))
        await asyncio.sleep(1.5)

        # setup: put foreground on B (structured)
        moved, _, note = await switch_and_verify(
            lambda: switch_window({"title": w_b}), w_b
        )
        print(f"  setup fg=B: {note}")
        setup_ok = moved

        # T2: NL query with filler -> A
        ok2, res2, note2 = await switch_and_verify(
            lambda: switch_window({"query": f"switch to my {w_a}"}), w_a
        )
        print(f"  T2 NL: {note2}")
        if res2 and res2.get("error"):
            print(f"    error={res2['error']!r}")
        rec("T2 NL query 'switch to my <A>' switches to A",
            bool(setup_ok and ok2 and not (res2 or {}).get("error")), note2)

        # T3: structured title -> B (explicit path untouched, fg really moves)
        ok3, res3, note3 = await switch_and_verify(
            lambda: switch_window({"title": w_b}), w_b
        )
        print(f"  T3 structured: {note3}")
        if res3 and res3.get("error"):
            print(f"    error={res3['error']!r}")
        rec("T3 structured args['title'] still switches (A -> B)",
            bool(ok3 and not (res3 or {}).get("error")), note3)

        # ── T4: negative NL ──
        print("\n--- T4: negative NL query ---")
        raw = "switch to ZzzNoSuchWindow777"
        res4 = await switch_window({"query": raw})
        err = str(res4.get("error", ""))
        ok4 = (
            "no window found" in err
            and raw in err
            and bool(res4.get("available"))
            and res4.get("result") is None
        )
        print(f"  error={err!r} available={len(res4.get('available') or [])}")
        rec("T4 negative: structured error quoting raw query", ok4)

        # ── T5: routing regression ──
        print("\n--- T5: routing regression ---")
        cfg = DaemonConfig()
        routed = await ToolRouter(cfg, registry).route(f"switch to my {w_a}")
        selected = [t.name for t in routed.selected_tools]
        print(f"  selected={selected} by_keyword={routed.by_keyword}")
        rec("T5 'switch to my <A>' routes to switch_window",
            "switch_window" in selected or "switch_window" in routed.by_keyword)

    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        await asyncio.sleep(0.8)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

        def cb(hwnd, acc):
            t = win32gui.GetWindowText(hwnd) or ""
            if t.startswith("j7"):
                acc.append(t)
            return True

        acc: list[str] = []
        win32gui.EnumWindows(cb, acc)
        if acc:
            print(f"  cleanup warning: leftover windows {acc}")

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"F7 GATE: {passed}/{len(results)} tests passed")
    for name, ok, _ in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
