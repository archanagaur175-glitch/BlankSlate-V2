"""Hardware & media control tools (Phase 1) — local COM/Win32 only.

Zero network: pycaw (WASAPI COM), win32api media keys. All handlers follow
the dispatcher contract — structured args in, structured dicts out, no
unhandled exceptions reaching the daemon.

Endpoint singletons: get_render_endpoint()/get_capture_endpoint() cache one
AudioDevice per flow, so the IAudioEndpointVolume interface is activated
exactly once per flow for the process lifetime. Phase 2 registers its
IAudioEndpointVolumeCallback on the render singleton returned here — no
second audio interface anywhere.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import win32api
import win32con
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, AudioDevice

logger = logging.getLogger("blankslate.hardware")

# action -> (win32 VK, APPCOMMAND id for gate verification)
MEDIA_ACTIONS: dict[str, tuple[int, int]] = {
    "play_pause": (win32con.VK_MEDIA_PLAY_PAUSE, 14),   # APPCOMMAND_MEDIA_PLAY_PAUSE
    "next": (win32con.VK_MEDIA_NEXT_TRACK, 11),         # APPCOMMAND_MEDIA_NEXT_TRACK
    "previous": (win32con.VK_MEDIA_PREV_TRACK, 12),     # APPCOMMAND_MEDIA_PREV_TRACK
}

# Microphone context: queries that are clearly about the CAPTURE endpoint.
_MIC_CTX_RE = re.compile(r"\b(mic|mics|microphone|microphones|input)\b")

_render: AudioDevice | None = None
_capture: AudioDevice | None = None


def get_render_endpoint() -> AudioDevice:
    """Default render (speakers) endpoint — cached, single activation."""
    global _render
    if _render is None:
        _render = AudioUtilities.GetSpeakers()
        logger.info("render endpoint: %s", _render.FriendlyName)
    return _render


def get_capture_endpoint() -> AudioDevice:
    """Default capture (microphone) endpoint — cached, single activation."""
    global _capture
    if _capture is None:
        _capture = AudioUtilities.CreateDevice(AudioUtilities.GetMicrophone())
        logger.info("capture endpoint: %s", _capture.FriendlyName)
    return _capture


def _flow_code(endpoint_id: str) -> int:
    """OS data-flow for an endpoint: 0 = eRender, 1 = eCapture."""
    return int(AudioUtilities.GetEndpointDataFlow(endpoint_id, 1))


def _err(tool: str, msg: str) -> dict[str, Any]:
    return {"tool": tool, "error": msg, "result": None, "deterministic": False}


def _silent_skip(tool: str, why: str) -> dict[str, Any]:
    """Not this tool's request: no result block, no reply part, not executed."""
    logger.info("%s skipped from query: %s", tool, why)
    return {"tool": tool, "deterministic": True, "reply": ""}


def _extract_volume_query(args: dict[str, Any]) -> dict[str, Any] | None:
    """NL fallback for set_volume — the judge/orchestrator (voice, HUD, IPC
    wire) only ever passes {'query': ...}; structured args always win.

    Returns parsed args ({percent|level|delta_percent|muted}), a
    {'skip': ...} marker when the query is clearly not about the speakers,
    or None when nothing is parseable (caller keeps the structured error).
    """
    if args.get("level") is not None or "percent" in args or args.get("muted") is not None:
        return None
    q = str(args.get("query", "")).lower()
    if _MIC_CTX_RE.search(q):
        return {"skip": "query is about the microphone, not speakers"}
    if re.search(r"\bunmute\b", q):
        return {"muted": False}
    if re.search(r"\bmute\b", q):
        return {"muted": True}
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)\b", q)
    if m:
        return {"percent": float(m.group(1))}
    if re.search(r"\blouder\b|\bturn\s+(?:it\s+)?up\b", q):
        return {"delta_percent": 10.0}
    if re.search(r"\bquieter\b|\bturn\s+(?:it\s+)?down\b", q):
        return {"delta_percent": -10.0}
    m = re.search(r"\b(?:to|at)\s+(\d+(?:\.\d+)?)\b", q) or re.search(
        r"\bvolume\s*(?:to\s+|at\s+)?(\d+(?:\.\d+)?)", q
    )
    if m:
        tok = m.group(1)
        # "0.5" reads as level 0-1; plain integers read as percent (0-100).
        return {"level": float(tok)} if "." in tok else {"percent": float(tok)}
    return None


async def set_volume(args: dict[str, Any]) -> dict[str, Any]:
    """Set master output volume. Args: {'level': 0.0-1.0} or {'percent': 0-100}
    (structured), else parsed from {'query': ...}; 'mute'/'unmute' queries
    toggle system audio, 'louder'/'quieter' step ±10pp."""
    try:
        level = args.get("level")
        if level is None and "percent" in args:
            try:
                level = float(args["percent"]) / 100.0
            except (TypeError, ValueError):
                return _err("set_volume", "'percent' must be a number 0-100")
        if level is None:
            ext = _extract_volume_query(args) or {}
            if "skip" in ext:
                return _silent_skip("set_volume", str(ext["skip"]))
            if "muted" in ext:
                vol = get_render_endpoint().EndpointVolume
                vol.SetMute(1 if ext["muted"] else 0, None)
                now = bool(vol.GetMute())
                label = "muted" if now else "unmuted"
                return {
                    "tool": "set_volume",
                    "muted": now,
                    "result": f"System audio {label}.",
                    "reply": f"System audio {label}.",
                    "deterministic": True,
                }
            if "delta_percent" in ext:
                vol = get_render_endpoint().EndpointVolume
                cur = vol.GetMasterVolumeLevelScalar()
                level = min(1.0, max(0.0, cur + ext["delta_percent"] / 100.0))
            elif "percent" in ext:
                level = ext["percent"] / 100.0
            elif "level" in ext:
                level = ext["level"]
        if level is None:
            return _err(
                "set_volume",
                "provide 'level' (0.0-1.0) or 'percent' (0-100)",
            )
        try:
            level = float(level)
        except (TypeError, ValueError):
            return _err("set_volume", "'level' must be a number 0.0-1.0")
        if not 0.0 <= level <= 1.0:
            return _err("set_volume", f"level {level} out of range 0.0-1.0")

        vol = get_render_endpoint().EndpointVolume
        previous = vol.GetMasterVolumeLevelScalar()
        vol.SetMasterVolumeLevelScalar(level, None)  # pguidEventContext=None
        readback = vol.GetMasterVolumeLevelScalar()
        pct = round(readback * 100)
        return {
            "tool": "set_volume",
            "level": readback,
            "previous": previous,
            "readback": readback,
            "result": f"Volume set to {pct}%.",
            "reply": f"Volume set to {pct}%.",
            "deterministic": True,
        }
    except Exception as exc:  # noqa: BLE001 — dispatcher contract
        return _err("set_volume", f"{type(exc).__name__}: {exc}")


async def mute_microphone(args: dict[str, Any]) -> dict[str, Any]:
    """Mute/unmute the CAPTURE endpoint.

    Args: {'muted': bool} or {'action': 'mute'|'unmute'|'toggle'}.
    Result carries readbacks for capture AND render, so callers can verify
    the speaker device was not touched.
    """
    try:
        capture = get_capture_endpoint()
        render = get_render_endpoint()

        muted = args.get("muted")
        if muted is None:
            action = str(args.get("action", "")).strip().lower()
            if action not in ("toggle", "mute", "unmute"):
                # NL path: judge/orchestrator only passes {'query': ...}.
                # Shared 'mute'/'unmute' keywords can route non-mic queries
                # here — skip silently so the speaker tool can answer them.
                q = str(args.get("query", "")).lower()
                if not _MIC_CTX_RE.search(q):
                    return _silent_skip(
                        "mute_microphone", "query is not about the microphone"
                    )
                if re.search(r"\btoggle\b", q):
                    action = "toggle"
                elif re.search(r"\bunmute\b", q):
                    action = "unmute"
                elif re.search(r"\bmute\b", q):
                    action = "mute"
            if action == "toggle":
                muted = not bool(capture.EndpointVolume.GetMute())
            elif action == "mute":
                muted = True
            elif action == "unmute":
                muted = False
            else:
                return _err(
                    "mute_microphone",
                    "provide 'muted' (bool) or 'action' (mute|unmute|toggle)",
                )
        muted = bool(muted)

        render_mute_before = bool(render.EndpointVolume.GetMute())
        render_vol_before = render.EndpointVolume.GetMasterVolumeLevelScalar()

        capture.EndpointVolume.SetMute(1 if muted else 0, None)  # pguidEventContext=None
        # Endpoints (esp. BT hands-free) can report the PRE-change value
        # immediately after SetMute; poll briefly so the readback reflects
        # the state that was actually applied.
        capture_readback = bool(capture.EndpointVolume.GetMute())
        settle_deadline = time.monotonic() + 0.5
        while capture_readback != muted and time.monotonic() < settle_deadline:
            time.sleep(0.05)
            capture_readback = bool(capture.EndpointVolume.GetMute())

        render_mute_after = bool(render.EndpointVolume.GetMute())
        render_vol_after = render.EndpointVolume.GetMasterVolumeLevelScalar()

        state = "muted" if capture_readback else "unmuted"
        return {
            "tool": "mute_microphone",
            "muted": capture_readback,
            "capture_mute_readback": capture_readback,
            "capture_device": capture.FriendlyName,
            "capture_flow": _flow_code(capture.id),
            "render_mute_unchanged": render_mute_after == render_mute_before,
            "render_volume_unchanged": abs(render_vol_after - render_vol_before) < 1e-9,
            "result": f"Microphone {state} ({capture.FriendlyName}).",
            "reply": f"Microphone {state}.",
            "deterministic": True,
        }
    except Exception as exc:  # noqa: BLE001 — dispatcher contract
        return _err("mute_microphone", f"{type(exc).__name__}: {exc}")


def _extract_media_action(args: dict[str, Any]) -> str:
    """Resolve action from structured arg first, then from a raw query."""
    action = str(args.get("action", "")).strip().lower().replace("-", "_")
    if action == "prev":
        action = "previous"
    if action in MEDIA_ACTIONS:
        return action
    query = str(args.get("query", "")).lower()
    if "next" in query:
        return "next"
    if "previous" in query or "prev" in query or "back" in query:
        return "previous"
    if "play" in query or "pause" in query:
        return "play_pause"
    return ""


async def media_control(args: dict[str, Any]) -> dict[str, Any]:
    """Send a media key (play/pause, next, previous) globally via win32api."""
    try:
        action = _extract_media_action(args)
        if not action:
            return _err(
                "media_control",
                "provide 'action' (play_pause|next|previous) "
                "or a query containing play/pause/next/previous",
            )
        vk, appcmd = MEDIA_ACTIONS[action]
        win32api.keybd_event(vk, 0, 0, 0)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        return {
            "tool": "media_control",
            "action": action,
            "vk": vk,
            "appcommand": appcmd,
            "result": f"Sent media key: {action}.",
            "reply": f"Sent media key: {action}.",
            "deterministic": True,
        }
    except Exception as exc:  # noqa: BLE001 — dispatcher contract
        return _err("media_control", f"{type(exc).__name__}: {exc}")
