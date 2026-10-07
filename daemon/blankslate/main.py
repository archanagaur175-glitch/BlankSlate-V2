"""Main daemon orchestration — importable by run_daemon.py and __main__.py."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path

from blankslate.config import DaemonConfig, load_config
from blankslate.context.manager import ContextManager
from blankslate.guardrail.digest import ContextDigester
from blankslate.guardrail.store import ContextStore
from blankslate.ipc.server import IPCState, broadcast, start_ipc_server
from blankslate.llm.ollama_client import OllamaClient
from blankslate.monitor.system import SystemMonitor
from blankslate.voice.pipeline import VoicePipeline

logger = logging.getLogger("blankslate")


class _TTSSingleton:
    """Lazily-constructed, process-wide TTS instance (probe once per boot)."""

    _instance: "TTS | None" = None
    _voice: str | None = None
    # Phase 5: streaming branch sets this so every constructed TTS reports
    # speaking/interrupted transitions (tts_state broadcast). PTT mode
    # leaves it None — zero wire delta vs the approved baseline.
    on_state_hook = None

    @classmethod
    def get(cls, cfg: DaemonConfig) -> "TTS | None":
        if cls._instance is None or cls._voice != cfg.tts_voice:
            from blankslate.voice.tts import TTS

            cls._instance = TTS(device=cfg.tts_device, on_state=cls.on_state_hook)
            cls._voice = cfg.tts_voice
        return cls._instance

    @classmethod
    def peek(cls) -> "TTS | None":
        """Existing instance or None — never constructs (safe to poll)."""
        return cls._instance


def _ipc_auth_enabled() -> bool:
    """IPC auth on for the frozen production daemon; BS_IPC_AUTH=1/0 overrides."""
    env = os.environ.get("BS_IPC_AUTH")
    if env is not None:
        return env == "1"
    return bool(getattr(sys, "frozen", False))


def _issue_ipc_token() -> str:
    """Per-session token persisted at daemon/data/ipc_token for the HUD bridge."""
    token = secrets.token_urlsafe(32)
    path = Path(__file__).resolve().parent.parent / "data" / "ipc_token"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
    except OSError:
        logger.exception("IPC token file not written; HUD cannot authenticate")
    return token


async def _run(cfg: DaemonConfig) -> None:
    # Amendment A: startup recovery runs FIRST, before any other work.
    from blankslate.rag.store import default_db_path
    from blankslate.vault.core import recover_rag_db

    rec = recover_rag_db(default_db_path())
    logger.info("vault recovery: lock=%s db=%s", rec["lock"], rec["db"])

    llm = OllamaClient(cfg)
    ctx = ContextManager(cfg, max_turns=cfg.context_max_turns)

    # Task planner (Phase 1): wire the local LLM into NL extraction and open
    # the sealed task store (created at startup so IPC-up implies it exists).
    from blankslate.tasks import extract as task_extract
    from blankslate.tasks.store import get_store, close_store as close_task_store

    task_extract.set_llm(llm)
    task_store = get_store()

    # Guardrail layer: persistent redacted store + context digester
    store = ContextStore(cfg) if cfg.persist_enabled else None
    digester = ContextDigester(cfg, llm) if (cfg.digest_enabled and store) else None

    # Tool orchestration layer (Phase 8)
    from blankslate.tools.registry import ToolRegistry
    from blankslate.tools.planner import plan_task

    registry = ToolRegistry()
    mcp_clients: dict = {}
    planner = (lambda q: plan_task(q, llm, registry)) if cfg.planner_enabled else None

    if cfg.mcp_enabled and cfg.mcp_command:
        try:
            from blankslate.mcp.client import MCPClient, MCPSessionConfig
            from blankslate.tools.registry import ToolSpec

            mcp = MCPClient(
                MCPSessionConfig(
                    name="mcp-default",
                    command=[c for c in cfg.mcp_command.split() if c],
                    idle_timeout_s=cfg.mcp_idle_timeout_s,
                    tool_prefix=cfg.mcp_tool_prefix,
                )
            )
            mcp_clients["mcp-default"] = mcp
            mcp_tools = await mcp.list_tools()
            for t in mcp_tools:
                registry.register(
                    ToolSpec(
                        name=t["name"],  # already prefixed with 'mcp.'
                        description=t["description"],
                        keywords=(),
                    )
                )
            logger.info("MCP: exposing %d tools", len(mcp_tools))
        except Exception:
            logger.exception("MCP setup failed — continuing without MCP")

    state = IPCState(
        ctx=ctx, llm=llm, store=store, digester=digester,
        cfg=cfg, registry=registry, mcp_clients=mcp_clients, planner=planner,
    )
    state.started_at = time.time()
    state.status = "running"

    # Floating overlay pill (six-feature expansion, Phase 1): the overlay
    # module broadcasts through this IPC state (show/hide commands).
    from blankslate import overlay as overlay_control
    overlay_control.bind(state)

    # Voice-stress adaptation (Phase 3): latest finalized VAD speech chunk.
    # The subscriber runs BEFORE STT (the pipeline fans chunks out first),
    # so once on_voice_transcription fires these frames belong to THIS
    # utterance. Frames are used in RAM only — never persisted anywhere.
    _stress_chunk: dict[str, object] = {"pcm": None, "ts": 0.0}

    def _capture_stress_chunk(audio_bytes: bytes) -> None:
        # Mic-thread callback: store only — estimation runs on dispatch.
        _stress_chunk["pcm"] = audio_bytes
        _stress_chunk["ts"] = time.time()

    async def on_voice_transcription(text: str) -> None:
        """Handle transcribed voice input through the same text pipeline."""
        from blankslate.intent.judge import handle_text_input
        from blankslate.voice import stress as stress_mod

        # Per-utterance stress reading (voice path only, ~60 s freshness so
        # a stale chunk can never be attributed to this turn). Failure here
        # must never block the turn — fall back to no-stress.
        stress_reading: dict[str, object] | None = None
        pcm = _stress_chunk["pcm"]
        ts = float(_stress_chunk["ts"] or 0.0)
        if isinstance(pcm, bytes) and pcm and (time.time() - ts) <= 60.0:
            try:
                stress_reading = stress_mod.compute_from_pcm(pcm, text)
            except Exception:
                logger.exception("stress estimation failed — turn continues without stress")

        if stress_reading is not None:
            # Wire metadata (advisory only; HUD drops unknown types today).
            await broadcast(state, {
                "type": "transcription_stress",
                "text": text,
                "urgency": stress_reading["urgency"],
                "band": stress_reading["band"],
                "features": stress_reading["features"],
            })

        result = await handle_text_input(
            text,
            ctx,
            llm,
            store=store,
            digester=digester,
            persist_enabled=bool(store),
            cfg=cfg,
            registry=registry,
            mcp_clients=mcp_clients,
            planner=planner,
            stress=stress_reading,
            stress_enabled=bool(state.stress_adaptation_enabled),
        )
        await broadcast(state, {
            "type": "assistant_response",
            "text": result.reply,
            "intent": result.intent,
            "tools": list(result.tools),
            "source": "voice",
        })
        # Speak the reply in a thread so TTS never blocks the IPC loop.
        if cfg.tts_enabled:
            tts = _TTSSingleton.get(cfg)
            if tts and tts.is_ready:
                threading.Thread(
                    target=tts.speak,
                    args=(result.reply,),
                    daemon=True,
                    name="tts-speak",
                ).start()

    # Full-duplex streaming voice (six-feature expansion, Phase 5): additive
    # mode switch. Default voice_mode="ptt" -> the existing VoicePipeline,
    # unchanged; "streaming" -> the full-duplex pipeline (continuous mic,
    # partial transcription, barge-in). Both expose the same duck-typed
    # surface (subscribers / session mic / inject hook) used below.
    if cfg.voice_mode == "streaming":
        from blankslate.voice.streaming import StreamingVoicePipeline

        voice = StreamingVoicePipeline(
            cfg, on_transcription=on_voice_transcription, ipc_state=state,
        )
        logger.info("voice mode: streaming (full-duplex StreamingVoicePipeline)")
    else:
        voice = VoicePipeline(cfg, on_transcription=on_voice_transcription, ipc_state=state)
        logger.info("voice mode: ptt (baseline VoicePipeline)")
    # Phase 3: stress capture rides the existing chunk-subscriber fan-out
    # (timeline session audio uses the same mechanism — additive, no edits
    # to the pipeline itself).
    voice.register_chunk_subscriber(_capture_stress_chunk)
    state.voice = voice

    logger.info(
        "\u2728 BlankSlate daemon started \u2014 IPC on %s:%d, model=%s",
        cfg.ipc_host,
        cfg.ipc_port,
        cfg.ollama_model,
    )

    loop = asyncio.get_running_loop()

    # Phase 5 streaming wiring: tts_state broadcast + barge-in control. The
    # hook fires from the speak thread; broadcasting is scheduled thread-safe
    # onto the event loop. Set BEFORE any utterance can reach the TTS.
    if cfg.voice_mode == "streaming":
        def _tts_state_hook(speaking: bool, interrupted: bool) -> None:
            voice.notify_tts_state(speaking, interrupted)
            asyncio.run_coroutine_threadsafe(
                broadcast(state, {
                    "type": "tts_state",
                    "speaking": speaking,
                    "interrupted": interrupted,
                }),
                loop,
            )

        _TTSSingleton.on_state_hook = _tts_state_hook
        voice.set_tts_control(_TTSSingleton.peek)  # type: ignore[union-attr]

    # Start IPC first (fast), then voice in background (STT model load is slow)
    async def start_voice_background() -> None:
        """Start voice pipeline in background — IPC is already listening."""
        try:
            await voice.start(loop)
        except Exception:
            logger.exception("Voice pipeline failed to start")

    asyncio.create_task(start_voice_background())

    # Proactive diagnostics (six-feature expansion, Phase 2): crash watcher +
    # anomaly triage -> LLM proposal -> propose-and-confirm over the same
    # broadcast channel. Execution happens only on diagnose_confirm.
    from blankslate.monitor.diagnostics import DiagnosticsService

    diagnostics = DiagnosticsService(cfg, emit=lambda m: broadcast(state, m), llm=llm)
    state.diagnostics = diagnostics
    await diagnostics.start()

    monitor = SystemMonitor(
        cfg,
        emit=lambda msg: broadcast(state, msg),
        on_alert=diagnostics.on_anomaly,
    )
    await monitor.start()

    # File-watch event agents (Phase 2): same broadcast channel, never raises.
    from blankslate.monitor.watcher import FileWatcher, set_watcher

    watcher = FileWatcher(cfg, emit=lambda msg: broadcast(state, msg))
    await watcher.start()
    set_watcher(watcher)

    # Timeline sessions (Phase 4): opt-in capture; indicator rides state pushes.
    from blankslate.timeline.service import TimelineService, set_service

    timeline = TimelineService(cfg)
    timeline.bind_ipc(state)
    timeline.bind_voice(voice)
    state.timeline = timeline.state
    set_service(timeline)

    # Webcam gesture layer (six-feature expansion, Phase 4): the service is
    # always constructed (IPC handlers + the on-connect camera_state push
    # need it), but the camera only opens on explicit opt-in — config at
    # boot, or set_gesture_mode at runtime. Frames stay in RAM (no file I/O
    # in blankslate/gesture/ — gate source-asserted).
    from blankslate.gesture.service import GestureService

    gesture = GestureService(cfg, emit=lambda m: broadcast(state, m))
    state.gesture = gesture
    if cfg.gesture_enabled:
        await gesture.enable()

    # Vault auto-purge (Phase 3): periodic TTL sweep.
    from blankslate.vault.purge import purge_loop

    purge_task = asyncio.create_task(purge_loop(cfg, store))

    # Reminder sweeper (Phase 1): rides the monitor package's broadcast path.
    from blankslate.monitor.reminders import reminder_loop

    reminder_task = asyncio.create_task(
        reminder_loop(cfg, task_store, emit=lambda msg: broadcast(state, msg))
    )

    ipc_token = _issue_ipc_token()
    try:
        await start_ipc_server(
            cfg, state, token=ipc_token if _ipc_auth_enabled() else ""
        )
    finally:
        # Explicit per-task cancels: hw3's structural source check asserts
        # the literal `purge_task.cancel()`; behavior is per-task either way.
        purge_task.cancel()
        reminder_task.cancel()
        for tsk in (purge_task, reminder_task):
            try:
                await tsk
            except asyncio.CancelledError:
                pass
        await monitor.stop()
        await diagnostics.stop()
        await watcher.stop()
        set_watcher(None)
        await gesture.shutdown()
        await timeline.close()
        voice.stop()
        from blankslate.rag.service import close_store

        close_store()
        close_task_store()
        await llm.close()


def main() -> None:
    try:
        import sounddevice  # noqa: F401
    except Exception:
        pass
    cfg = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        logger.info("Daemon shutting down.")
