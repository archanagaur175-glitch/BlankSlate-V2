"""Phase 2 gate test — Screen/Clipboard X-Ray + local vision (moondream).

Covers: foreground-window capture (memory-only unless save=), vision Q&A via
moondream, clipboard text with guardrail redaction, clipboard image (CF_DIB)
decode, the VRAM keep-alive benchmark, tool routing for the two new tools,
and regressions proving normal chat + normal tool routing are unchanged after
ollama_client gained vision().

Safety: types only into our scratch Notepad (foreground-verified), saves
screenshots only into temp, kills Notepad only when every Notepad window is
ours, restores the original clipboard at the end.

PREREQS (run yourself, first): pip install mss ; ollama pull moondream
"""
import asyncio
import base64
import io
import os
import struct
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import pyperclip
import win32clipboard
import win32con
import win32gui
from PIL import Image, ImageDraw, ImageFont

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.intent.judge import handle_text_input
from blankslate.llm.ollama_client import OllamaClient, OllamaError
from blankslate.tools.orchestrator import orchestrate
from blankslate.tools.registry import ToolRegistry
from blankslate.tools.xray import benchmark_vision_vram
from blankslate.vault import core as vault
from pathlib import Path

SCRATCH_PREFIX = "blankslate_xray_b"
MARKER = "XRAY_MARKER_42"
SAVE_PATH = os.path.join(tempfile.gettempdir(), "blankslate_xray_gate.png")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


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


def check_prereqs() -> list[str]:
    missing: list[str] = []
    try:
        import mss  # noqa: F401
    except ImportError:
        missing.append("mss -> run: pip install mss")
    try:
        import httpx

        tags = httpx.get("http://localhost:11434/api/tags", timeout=5.0).json()
        names = [m.get("name", "") for m in tags.get("models", [])]
        if not any(n.startswith("moondream") for n in names):
            missing.append(f"moondream -> run: ollama pull moondream (have: {names})")
    except Exception as exc:
        missing.append(f"ollama unreachable: {exc}")
    return missing


def _set_clipboard_dib(dib: bytes) -> None:
    opened = False
    for _ in range(3):
        try:
            win32clipboard.OpenClipboard()
            opened = True
            break
        except Exception:
            time.sleep(0.05)
    if not opened:
        raise RuntimeError("OpenClipboard failed in gate helper")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()


def _green_dib() -> bytes:
    """Hand-built 80x60 24bpp BI_RGB DIB (bottom-up), reader-verified pair."""
    w, h = 80, 60
    stride = ((w * 24 + 31) // 32) * 4
    row = bytes((0, 200, 0)) * w + bytes(stride - w * 3)  # B,G,R green
    header = struct.pack(
        "<IiiHHIIiiII", 40, w, h, 1, 24, 0, stride * h, 0, 0, 0, 0
    )
    return header + row * h  # bottom-up: rows in reverse display order = same color anyway


async def _h(registry: ToolRegistry, name: str, args: dict) -> dict:
    return await registry.get(name).handler(args)


async def run_tests():
    print("=" * 60)
    print("PHASE 2 GATE TESTS — Screen/Clipboard X-Ray + Vision")
    print("=" * 60)

    missing = check_prereqs()
    if missing:
        print("\nPREREQS MISSING — install them, then re-run:")
        for m in missing:
            print(f"  ! {m}")
        print("\nPHASE 2 GATE: BLOCKED (0 tests run)")
        sys.exit(1)

    cfg = DaemonConfig()
    object.__setattr__(cfg, "tools_enabled", True)
    ctx = ContextManager(cfg, max_turns=10)
    llm = OllamaClient(cfg)
    registry = ToolRegistry()
    results: list[tuple[str, bool]] = []

    clip_orig = None
    try:
        clip_orig = pyperclip.paste()
    except Exception:
        clip_orig = ""

    # Guaranteed restore even if a test raises (SystemExit/exception paths).
    import atexit

    def _restore_clipboard() -> None:
        try:
            pyperclip.copy(clip_orig or "")
        except Exception:
            pass

    def _remove_save() -> None:
        try:
            if os.path.exists(SAVE_PATH):
                os.remove(SAVE_PATH)
        except Exception:
            pass

    atexit.register(_restore_clipboard)
    atexit.register(_remove_save)

    # ── SETUP: scratch Notepad with a typed marker (only touch our windows) ──
    print("\n--- SETUP: scratch Notepad + marker text ---")
    existing = notepad_titles()
    user_notepad_open = bool(existing and not all(SCRATCH_PREFIX in t for t in existing))
    if existing and not user_notepad_open:
        await _h(registry, "app_kill", {"query": "notepad"})
        await asyncio.sleep(1.0)
    elif existing:
        print(f"  note: leaving user Notepad open: {existing}")
    scratch = os.path.join(tempfile.gettempdir(), f"{SCRATCH_PREFIX}.txt")
    with open(scratch, "w", encoding="utf-8") as fh:
        fh.write("xray gate scratch\n")
    os.startfile(scratch)
    await asyncio.sleep(2.5)
    if SCRATCH_PREFIX not in fg_title().lower():
        await _h(registry, "switch_window", {"title": SCRATCH_PREFIX})
        await asyncio.sleep(0.6)
    fg = fg_title()
    scratch_focused = SCRATCH_PREFIX in fg.lower()
    if scratch_focused:
        typed = await _h(registry, "type_text", {"text": f"{MARKER} alpha beta gamma"})
        await asyncio.sleep(0.4)
        scratch_focused = SCRATCH_PREFIX in fg_title().lower()
        print(f"  foreground: {fg!r} typed err: {typed.get('error')} -> ", end="")
    print("OK" if scratch_focused else "FAIL")
    results.append(("Setup: scratch + marker", scratch_focused))

    if not scratch_focused:
        for skipped in [
            "T1 capture + routing",
            "T2 save= writes PNG",
            "T3 vision reads BANANA",
            "T4 xray + question integration",
        ]:
            results.append((skipped, False))
    else:
        # ── T1: capture (memory-only) + routing of the new tools ──
        print("\n--- T1: screen_xray capture (no save) + routing ---")
        from blankslate.tools.router import ToolRouter

        routed = await ToolRouter(cfg, registry).route("what's on my screen")
        routed_names = [t.name for t in routed.selected_tools]
        r = await _h(registry, "screen_xray", {"query": "capture"})
        b64 = str(r.get("image_b64") or "")
        print(f"  routed: {routed_names}")
        print(
            f"  err: {r.get('error')} size: {r.get('width')}x{r.get('height')} "
            f"title: {r.get('window_title')!r} saved_to: {r.get('saved_to')}"
        )
        try:
            decoded_magic = base64.b64decode(b64)[:8] == PNG_MAGIC
        except Exception:
            decoded_magic = False
        ok1 = (
            r.get("error") is None
            and r.get("width", 0) >= 2
            and r.get("height", 0) >= 2
            and len(b64) > 100
            and decoded_magic
            and r.get("saved_to") is None
            and SCRATCH_PREFIX in str(r.get("window_title", "")).lower()
            and "screen_xray" in routed_names
            and "clipboard_read" not in routed_names
            and b64[:24] not in str(r.get("reply", ""))
        )
        results.append(("T1 capture + routing", ok1))
        print(f"  {'OK' if ok1 else 'FAIL'}")

        # ── T2: explicit save= writes a real PNG, nothing else writes ──
        print("\n--- T2: screen_xray save= -> PNG on disk ---")
        if os.path.exists(SAVE_PATH):
            os.remove(SAVE_PATH)
        r = await _h(registry, "screen_xray", {"query": "capture", "save": SAVE_PATH})
        await asyncio.sleep(0.2)
        file_ok = os.path.exists(SAVE_PATH)
        envelope_ok = False
        magic_ok = False
        if file_ok:
            # save= goes through the vault choke point (encrypt-on-write),
            # so the on-disk file is an envelope that decrypts to PNG bytes.
            raw = Path(SAVE_PATH).read_bytes()
            envelope_ok = vault.is_envelope(raw)
            try:
                plain = vault.read_bytes(Path(SAVE_PATH)) if envelope_ok else raw
                magic_ok = plain[:8] == PNG_MAGIC
            except Exception as exc:
                print(f"  decrypt error: {exc}")
        print(f"  err: {r.get('error')} saved_to: {r.get('saved_to')} file: {file_ok} "
              f"envelope: {envelope_ok} magic: {magic_ok}")
        ok2 = (r.get("error") is None and file_ok and envelope_ok and magic_ok
               and r.get("saved_to") == SAVE_PATH)
        results.append(("T2 save= writes PNG", ok2))
        print(f"  {'OK' if ok2 else 'FAIL'}")

    # ── T3: vision direct — moondream reads BANANA from a PIL image ──
    print("\n--- T3: vision (moondream) reads 'BANANA' from synthetic image ---")
    try:
        img = Image.new("RGB", (460, 150), "white")
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 110)
        except Exception:
            font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 110)
        ImageDraw.Draw(img).text((30, 10), "BANANA", fill="black", font=font)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        banana_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        vis_llm = OllamaClient(DaemonConfig())
        try:
            resp = await vis_llm.vision(
                "What word is written in the image?", banana_b64, keep_alive="0"
            )
        finally:
            await vis_llm.close()
        answer = resp.content.strip()
        print(f"  vision: {answer!r}")
        ok3 = "banana" in answer.lower()
    except OllamaError as exc:
        print(f"  OllamaError: {exc}")
        ok3 = False
    except Exception as exc:
        print(f"  ERROR: {exc}")
        ok3 = False
    results.append(("T3 vision reads BANANA", ok3))
    print(f"  {'OK' if ok3 else 'FAIL'}")

    if scratch_focused:
        # ── T4: capture + question -> vision answer (integration) ──
        print("\n--- T4: screen_xray {question} on scratch window ---")
        if SCRATCH_PREFIX not in fg_title().lower():
            await _h(registry, "switch_window", {"title": SCRATCH_PREFIX})
            await asyncio.sleep(0.6)
        r = await _h(registry, "screen_xray", {"query": "what's on my screen"})
        answer = str(r.get("answer") or "")
        print(f"  err: {r.get('error')} vision_error: {r.get('vision_error')}")
        print(f"  title: {r.get('window_title')!r}")
        print(f"  answer: {answer!r}")
        print(f"  soft marker check: {MARKER.lower() in answer.lower()}")
        ok4 = (
            r.get("error") is None
            and r.get("vision_error") is None
            and answer != ""
            and SCRATCH_PREFIX in str(r.get("window_title", "")).lower()
        )
        results.append(("T4 xray + question integration", ok4))
        print(f"  {'OK' if ok4 else 'FAIL'}")

    # ── T5: clipboard text + guardrail redaction ──
    print("\n--- T5: clipboard text + redaction ---")
    try:
        pyperclip.copy(f"hello clip CLIP_MARKER_77")
        r1 = await _h(registry, "clipboard_read", {})
        print(f"  plain: err={r1.get('error')} text={r1.get('text')!r} redacted={r1.get('redacted')}")
        secret = "token sk-9f8e7d6c5b4a3c2d1e0f"
        pyperclip.copy(secret)
        r2 = await _h(registry, "clipboard_read", {})
        print(f"  secret: text={r2.get('text')!r} redacted={r2.get('redacted')}")
        ok5 = (
            r1.get("error") is None
            and "CLIP_MARKER_77" in str(r1.get("text", ""))
            and r1.get("redacted") is False
            and r2.get("error") is None
            and "[REDACTED:api_key]" in str(r2.get("text", ""))
            and "9f8e7d6c5b4a3c2d1e0f" not in str(r2.get("text", ""))
            and r2.get("redacted") is True
        )
    except Exception as exc:
        print(f"  ERROR: {exc}")
        ok5 = False
    results.append(("T5 clipboard text + redaction", ok5))
    print(f"  {'OK' if ok5 else 'FAIL'}")

    # ── T6: clipboard image (CF_DIB) decode ──
    print("\n--- T6: clipboard image (CF_DIB) -> PNG base64 ---")
    try:
        _set_clipboard_dib(_green_dib())
        r = await _h(registry, "clipboard_read", {})
        img_b64 = str(r.get("image_b64") or "")
        print(
            f"  err: {r.get('error')} has_image: {r.get('has_image')} "
            f"size: {r.get('image_width')}x{r.get('image_height')}"
        )
        ok6 = (
            r.get("error") is None
            and r.get("has_image") is True
            and r.get("image_width") == 80
            and r.get("image_height") == 60
            and base64.b64decode(img_b64)[:8] == PNG_MAGIC
        )
    except Exception as exc:
        print(f"  ERROR: {exc}")
        ok6 = False
    results.append(("T6 clipboard image decode", ok6))
    print(f"  {'OK' if ok6 else 'FAIL'}")

    # ── T7: VRAM benchmark decides vision keep-alive ──
    print("\n--- T7: benchmark_vision_vram (chat->vision->chat, nvidia-smi) ---")
    try:
        b = await benchmark_vision_vram()
        for k in (
            "chat_ms_1", "vision_ms", "chat_ms_2",
            "vram_baseline_mb", "vram_peak_mb", "degraded", "keep_alive",
            "chat_1_answer", "vision_answer", "chat_2_answer",
        ):
            print(f"  {k}: {b.get(k)!r}")
        ok7 = (
            b.get("keep_alive") in ("0", "5m")
            and isinstance(b.get("chat_ms_1"), int)
            and isinstance(b.get("vision_ms"), int)
            and isinstance(b.get("chat_ms_2"), int)
            and bool(b.get("chat_1_answer"))
            and bool(b.get("chat_2_answer"))
            and bool(b.get("vision_answer"))
            and b.get("vram_peak_mb", 0) >= b.get("vram_baseline_mb", 0)
        )
    except OllamaError as exc:
        print(f"  OllamaError: {exc}")
        ok7 = False
    except Exception as exc:
        print(f"  ERROR: {exc}")
        ok7 = False
    results.append(("T7 VRAM benchmark", ok7))
    print(f"  {'OK' if ok7 else 'FAIL'}")

    # ── R1: regression — normal chat unchanged (ollama_client touched) ──
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

    # ── Cleanup ──
    try:
        pyperclip.copy(clip_orig or "")
    except Exception:
        pass
    if os.path.exists(SAVE_PATH):
        try:
            os.remove(SAVE_PATH)
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
    print(f"PHASE 2 GATE: {passed}/{total} tests passed")
    for name, ok in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
    print("=" * 60)

    await llm.close()
    sys.exit(0 if passed == total else 1)


asyncio.run(run_tests())
