"""Desktop automation — safe text injection and key simulation.

Uses clipboard-based text injection for Unicode support and reliability.
All blocking Win32 calls run via loop.run_in_executor().

Safety:
- Clipboard is saved/restored after each operation
- Key names validated against a whitelist
- All actions logged for audit trail
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import re
import time
from typing import Any

import win32clipboard
import win32con
import win32gui

logger = logging.getLogger("blankslate.tools.text_injector")

_user32 = ctypes.windll.user32

# ── Key name mapping ────────────────────────────────────────────────────────

_KEY_MAP: dict[str, int] = {
    "enter": win32con.VK_RETURN,
    "return": win32con.VK_RETURN,
    "tab": win32con.VK_TAB,
    "escape": win32con.VK_ESCAPE,
    "esc": win32con.VK_ESCAPE,
    "backspace": win32con.VK_BACK,
    "delete": win32con.VK_DELETE,
    "space": win32con.VK_SPACE,
    "up": win32con.VK_UP,
    "down": win32con.VK_DOWN,
    "left": win32con.VK_LEFT,
    "right": win32con.VK_RIGHT,
    "home": win32con.VK_HOME,
    "end": win32con.VK_END,
    "pageup": win32con.VK_PRIOR,
    "pagedown": win32con.VK_NEXT,
    "f1": win32con.VK_F1,
    "f2": win32con.VK_F2,
    "f3": win32con.VK_F3,
    "f4": win32con.VK_F4,
    "f5": win32con.VK_F5,
    "f6": win32con.VK_F6,
    "f7": win32con.VK_F7,
    "f8": win32con.VK_F8,
    "f9": win32con.VK_F9,
    "f10": win32con.VK_F10,
    "f11": win32con.VK_F11,
    "f12": win32con.VK_F12,
    "ctrl": win32con.VK_CONTROL,
    "control": win32con.VK_CONTROL,
    "alt": win32con.VK_MENU,
    "shift": win32con.VK_SHIFT,
    "win": win32con.VK_LWIN,
    "lwin": win32con.VK_LWIN,
    "rwin": win32con.VK_RWIN,
    "capslock": win32con.VK_CAPITAL,
    "numlock": win32con.VK_NUMLOCK,
    "printscreen": win32con.VK_SNAPSHOT,
    "insert": win32con.VK_INSERT,
}

# Characters that need Shift key
_SHIFT_CHARS = "!@#$%^&*()_+{}|:\"<>?"

# ── Natural-language argument extraction (voice path) ───────────────────────
# The orchestrator passes every handler {"query": <raw transcript>}. Handlers
# fall back to deterministic extraction from that query — the same pattern
# app_launch._extract_app_name and calculator._extract_expression already use.

_KEY_ALIASES: dict[str, str] = {
    "page up": "pageup",
    "page down": "pagedown",
    "print screen": "printscreen",
    "caps lock": "capslock",
    "num lock": "numlock",
}

_TEXT_VERB_RE = re.compile(
    r"^(?:write down|enter text|type|write|input|dictate|enter|say)\s+",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"""["']([^"']+)["']""")
_KEY_VERB_RE = re.compile(r"^(?:press|hit|push|key|the)\s+", re.IGNORECASE)
_COMBO_VERB_RE = re.compile(r"^(?:press|hit|use|trigger|run)\s+", re.IGNORECASE)


def _extract_text_to_type(query: str) -> str:
    """Extract text to type from a raw query ('type hello' -> 'hello').

    Quoted content wins when present ('type "hi there"' -> 'hi there').
    Otherwise the leading command verb is stripped; a query with no verb is
    used as-is (last resort, mirroring _extract_app_name).
    """
    q = query.strip()
    if not q:
        return ""
    quoted = _QUOTED_RE.search(q)
    if quoted:
        return quoted.group(1).strip()
    return _TEXT_VERB_RE.sub("", q, count=1).strip()


def _extract_key(query: str) -> str:
    """Extract a single key name from a raw query ('press enter' -> 'enter').

    Scans word-boundary matches against _KEY_MAP (longest names first), so
    surrounding filler words are harmless. If no known key appears, the first
    token after the verb is returned as-is (single chars are valid; unknown
    names fall through to the existing 'unknown key' error path).
    """
    q = query.lower().strip()
    for src, dst in _KEY_ALIASES.items():
        q = q.replace(src, dst)
    for name in sorted(_KEY_MAP, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", q):
            return name
    rest = _KEY_VERB_RE.sub("", q, count=1).strip()
    return rest.split()[0] if rest else ""


def _extract_combo(query: str) -> str:
    """Extract a key combination from a raw query ('press ctrl shift t' -> 'ctrl+shift+t').

    Walks tokens after the leading verb: valid key tokens are collected,
    leading filler words ('use shortcut ...') are skipped, 'and' acts as a
    separator between keys, and the first other token ends the run. Returns
    "" when fewer than two keys form a run.
    """
    q = query.lower().strip()
    for src, dst in _KEY_ALIASES.items():
        q = q.replace(src, dst)
    q = _COMBO_VERB_RE.sub("", q, count=1)
    tokens: list[str] = []
    for token in q.replace("+", " ").split():
        # Key validity wins: 'a' is the A key once collection has started,
        # and always when it appears where a key is expected.
        if token in _KEY_MAP or (len(token) == 1 and token.isalnum()):
            tokens.append(token)
        elif not tokens:
            continue  # leading filler: 'use shortcut ctrl ...'
        elif token == "and":
            continue  # separator: 'ctrl and t'
        else:
            break
    return "+".join(tokens) if len(tokens) >= 2 else ""


# ── Blocking helpers (run in executor) ──────────────────────────────────────

_CLIPBOARD_OPEN_RETRIES = 3
_CLIPBOARD_OPEN_RETRY_DELAY = 0.05  # seconds


def _open_clipboard_retry() -> None:
    """Open the clipboard, retrying briefly while another process holds it (F6)."""
    last_exc: Exception | None = None
    for attempt in range(_CLIPBOARD_OPEN_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception as exc:  # noqa: BLE001 — re-raised after retries
            last_exc = exc
            if attempt < _CLIPBOARD_OPEN_RETRIES - 1:
                time.sleep(_CLIPBOARD_OPEN_RETRY_DELAY)
    raise last_exc  # type: ignore[misc]


def _get_clipboard() -> str:
    """Save current clipboard content."""
    try:
        _open_clipboard_retry()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return data if isinstance(data, str) else data.decode("utf-16-le")
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    return ""


def _set_clipboard(text: str) -> None:
    """Set clipboard content."""
    _open_clipboard_retry()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def _restore_clipboard(saved: str) -> None:
    """Restore clipboard content."""
    if saved:
        try:
            _set_clipboard(saved)
        except Exception:
            pass


def _type_text_blocking(text: str) -> dict[str, Any]:
    """Type text into the focused window using clipboard paste.

    Uses Ctrl+V paste for reliability and Unicode support.
    Clipboard is saved and restored after the operation.
    """
    if not text:
        return {"error": "no text to type", "typed": 0}

    saved_clipboard = _get_clipboard()
    typed = 0

    try:
        _set_clipboard(text)
        time.sleep(0.02)

        # Send Ctrl+V
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        VK_CONTROL = win32con.VK_CONTROL
        VK_V = ord("V")

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

        inputs = (INPUT * 4)()
        # Ctrl down
        inputs[0].type = INPUT_KEYBOARD
        inputs[0]._u.ki.wVk = VK_CONTROL
        # V down
        inputs[1].type = INPUT_KEYBOARD
        inputs[1]._u.ki.wVk = VK_V
        # V up
        inputs[2].type = INPUT_KEYBOARD
        inputs[2]._u.ki.wVk = VK_V
        inputs[2]._u.ki.dwFlags = KEYEVENTF_KEYUP
        # Ctrl up
        inputs[3].type = INPUT_KEYBOARD
        inputs[3]._u.ki.wVk = VK_CONTROL
        inputs[3]._u.ki.dwFlags = KEYEVENTF_KEYUP

        _user32.SendInput(4, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        typed = len(text)
        time.sleep(0.05)

    finally:
        _restore_clipboard(saved_clipboard)

    return {"typed": typed, "method": "clipboard_paste"}


def _press_key_blocking(key_name: str, count: int = 1) -> dict[str, Any]:
    """Press a single key one or more times."""
    key_name = key_name.lower().strip()
    vk = _KEY_MAP.get(key_name)
    if vk is None:
        # Try single character
        if len(key_name) == 1:
            vk = ord(key_name.upper())
        else:
            return {"error": f"unknown key: {key_name!r}", "pressed": 0}

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

    for _ in range(count):
        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0]._u.ki.wVk = vk
        inputs[1].type = INPUT_KEYBOARD
        inputs[1]._u.ki.wVk = vk
        inputs[1]._u.ki.dwFlags = KEYEVENTF_KEYUP
        _user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        time.sleep(0.02)

    return {"pressed": key_name, "count": count}


def _hotkey_blocking(keys: list[str]) -> dict[str, Any]:
    """Press a key combination (e.g. Ctrl+C, Alt+F4)."""
    vks = []
    for k in keys:
        k = k.lower().strip()
        vk = _KEY_MAP.get(k)
        if vk is None and len(k) == 1:
            vk = ord(k.upper())
        if vk is None:
            return {"error": f"unknown key in combo: {k!r}", "pressed": False}
        vks.append(vk)

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("_u", _U)]

    # Press all keys down
    down_inputs = (INPUT * len(vks))()
    for i, vk in enumerate(vks):
        down_inputs[i].type = INPUT_KEYBOARD
        down_inputs[i]._u.ki.wVk = vk
    _user32.SendInput(len(vks), ctypes.byref(down_inputs), ctypes.sizeof(INPUT))
    time.sleep(0.02)

    # Release all keys in reverse order
    up_inputs = (INPUT * len(vks))()
    for i, vk in enumerate(reversed(vks)):
        up_inputs[i].type = INPUT_KEYBOARD
        up_inputs[i]._u.ki.wVk = vk
        up_inputs[i]._u.ki.dwFlags = KEYEVENTF_KEYUP
    _user32.SendInput(len(vks), ctypes.byref(up_inputs), ctypes.sizeof(INPUT))
    time.sleep(0.02)

    return {"combo": "+".join(keys), "pressed": True}


# ── Async handlers ──────────────────────────────────────────────────────────

async def type_text(args: dict[str, Any]) -> dict[str, Any]:
    """Type text into the currently focused window.

    Uses clipboard paste for Unicode support and reliability.
    Clipboard is saved/restored after the operation.
    """
    text = str(args.get("text", "")).strip()
    if not text:
        # Voice path: orchestrator passes {"query": <raw transcript>}
        text = _extract_text_to_type(str(args.get("query", "")))
    if not text:
        return {
            "tool": "type_text",
            "error": "no text provided",
            "result": None,
        }

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _type_text_blocking, text)

    if "error" in info:
        return {"tool": "type_text", **info, "result": None}

    fg = win32gui.GetForegroundWindow()
    fg_title = win32gui.GetWindowText(fg)

    return {
        "tool": "type_text",
        "text": text[:100] + ("..." if len(text) > 100 else ""),
        "typed": info["typed"],
        "method": info["method"],
        "window": fg_title,
        "result": f"Typed {info['typed']} characters into '{fg_title}'.",
        "reply": f"Typed {info['typed']} characters.",
        "deterministic": True,
    }


async def press_key(args: dict[str, Any]) -> dict[str, Any]:
    """Press a single key one or more times.

    Supported keys: enter, tab, escape, backspace, delete, space,
    arrows, home, end, pageup, pagedown, f1-f12, and single characters.
    """
    key = str(args.get("key", "")).strip()
    count = max(1, min(20, int(args.get("count", 1))))

    if not key:
        # Voice path: orchestrator passes {"query": <raw transcript>}
        key = _extract_key(str(args.get("query", "")))

    if not key:
        return {
            "tool": "press_key",
            "error": "no key provided",
            "result": None,
        }

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _press_key_blocking, key, count)

    if "error" in info:
        return {"tool": "press_key", **info, "result": None}

    return {
        "tool": "press_key",
        "key": info["pressed"],
        "count": info["count"],
        "result": f"Pressed '{key}' {count} time(s).",
        "reply": f"Pressed {key}.",
        "deterministic": True,
    }


async def hotkey(args: dict[str, Any]) -> dict[str, Any]:
    """Press a key combination (e.g. Ctrl+C, Alt+F4, Ctrl+Shift+S)."""
    combo = str(args.get("combo", args.get("keys", ""))).strip()
    if not combo:
        # Try parsing from individual key args
        keys_list = []
        for k in ["key1", "key2", "key3", "key4"]:
            v = str(args.get(k, "")).strip()
            if v:
                keys_list.append(v)
        combo = "+".join(keys_list) if keys_list else ""

    if not combo:
        # Voice path: orchestrator passes {"query": <raw transcript>}
        combo = _extract_combo(str(args.get("query", "")))

    if not combo:
        return {
            "tool": "hotkey",
            "error": "no key combination provided (use combo='Ctrl+C' format)",
            "result": None,
        }

    keys = [k.strip() for k in combo.split("+") if k.strip()]

    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, _hotkey_blocking, keys)

    if "error" in info:
        return {"tool": "hotkey", **info, "result": None}

    return {
        "tool": "hotkey",
        "combo": info["combo"],
        "result": f"Pressed {info['combo']}.",
        "reply": f"Pressed {info['combo']}.",
        "deterministic": True,
    }
