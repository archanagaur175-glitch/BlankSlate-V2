"""F6 GATE — clipboard OpenClipboard retry (text_injector only).

T1  flaky OpenClipboard (2 failures) -> _set_clipboard succeeds after
    exactly 3 attempts with >=90ms backoff (2 x 50ms)
T2  flaky OpenClipboard (2 failures) -> _get_clipboard still returns content
T3  always-failing OpenClipboard -> _set_clipboard raises after exactly 3
    attempts; _get_clipboard returns "" (swallow preserved); clipboard untouched
T4  real typing smoke: type_text -> scratch Notepad -> Ctrl+S -> marker on
    disk (foreground-guarded, 3 attempts on foreground steal)
T5  clipboard restore regression: type_text restores the pre-call clipboard
R1  F1 extractor regressions (code paths F6 must not have touched)

Ground truth: mock call counts/timers, disk content, clipboard round-trip,
extractor outputs — never handler optimism.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import time
from unittest import mock

import pywintypes
import win32gui

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )

import blankslate.tools.text_injector as ti
from blankslate.tools.registry import ToolRegistry

SCRATCH_PREFIX = "blankslate_f6"
registry = ToolRegistry()
results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'OK' if ok else 'FAIL'}   {name}" + (f" — {note}" if note else ""))


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


def make_flaky(fails: int, counter: dict, real_open):
    def flaky():
        counter["n"] += 1
        if counter["n"] <= fails:
            raise pywintypes.error(5, "OpenClipboard", "Access is denied.")
        real_open()

    return flaky


async def _h(name: str, args: dict) -> dict:
    return await registry.get(name).handler(args)


async def run_tests():
    print("=" * 60)
    print("F6 GATE TESTS — clipboard OpenClipboard retry")
    print("=" * 60)

    real_open = ti.win32clipboard.OpenClipboard

    # ── T1: flaky set (2 failures) succeeds on attempt 3, with backoff ──
    print("\n--- T1: _set_clipboard with 2 induced OpenClipboard failures ---")
    orig_clip = ti._get_clipboard()
    counter = {"n": 0}
    t0 = time.perf_counter()
    t1_note = ""
    try:
        with mock.patch.object(
            ti.win32clipboard, "OpenClipboard",
            new=make_flaky(2, counter, real_open),
        ):
            ti._set_clipboard("F6 probe text")
        attempts = counter["n"]
        elapsed = time.perf_counter() - t0
        readback = ti._get_clipboard()  # outside patch: counter may bump, that's fine
        ok1 = attempts == 3 and elapsed >= 0.09 and readback == "F6 probe text"
        t1_note = f"attempts={attempts} elapsed={elapsed*1000:.0f}ms readback_ok={readback == 'F6 probe text'}"
    except Exception as exc:
        ok1 = False
        t1_note = f"{type(exc).__name__}: {exc} (attempts={counter['n']})"
    finally:
        ti._set_clipboard(orig_clip)
    print(f"  {t1_note}")
    rec("T1 set retries: 3 attempts, >=90ms backoff, success", ok1, t1_note)

    # ── T2: flaky get still returns content ──
    print("\n--- T2: _get_clipboard with 2 induced failures ---")
    counter2 = {"n": 0}
    saved = ""
    try:
        ti._set_clipboard("KEEP_ME_F6")
        with mock.patch.object(
            ti.win32clipboard, "OpenClipboard",
            new=make_flaky(2, counter2, real_open),
        ):
            saved = ti._get_clipboard()
        ok2 = counter2["n"] == 3 and saved == "KEEP_ME_F6"
        note2 = f"attempts={counter2['n']} value_ok={saved == 'KEEP_ME_F6'}"
    finally:
        ti._set_clipboard(orig_clip)
    print(f"  {note2}")
    rec("T2 get retries: content returned after induced failures", ok2, note2)

    # ── T3: always-fail -> bounded raise / bounded swallow ──
    print("\n--- T3: always-failing OpenClipboard (3 attempts, then give up) ---")
    counter3 = {"n": 0}
    note3 = ""
    ok3 = False
    try:
        with mock.patch.object(
            ti.win32clipboard, "OpenClipboard",
            new=make_flaky(10_000, counter3, real_open),
        ):
            raised = False
            try:
                ti._set_clipboard("should not land")
            except Exception:
                raised = True
            got = ti._get_clipboard()
        after = ti._get_clipboard()
        ok3 = raised and counter3["n"] == 6 and got == "" and after == orig_clip
        note3 = (f"set_raised={raised} attempts={counter3['n']} "
                 f"(expect 6 = 3 per call x 2 calls) get_swallows={got == ''} "
                 f"clipboard_intact={after == orig_clip}")
    except Exception as exc:
        note3 = f"{type(exc).__name__}: {exc}"
    finally:
        ti._set_clipboard(orig_clip)
    print(f"  {note3}")
    rec("T3 bounded give-up (set raises, get swallows, clipboard intact)",
        ok3, note3)

    # ── T4 + T5: real typing into scratch Notepad ──
    print("\n--- T4/T5: real typing smoke + clipboard restore ---")
    existing = notepad_titles()
    user_notepad_open = bool(existing and not all(
        SCRATCH_PREFIX in t for t in existing
    ))
    if existing and not user_notepad_open:
        await _h("app_kill", {"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")
    scratch = os.path.join(
        tempfile.gettempdir(), f"{SCRATCH_PREFIX}_{os.getpid()}.txt"
    )
    with open(scratch, "w", encoding="utf-8") as fh:
        fh.write("f6 gate scratch\n")
    os.startfile(scratch)
    await asyncio.sleep(2.5)

    async def ensure_fg() -> bool:
        if SCRATCH_PREFIX in fg_title().lower():
            return True
        await _h("switch_window", {"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.7)
        ok = SCRATCH_PREFIX in fg_title().lower()
        print(f"  fg re-guard: {fg_title()!r} -> {'OK' if ok else 'FAIL'}")
        return ok

    marker = f"F6_SMOKE_{os.getpid()}"
    ok4, note4 = False, ""
    for attempt in range(1, 4):
        if not await ensure_fg():
            note4 = f"attempt {attempt}: could not focus scratch"
            continue
        res = await ti.type_text({"text": marker})
        await _h("hotkey", {"combo": "ctrl+s"})
        await asyncio.sleep(0.7)
        try:
            content = open(scratch, "r", encoding="utf-8").read()
        except OSError as exc:
            content = f"<read error: {exc}>"
        stolen = SCRATCH_PREFIX not in fg_title().lower()
        print(f"  attempt {attempt}: error={res.get('error')} "
              f"on_disk={marker in content} fg_stolen={stolen}")
        if marker in content and not stolen:
            ok4 = True
            note4 = f"attempt {attempt}, error={res.get('error')}"
            break
    rec("T4 type_text writes marker to disk (retry attempt noted)", ok4, note4)

    # T5: pre-set clipboard, type, verify restore
    keep = f"KEEP_F6_{os.getpid()}"
    ok5, note5 = False, ""
    try:
        ti._set_clipboard(keep)
        if await ensure_fg():
            res5 = await ti.type_text({"text": " restore probe"})
            after5 = ti._get_clipboard()
            ok5 = after5 == keep
            note5 = f"restored={after5 == keep} error={res5.get('error')}"
        else:
            note5 = "could not focus scratch"
    finally:
        # give the user their clipboard back (best effort)
        try:
            ti._set_clipboard(orig_clip if orig_clip else "")
        except Exception:
            pass
    print(f"  {note5}")
    rec("T5 type_text restores pre-call clipboard", ok5, note5)

    # ── R1: F1 extractor regressions (untouched by F6) ──
    print("\n--- R1: F1 extractor regressions ---")
    e1 = ti._extract_text_to_type('type "hi there"') == "hi there"
    e2 = ti._extract_text_to_type("type hello") == "hello"
    e3 = ti._extract_key("press enter") == "enter"
    e4 = ti._extract_combo("press ctrl shift t") == "ctrl+shift+t"
    print(f"  quoted={e1} verb={e2} key={e3} combo={e4}")
    rec("R1 F1 extractors unchanged", all((e1, e2, e3, e4)))

    # ── Cleanup ──
    nt = notepad_titles()
    if nt and not user_notepad_open and all(SCRATCH_PREFIX in t for t in nt):
        await _h("app_kill", {"query": "notepad"})
        await asyncio.sleep(0.8)
    elif nt and user_notepad_open:
        print(f"  note: user Notepad left open: {nt}")
    if os.path.exists(scratch):
        try:
            os.remove(scratch)
        except OSError:
            pass

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"F6 GATE: {passed}/{len(results)} tests passed")
    for name, ok, _ in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
