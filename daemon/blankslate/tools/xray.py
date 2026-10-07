"""Screen & clipboard X-Ray — local capture of the active window and clipboard.

Phase 2 of desktop awareness. Everything stays in memory (base64); disk
writes happen only when explicitly asked (save=...).

Safety / boundaries:
- Window rect is clamped to the virtual screen (multi-monitor safe); a
  minimized window yields a structured error instead of a garbage capture
- Clipboard text passes through guardrail redaction before leaving the handler
- Vision Q&A runs locally via moondream through OllamaClient.vision()
- nvidia-smi VRAM sampling benchmark decides the vision model keep-alive
  policy (thrash on a 4GB card -> unload immediately, else stay warm)
- OpenClipboard uses a short retry (F6-class contention) — this is NEW code;
  text_injector's F6 fix remains a separate pending decision
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import struct
import subprocess
import threading
import time
from typing import Any

import numpy as np
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from PIL import Image, ImageDraw, ImageOps

from blankslate.config import DaemonConfig
from blankslate.guardrail.redact import redact_text
from blankslate.llm.ollama_client import OllamaClient, OllamaError
from blankslate.vault import core as vault

logger = logging.getLogger("blankslate.xray")

VISION_MODEL = "moondream"
# Updated by benchmark_vision_vram(): "5m" = stay warm, "0" = unload after use.
VISION_KEEP_ALIVE = "5m"

# Queries that are pure capture commands — no vision question to answer.
_CAPTURE_VERB_RE = re.compile(
    r"^(?:capture|screenshot|take a screenshot|screen capture|snap(?:shot)?"
    r"|xray|picture|screen|my screen|the screen"
    r"|capture (?:the |my )?screen)[\s.!?]*$",
    re.IGNORECASE,
)
_POLITE_PREFIX_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|i want to\s+|help me\s+)+",
    re.IGNORECASE,
)


def _extract_question(query: str) -> str:
    """Turn a voice query into a vision question, or "" for pure captures."""
    q = (query or "").strip()
    if not q:
        return ""
    if _CAPTURE_VERB_RE.match(_POLITE_PREFIX_RE.sub("", q)):
        return ""
    return q


# ── screen capture ────────────────────────────────────────────────────────────

def _capture_window_blocking() -> dict[str, Any]:
    """Capture the foreground window as PNG base64 (clamped to virtual screen)."""
    try:
        import mss  # noqa: PLC0415 — local import: structured error if missing
    except ImportError:
        return {"error": "screen capture unavailable: no module 'mss' (run: pip install mss)"}

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return {"error": "no foreground window to capture"}
        title = win32gui.GetWindowText(hwnd) or "(untitled)"

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        vs_left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        vs_top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        vs_w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        vs_h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        x1 = max(int(left), vs_left)
        y1 = max(int(top), vs_top)
        x2 = min(int(right), vs_left + vs_w)
        y2 = min(int(bottom), vs_top + vs_h)
        if x2 - x1 < 2 or y2 - y1 < 2:
            return {
                "error": (
                    f"window rect unusable (minimized or off-screen?): "
                    f"{left},{top},{right},{bottom}"
                )
            }

        with mss.mss() as sct:
            shot = sct.grab({"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1})
        img = Image.frombytes("RGB", shot.size, shot.rgb)

        buf = io.BytesIO()
        img.save(buf, format="PNG")

        process = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            import psutil  # noqa: PLC0415

            process = psutil.Process(pid).name()
        except Exception:
            process = ""

        return {
            "image_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "width": img.width,
            "height": img.height,
            "window_title": title,
            "process": process,
        }
    except Exception as exc:
        logger.exception("screen capture failed")
        return {"error": f"capture failed: {exc}"}


async def _ask_vision(question: str, image_b64: str) -> tuple[str | None, str | None]:
    """Local moondream Q&A. Returns (answer, error) — never raises.

    moondream is greedy (modelfile temperature 0) and has a phi-2 quirk where
    some short questions (e.g. "what color is...") emit its stop token first
    -> empty content. Surface that as an error so callers can fall back.
    """
    try:
        llm = OllamaClient(DaemonConfig())
        try:
            resp = await llm.vision(
                question, image_b64, model=VISION_MODEL, keep_alive=VISION_KEEP_ALIVE
            )
            content = resp.content.strip()
            if not content:
                return None, "model returned an empty answer"
            return content, None
        finally:
            await llm.close()
    except OllamaError as exc:
        return None, str(exc)
    except Exception as exc:
        logger.exception("vision failed")
        return None, f"vision failed: {exc}"


async def screen_xray(args: dict[str, Any]) -> dict[str, Any]:
    """Capture the foreground window; optional save= path; optional vision Q&A."""
    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, _capture_window_blocking)
        if "error" in info:
            return {
                "tool": "screen_xray",
                "error": info["error"],
                "result": None,
                "deterministic": False,
            }

        out: dict[str, Any] = {
            "tool": "screen_xray",
            "window_title": info["window_title"],
            "process": info["process"],
            "width": info["width"],
            "height": info["height"],
            "image_b64": info["image_b64"],
            "saved_to": None,
            "answer": None,
            "vision_error": None,
        }

        save = str(args.get("save") or "").strip()
        if save:
            def _write() -> str:
                vault.write_bytes(save, base64.b64decode(info["image_b64"]))
                return save

            path = await loop.run_in_executor(None, _write)
            out["saved_to"] = path
            reply = (
                f"Captured {out['width']}x{out['height']} of "
                f"'{out['window_title']}' and saved to {path}."
            )
        else:
            reply = f"Captured {out['width']}x{out['height']} of '{out['window_title']}'."

        question = str(args.get("question") or "").strip() or _extract_question(
            str(args.get("query") or "")
        )
        if question:
            answer, v_err = await _ask_vision(question, out["image_b64"])
            out["answer"] = answer
            out["vision_error"] = v_err
            if answer:
                reply = answer
            elif v_err:
                reply = f"{reply} (vision unavailable: {v_err})"

        out["reply"] = reply
        out["result"] = reply
        out["deterministic"] = True
        return out
    except Exception as exc:
        logger.exception("screen_xray failed")
        return {
            "tool": "screen_xray",
            "error": f"screen_xray failed: {exc}",
            "result": None,
            "deterministic": False,
        }


# ── clipboard ─────────────────────────────────────────────────────────────────

def _open_clipboard_blocking() -> bool:
    """OpenClipboard with short retry (F6-class contention, new code only)."""
    for _ in range(3):
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def _clipboard_probe_blocking() -> dict[str, Any]:
    """Read clipboard text + image presence. Text is RAW (redaction at handler)."""
    opened = False
    try:
        opened = _open_clipboard_blocking()
        if not opened:
            return {"error": "clipboard busy (OpenClipboard failed after 3 retries)"}
        text = ""
        has_text = False
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            raw = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            text = raw if isinstance(raw, str) else str(raw)
            has_text = bool(text)
        has_image = bool(win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB))
        return {"text": text, "has_text": has_text, "has_image": has_image}
    except Exception as exc:
        return {"error": f"clipboard read failed: {exc}"}
    finally:
        if opened:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass


def _dib_to_png_b64(dib: bytes) -> tuple[str, int, int]:
    """Decode a BITMAPINFOHEADER DIB (24/32bpp, BI_RGB) to PNG base64."""
    if len(dib) < 40:
        raise ValueError("DIB too small")
    bi_size = struct.unpack_from("<I", dib, 0)[0]
    w = struct.unpack_from("<i", dib, 4)[0]
    h_raw = struct.unpack_from("<i", dib, 8)[0]
    _planes, bpp = struct.unpack_from("<HH", dib, 12)
    compression = struct.unpack_from("<I", dib, 16)[0]
    if compression != 0:
        raise ValueError(f"unsupported DIB compression {compression} (BI_RGB only)")
    top_down = h_raw < 0
    h = abs(h_raw)
    if w <= 0 or h <= 0:
        raise ValueError(f"bad DIB size {w}x{h}")
    if bpp not in (24, 32):
        raise ValueError(f"unsupported DIB bit depth {bpp} (need 24 or 32)")
    stride = ((w * bpp + 31) // 32) * 4
    pixels = dib[bi_size: bi_size + stride * h]
    if len(pixels) < stride * h:
        raise ValueError("truncated DIB pixel data")
    channels = bpp // 8
    arr = (
        np.frombuffer(pixels, dtype=np.uint8)
        .reshape(h, stride)[:, : w * channels]
        .reshape(h, w, channels)
    )
    if bpp == 32:
        rgb = arr[:, :, [2, 1, 0]]  # BGRA -> RGB (alpha dropped)
    else:
        rgb = arr[:, :, ::-1]  # BGR -> RGB
    img = Image.fromarray(np.ascontiguousarray(rgb))
    if not top_down:
        img = ImageOps.flip(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), w, h


def _clipboard_image_blocking() -> dict[str, Any]:
    """Fetch CF_DIB bytes and decode to PNG base64."""
    opened = False
    try:
        opened = _open_clipboard_blocking()
        if not opened:
            return {"error": "clipboard busy (OpenClipboard failed after 3 retries)"}
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB):
            return {"error": "no image on clipboard"}
        dib = win32clipboard.GetClipboardData(win32con.CF_DIB)
        if not isinstance(dib, (bytes, bytearray)):
            return {"error": f"unexpected clipboard image payload type {type(dib).__name__}"}
        b64, w, h = _dib_to_png_b64(bytes(dib))
        return {"image_b64": b64, "width": w, "height": h}
    except Exception as exc:
        return {"error": f"clipboard image decode failed: {exc}"}
    finally:
        if opened:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass


async def clipboard_read(args: dict[str, Any]) -> dict[str, Any]:
    """Read clipboard: redacted text + optional image (base64 in memory) + vision."""
    try:
        loop = asyncio.get_running_loop()
        probe = await loop.run_in_executor(None, _clipboard_probe_blocking)
        if "error" in probe:
            return {
                "tool": "clipboard_read",
                "error": probe["error"],
                "result": None,
                "deterministic": False,
            }

        raw_text = probe["text"]
        safe_text = redact_text(raw_text) if raw_text else ""

        img_b64: str | None = None
        iw = ih = 0
        image_error: str | None = None
        if probe["has_image"]:
            img_info = await loop.run_in_executor(None, _clipboard_image_blocking)
            if "error" in img_info:
                image_error = img_info["error"]
            else:
                img_b64 = img_info["image_b64"]
                iw, ih = img_info["width"], img_info["height"]

        parts: list[str] = []
        if safe_text:
            parts.append(safe_text)
        elif probe["has_text"]:
            parts.append("(text on clipboard)")
        if probe["has_image"]:
            if img_b64:
                parts.append(f"[image on clipboard: {iw}x{ih}]")
            elif image_error:
                parts.append(f"[image on clipboard, unreadable: {image_error}]")
        reply = " ".join(parts) if parts else "Clipboard is empty."

        answer: str | None = None
        v_err: str | None = None
        question = str(args.get("question") or "").strip() or _extract_question(
            str(args.get("query") or "")
        )
        if question and img_b64:
            answer, v_err = await _ask_vision(question, img_b64)
            if answer:
                reply = answer
            elif v_err:
                reply = f"{reply} (vision unavailable: {v_err})"

        return {
            "tool": "clipboard_read",
            "text": safe_text,
            "text_length": len(raw_text),
            "redacted": "[REDACTED" in safe_text,
            "has_image": probe["has_image"],
            "image_width": iw,
            "image_height": ih,
            "image_b64": img_b64,
            "answer": answer,
            "vision_error": v_err,
            "reply": reply,
            "result": reply,
            "deterministic": True,
        }
    except Exception as exc:
        logger.exception("clipboard_read failed")
        return {
            "tool": "clipboard_read",
            "error": f"clipboard_read failed: {exc}",
            "result": None,
            "deterministic": False,
        }


# ── VRAM benchmark (decides VISION_KEEP_ALIVE) ───────────────────────────────

def _vram_sampler(stop: threading.Event, samples: list[int]) -> None:
    while not stop.is_set():
        try:
            proc = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                samples.append(int(proc.stdout.strip().splitlines()[0]))
        except Exception:
            pass
        stop.wait(0.5)


async def benchmark_vision_vram() -> dict[str, Any]:
    """chat -> vision -> chat with 500ms nvidia-smi sampling.

    Thrash signature: chat latency AFTER vision much worse than BEFORE (the
    4GB card is swapping models). Thrash -> keep_alive "0" (moondream unloads
    right after use, qwen stays resident); clean -> "5m" (both fit, stay warm).
    Sets module VISION_KEEP_ALIVE so later screen_xray calls use the decision.
    """
    global VISION_KEEP_ALIVE
    cfg = DaemonConfig()
    llm = OllamaClient(cfg)
    stop = threading.Event()
    samples: list[int] = []
    sampler = threading.Thread(target=_vram_sampler, args=(stop, samples), daemon=True)
    sampler.start()
    try:
        await asyncio.sleep(1.1)  # let 2 samples land for a baseline
        baseline = min(samples) if samples else 0

        t0 = time.perf_counter()
        r1 = await llm.generate("Reply with only: ok")
        chat1_ms = (time.perf_counter() - t0) * 1000

        probe = Image.new("RGB", (96, 96), (220, 40, 40))
        draw = ImageDraw.Draw(probe)
        draw.rectangle([8, 8, 88, 88], outline=(255, 255, 255), width=3)
        buf = io.BytesIO()
        probe.save(buf, format="PNG")
        probe_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        t0 = time.perf_counter()
        # Reliable phrasing: moondream's greedy phi-2 quirk returns empty on
        # many short color questions ("what color fills..."); describe-style
        # prompts are stable (verified via temp=0 matrix).
        v = await llm.vision(
            "Describe this image briefly.", probe_b64,
            model=VISION_MODEL, keep_alive=VISION_KEEP_ALIVE,
        )
        vision_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        r3 = await llm.generate("Reply with only: ok")
        chat2_ms = (time.perf_counter() - t0) * 1000

        peak = max(samples) if samples else 0
        degraded = chat2_ms > max(chat1_ms * 1.8, chat1_ms + 2500)
        recommendation = "0" if degraded else "5m"
        VISION_KEEP_ALIVE = recommendation
        logger.info(
            "VRAM benchmark: chat1=%.0fms vision=%.0fms chat2=%.0fms "
            "baseline=%dMB peak=%dMB -> keep_alive=%s",
            chat1_ms, vision_ms, chat2_ms, baseline, peak, recommendation,
        )
        return {
            "chat_ms_1": round(chat1_ms),
            "vision_ms": round(vision_ms),
            "chat_ms_2": round(chat2_ms),
            "chat_1_answer": r1.content.strip(),
            "vision_answer": v.content.strip(),
            "chat_2_answer": r3.content.strip(),
            "vram_baseline_mb": baseline,
            "vram_peak_mb": peak,
            "degraded": degraded,
            "keep_alive": recommendation,
        }
    finally:
        stop.set()
        sampler.join(4)
        await llm.close()
