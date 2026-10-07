"""WebSocket IPC server.

Protocol: JSON messages over WebSocket.
  Client → Daemon:
    {"type": "ping"}
    {"type": "get_state"}
    {"type": "send_text_message", "text": "..."}
    {"type": "overlay_show", "target_title": "...", "text": "..."}
    {"type": "overlay_hide"}
    {"type": "set_gesture_mode", "enabled": true|false}
    {"type": "inject_gesture", "gesture": "wave"}   (BS_GATE_HOOKS=1 only)
    {"type": "diagnose_confirm", "id": "diag-..."}
    {"type": "diagnose_dismiss", "id": "diag-..."}
  Daemon → Client:
    {"type": "pong"}
    {"type": "state", ...}
    {"type": "text_reply", "text": "...", "intent": "..."}
    {"type": "overlay", "action": "show"|"hide", "x", "y", "w", "h", "text"}
    {"type": "camera_state", "active": true|false}
    {"type": "gesture_event", "gesture": "wave"|"palm", "action": str|null}
    {"type": "ack", "setting": "diagnosis", "id": "...", "status": "executed"|"dismissed"}
    {"type": "monitor_alert", "category": "diagnosis", "id", "anomaly", "summary",
     "cause", "proposal", "action", "action_params",
     "status": "awaiting_confirmation"|"executed"|"dismissed"}
    {"type": "error", "detail": "..."}

  Connection authentication — GENERAL hardening, NOT sandbox-specific:
  Until now ANY local process could open a WebSocket to the loopback
  control plane and issue commands (send_text_message, overlay, ...)
  with no credential of any kind. start_ipc_server() now accepts a
  per-session `token` (issued at daemon startup, persisted for the HUD
  at daemon/data/ipc_token — inside the user profile, so the profile
  DACL plus the sandbox account's deny-ACE keep it from the sandbox);
  when set, the handshake must present it as `?token=<value>` or it is
  rejected HTTP 401 before any message is processed. Enforcement is
  production-only (frozen daemon or BS_IPC_AUTH=1) because the existing
  gate suites spawn tokenless dev daemons — see blankslate/main.py.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
import websockets.server
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from blankslate.config import DaemonConfig
from blankslate.context.manager import ContextManager
from blankslate.llm.ollama_client import OllamaClient

logger = logging.getLogger("blankslate.ipc")


class IPCState:
    """Shared daemon state exposed over IPC."""

    def __init__(
        self,
        ctx: ContextManager,
        llm: OllamaClient,
        store: Any = None,
        digester: Any = None,
        cfg: Any = None,
        registry: Any = None,
        mcp_clients: Any = None,
        planner: Any = None,
    ) -> None:
        self.status: str = "starting"
        self.started_at: float = 0.0
        self.subscribers: set[websockets.ServerConnection] = set()
        self.ctx = ctx
        self.llm = llm
        self.store = store
        self.digester = digester
        self.cfg = cfg
        self.registry = registry
        self.mcp_clients = mcp_clients
        self.planner = planner
        self.voice: Any = None  # set after VoicePipeline init
        self.online_mode: bool = False  # offline by default
        # Voice-stress adaptation (Phase 3): runtime toggle for the advisory
        # brevity suffix (set_stress_adaptation). Initialized from config so
        # BS_STRESS_ADAPTATION_ENABLED is honored at boot.
        self.stress_adaptation_enabled: bool = (
            bool(getattr(cfg, "stress_adaptation_enabled", True))
            if cfg is not None else True
        )
        # Proactive diagnostics (Phase 2): DiagnosticsService set in main.py.
        # diagnose_confirm/dismiss execute ONLY through this service.
        self.diagnostics: Any = None
        # Webcam gesture layer (Phase 4): GestureService set in main.py.
        # set_gesture_mode / inject_gesture route through this service.
        self.gesture: Any = None
        # Timeline indicator state (Phase 4): service mutates this dict in
        # place and pushes a fresh "state" message on start/stop. audio/
        # transcripts (Phase 5): mic contribution + sealed transcript count.
        self.timeline: dict[str, Any] = {
            "active": False,
            "started_at": None,
            "frames": 0,
            "audio": False,
            "transcripts": 0,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "subscribers": len(self.subscribers),
            "context_turns": self.ctx.turn_count,
            "persisted_records": self.store.count if self.store else 0,
            "timeline": self.timeline,
        }


async def _handle_client(
    ws: websockets.ServerConnection,
    state: IPCState,
) -> None:
    """Handle a single IPC client connection."""
    from blankslate.intent.judge import handle_text_input
    from blankslate.sandbox.registry import get_registry
    from blankslate.sandbox.runner import execute as sandbox_execute

    state.subscribers.add(ws)
    remote = ws.remote_address
    logger.info("IPC client connected: %s", remote)
    # Gesture camera indicator (Phase 4): push the CURRENT camera state so
    # a client that connects after boot-enabled sessions still lights the
    # HUD indicator (subscribers were previously state-on-get_state only).
    if state.gesture is not None:
        await ws.send(json.dumps({
            "type": "camera_state",
            "active": bool(getattr(state.gesture, "active", False)),
        }))
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "detail": "invalid JSON"}))
                continue

            msg_type = msg.get("type", "")

            if msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))

            elif msg_type == "get_state":
                await ws.send(json.dumps({"type": "state", **state.to_dict()}))

            elif msg_type == "send_text_message":
                text = msg.get("text", "")
                try:
                    result = await handle_text_input(
                        text,
                        state.ctx,
                        state.llm,
                        store=state.store,
                        digester=state.digester,
                        persist_enabled=bool(state.store),
                        cfg=state.cfg,
                        registry=state.registry,
                        mcp_clients=state.mcp_clients,
                        planner=state.planner,
                    )
                    reply_msg = {
                        "type": "text_reply",
                        "text": result.reply,
                        "intent": result.intent,
                        "tools": list(result.tools),
                    }
                    await ws.send(json.dumps(reply_msg))
                    await broadcast(state, {
                        "type": "assistant_response",
                        "text": result.reply,
                        "intent": result.intent,
                        "tools": list(result.tools),
                    }, exclude=ws)
                except Exception as exc:
                    logger.exception("send_text_message failed")
                    await ws.send(json.dumps({
                        "type": "error",
                        "detail": f"text processing failed: {exc}",
                    }))

            elif msg_type == "set_mic_gain":
                gain = float(msg.get("gain", 1.0))
                if state.voice:
                    state.voice.update_mic_gain(gain)
                await ws.send(json.dumps({"type": "ack", "setting": "mic_gain", "value": gain}))

            elif msg_type == "set_vad_aggressiveness":
                vad = int(msg.get("level", 1))
                if state.voice:
                    state.voice.update_vad_aggressiveness(vad)
                await ws.send(json.dumps({"type": "ack", "setting": "vad_aggressiveness", "value": vad}))

            elif msg_type == "set_stress_adaptation":
                # Voice-stress adaptation (Phase 3): runtime toggle. The
                # advisory suffix is the ONLY thing this gates — stress
                # readings are still computed and broadcast either way.
                enabled = bool(msg.get("enabled", True))
                state.stress_adaptation_enabled = enabled
                logger.info(
                    "stress adaptation %s (runtime toggle)",
                    "enabled" if enabled else "disabled",
                )
                await ws.send(json.dumps({
                    "type": "ack", "setting": "stress_adaptation",
                    "enabled": enabled,
                }))

            elif msg_type == "set_online_mode":
                enabled = bool(msg.get("enabled", False))
                state.online_mode = enabled
                from blankslate.tools.handlers import set_online_mode
                set_online_mode(enabled)
                mode_label = "online" if enabled else "offline"
                logger.info("Mode changed to: %s", mode_label)
                await ws.send(json.dumps({"type": "mode_change", "online": enabled}))
                await broadcast(state, {"type": "mode_change", "online": enabled}, exclude=ws)

            elif msg_type in ("overlay_show", "overlay_hide"):
                # Floating overlay pill (six-feature expansion, Phase 1):
                # resolve target window -> broadcast coords to every HUD.
                from blankslate import overlay as overlay_mod

                try:
                    if msg_type == "overlay_show":
                        result = await overlay_mod.show_pill(
                            str(msg.get("target_title", "")),
                            str(msg.get("text", "")),
                        )
                    else:
                        result = await overlay_mod.hide_pill()
                except Exception as exc:
                    logger.exception("overlay command failed")
                    result = {"error": f"overlay failed: {exc}"}
                if "error" in result:
                    await ws.send(json.dumps({"type": "error", "detail": result["error"]}))
                else:
                    await ws.send(json.dumps({"type": "ack", "setting": "overlay", **result}))

            elif msg_type == "inject_speech_chunk":
                # TEST-ONLY hook (BS_GATE_HOOKS=1, set by test spawns only):
                # hands synthesized PCM to the voice pipeline at the exact
                # boundary mic.py uses after VAD. Refused otherwise.
                if os.environ.get("BS_GATE_HOOKS") != "1":
                    await ws.send(json.dumps({
                        "type": "error", "detail": "gate hooks disabled",
                    }))
                elif not state.voice:
                    await ws.send(json.dumps({
                        "type": "error", "detail": "voice pipeline not ready",
                    }))
                else:
                    try:
                        pcm = base64.b64decode(msg.get("pcm_b64", ""))
                        state.voice.inject_chunk_for_tests(pcm)
                        await ws.send(json.dumps({
                            "type": "ack", "setting": "inject_speech_chunk",
                            "bytes": len(pcm),
                        }))
                    except Exception as exc:
                        logger.exception("inject_speech_chunk failed")
                        await ws.send(json.dumps({
                            "type": "error", "detail": f"inject failed: {exc}",
                        }))

            elif msg_type == "set_gesture_mode":
                # Webcam gesture layer (Phase 4): runtime opt-in/out. The
                # camera opens/closes here; camera_state is broadcast by
                # the service either way.
                svc = state.gesture
                if svc is None:
                    await ws.send(json.dumps({
                        "type": "error", "detail": "gesture service unavailable",
                    }))
                else:
                    enabled = bool(msg.get("enabled", True))
                    try:
                        result = await svc.set_enabled(enabled)
                    except Exception as exc:
                        logger.exception("set_gesture_mode failed")
                        result = {"error": f"set_gesture_mode failed: {exc}"}
                    if "error" in result:
                        await ws.send(json.dumps({
                            "type": "error", "detail": result["error"],
                        }))
                    else:
                        await ws.send(json.dumps({
                            "type": "ack", "setting": "gesture_mode", **result,
                        }))

            elif msg_type == "inject_gesture":
                # TEST-ONLY hook (BS_GATE_HOOKS=1, set by test spawns only):
                # feed a named gesture through the SAME service code path
                # the camera detector uses (action mapping proven on the
                # wire without requiring a human wave on cue — the P4 gate
                # documents this deviation). Refused otherwise.
                if os.environ.get("BS_GATE_HOOKS") != "1":
                    await ws.send(json.dumps({
                        "type": "error", "detail": "gate hooks disabled",
                    }))
                elif state.gesture is None:
                    await ws.send(json.dumps({
                        "type": "error", "detail": "gesture service unavailable",
                    }))
                else:
                    name = str(msg.get("gesture", ""))
                    try:
                        result = await state.gesture.handle_gesture(name)
                    except Exception as exc:
                        logger.exception("inject_gesture failed")
                        result = {"error": f"inject_gesture failed: {exc}"}
                    if "error" in result:
                        await ws.send(json.dumps({
                            "type": "error", "detail": result["error"],
                        }))
                    else:
                        await ws.send(json.dumps({
                            "type": "ack", "setting": "inject_gesture",
                            **result,
                        }))

            elif msg_type in ("diagnose_confirm", "diagnose_dismiss"):
                # Proactive diagnostics (Phase 2): propose-and-confirm. This
                # handler is the ONLY execution path — nothing runs at
                # proposal time; illegal state transitions come back as errors.
                svc = state.diagnostics
                if svc is None:
                    await ws.send(json.dumps({
                        "type": "error", "detail": "diagnostics unavailable",
                    }))
                else:
                    did = str(msg.get("id", ""))
                    try:
                        if msg_type == "diagnose_confirm":
                            result = await svc.confirm(did)
                        else:
                            result = await svc.dismiss(did)
                    except Exception as exc:
                        logger.exception("%s failed", msg_type)
                        result = {"error": f"{msg_type} failed: {exc}"}
                    if "error" in result:
                        await ws.send(json.dumps({
                            "type": "error", "detail": result["error"],
                        }))
                    else:
                        await ws.send(json.dumps({
                            "type": "ack", "setting": "diagnosis", **result,
                        }))

            elif msg_type == "sandbox_propose":
                # Phase 6b: proposal broadcast only — nothing spawns here
                # (the confirm handler below is the single spawn point).
                cfg = state.cfg
                if cfg is None or not bool(getattr(cfg, "sandbox_enabled", False)):
                    await ws.send(json.dumps({
                        "type": "error",
                        "detail": "sandbox disabled (sandbox_enabled=False)",
                    }))
                elif not str(msg.get("script", "")).strip():
                    await ws.send(json.dumps({
                        "type": "error", "detail": "empty script",
                    }))
                else:
                    prop = get_registry().propose(
                        str(msg.get("script", "")),
                        str(msg.get("purpose", "")),
                        ttl_s=float(getattr(cfg, "sandbox_proposal_ttl_s", 120.0)),
                        limits={
                            "timeout_s": float(
                                getattr(cfg, "sandbox_timeout_s", 30.0)
                            ),
                            "memory_mb": int(
                                getattr(cfg, "sandbox_memory_mb", 512)
                            ),
                        },
                    )
                    await broadcast(state, {
                        "type": "sandbox_proposal",
                        "id": prop["id"],
                        "script": prop["script"],
                        "purpose": prop["purpose"],
                        "limits": prop["limits"],
                        "expires_at": prop["expires_at"],
                    })

            elif msg_type in ("sandbox_confirm", "sandbox_dismiss"):
                # Phase 6b state machine (§5): illegal transitions rejected;
                # sandbox_confirm is the ONLY path that spawns a process.
                reg = get_registry()
                pid = str(msg.get("id", ""))
                if msg_type == "sandbox_dismiss":
                    code, detail, prop = reg.dismiss(pid)
                    if prop is None:
                        if code == "timed_out":
                            await broadcast(state, {
                                "type": "sandbox_result",
                                "id": pid,
                                "status": "timed_out",
                            })
                        await ws.send(json.dumps(
                            {"type": "error", "detail": detail}
                        ))
                    else:
                        await broadcast(state, {
                            "type": "sandbox_result",
                            "id": pid,
                            "status": "dismissed",
                            "purpose": prop.get("purpose", ""),
                        })
                else:
                    code, detail, prop = reg.confirm(pid)
                    if prop is None:
                        if code == "timed_out":
                            await broadcast(state, {
                                "type": "sandbox_result",
                                "id": pid,
                                "status": "timed_out",
                            })
                        await ws.send(json.dumps(
                            {"type": "error", "detail": detail}
                        ))
                    else:
                        result = await asyncio.to_thread(
                            sandbox_execute, prop["script"], state.cfg, pid
                        )
                        if "error" in result:
                            reg.release(pid)
                            await ws.send(json.dumps({
                                "type": "error",
                                "detail": result["error"],
                            }))
                        else:
                            await broadcast(state, {
                                "type": "sandbox_result",
                                "id": pid,
                                **result,
                            })

            else:
                await ws.send(
                    json.dumps({"type": "error", "detail": f"unknown type: {msg_type}"})
                )
    except websockets.ConnectionClosed:
        pass
    finally:
        state.subscribers.discard(ws)
        logger.info("IPC client disconnected: %s", remote)


async def broadcast(
    state: IPCState,
    message: dict[str, Any],
    exclude: websockets.ServerConnection | None = None,
) -> None:
    """Send a message to all connected IPC subscribers, optionally excluding one."""
    payload = json.dumps(message)
    dead: list[websockets.ServerConnection] = []
    for ws in state.subscribers:
        if ws is exclude:
            continue
        try:
            await ws.send(payload)
        except websockets.ConnectionClosed:
            dead.append(ws)
    for ws in dead:
        state.subscribers.discard(ws)


def _token_guard(token: str):
    """Reject handshakes lacking `?token=` with HTTP 401 (see docstring)."""

    def guard(connection: Any, request: Request) -> Response | None:
        presented = parse_qs(urlsplit(request.path).query).get("token", [""])[0]
        if presented and hmac.compare_digest(presented, token):
            return None
        logger.warning(
            "IPC handshake rejected (missing/invalid token): %s",
            getattr(connection, "remote_address", "?"),
        )
        return Response(401, "Unauthorized", Headers([("Content-Length", "0")]))

    return guard


async def start_ipc_server(
    cfg: DaemonConfig, state: IPCState, token: str = ""
) -> None:
    """Start the WebSocket IPC server and run forever.

    Non-empty `token` enables handshake authentication (module docstring);
    empty = tokenless (dev/gate daemons — existing behavior).
    """
    stop = asyncio.Event()
    guard = _token_guard(token) if token else None

    async def handler(ws: websockets.ServerConnection) -> None:
        await _handle_client(ws, state)

    async with websockets.serve(
        handler,
        cfg.ipc_host,
        cfg.ipc_port,
        ping_interval=20,
        ping_timeout=20,
        process_request=guard,
    ):
        logger.info(
            "IPC server listening on ws://%s:%d%s",
            cfg.ipc_host,
            cfg.ipc_port,
            " (token auth ON)" if token else "",
        )
        await stop.wait()
