"""BlankSlate v2 daemon config — single source of truth.

Every module imports from here. No getattr(cfg, X, fallback) with
a different default anywhere else.
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DaemonConfig:
    ipc_host: str = "127.0.0.1"
    ipc_port: int = 8765

    ollama_host: str = "127.0.0.1"
    ollama_port: int = 11434
    ollama_model: str = "qwen2.5:0.5b"

    num_ctx: int = 2048
    chat_timeout: float = 120.0
    tool_timeout: float = 30.0
    ollama_retry_attempts: int = 3
    ollama_retry_base_delay: float = 1.0

    # Voice pipeline
    stt_model: str = "models/faster-whisper-medium.en"
    stt_device: str = "cuda"  # GPU with Ollama cuBLAS fallback
    stt_compute_type: str = "float16"  # float16 for GPU
    ptt_hotkey: str = "<ctrl>+<alt>+<space>"
    wake_words: tuple[str, ...] = ("hey blank", "blank slate", "hey assistant")
    mic_device: int | None = None
    mic_gain: float = 1.0            # 0.1–10.0, software amplification before VAD
    vad_aggressiveness: int = 1      # 0–3, 0 = most sensitive, 3 = least

    # Vocabulary priming — words/phrases to bias STT toward
    # Format: string passed as initial_prompt to faster-whisper
    # Helps with wake words, app names, and commonly misheard terms
    stt_vocabulary_prompt: str = (
        "BlankSlate, open notepad, Chrome, Cursor, Visual Studio Code, "
        "switch window, type text, list windows, get time, "
        "hey BlankSlate, Blank Slate assistant"
    )

    # Voice output (TTS)
    tts_enabled: bool = True
    tts_voice: str = "Microsoft David Desktop"
    tts_device: int | None = None

    # Guardrails / context
    persist_enabled: bool = True
    digest_enabled: bool = True
    digest_max_recent_turns: int = 6
    context_max_turns: int = 20

    # Tool orchestration (Phase 8)
    tools_enabled: bool = True
    planner_enabled: bool = False  # LLM planner is optional; keyword routing is the fast path
    router_embedding: bool = False  # extra LLM pass for non-keyword queries; off = low latency
    mcp_enabled: bool = False      # toggle MCP server adoption
    mcp_command: str = ""          # e.g. 'python -m mcp_server'
    mcp_idle_timeout_s: float = 60.0
    mcp_tool_prefix: str = "mcp."

    # System event & hardware monitor (Phase 2)
    monitor_poll_interval: float = 5.0      # seconds between CPU/GPU/battery samples
    monitor_cpu_threshold: float = 90.0     # % sustained over monitor_sustained_samples
    monitor_gpu_threshold: float = 90.0     # % sustained over monitor_sustained_samples
    monitor_battery_threshold: float = 20.0 # % — alert at or below
    monitor_sustained_samples: int = 3      # consecutive samples for CPU/GPU alerts

    # Encrypted vault (Phase 3)
    vault_ttl_days: float = 14.0            # TTL for conversation/notes/captures/RAG

    # Local task planner (Phase 1)
    reminder_interval: float = 30.0         # seconds between reminder sweeps (monitor pkg)

    # Proactive background event agents (Phase 2)
    watch_enabled: bool = True              # file-watch triggers on the broadcast channel
    watch_debounce_s: float = 0.5           # one alert per file per this many seconds

    # Timeline (Phase 4) — opt-in screen indexing, OFF at boot (design doc §1)
    timeline_enabled: bool = False
    timeline_interval_s: float = 3.0        # capture tick cadence while a session runs
    timeline_ttl_hours: float = 24.0        # row TTL (security doc §6)
    timeline_hamming_threshold: int = 8     # pHash distance below this = same frame
    timeline_exclude_processes: tuple[str, ...] = ()   # checked BEFORE capture (§2)
    timeline_exclude_titles: tuple[str, ...] = ()

    # Proactive diagnostics (six-feature expansion, Phase 2)
    diagnostics_enabled: bool = True          # crash watcher + anomaly -> LLM diagnosis
    diagnosis_cooldown_s: float = 60.0        # per-image-path dedup between proposals
    diagnosis_llm_timeout_s: float = 30.0     # hard cap on the diagnosis LLM call

    # Voice-stress adaptation (six-feature expansion, Phase 3)
    # Advisory brevity suffix on rushed VOICE turns only; never persisted.
    stress_adaptation_enabled: bool = True

    # Webcam gesture layer (six-feature expansion, Phase 4)
    # Off by default: explicit opt-in via config or set_gesture_mode IPC.
    # Frames are RAM-only; no file I/O anywhere in blankslate/gesture/.
    gesture_enabled: bool = False

    # Full-duplex streaming voice (six-feature expansion, Phase 5)
    # Additive mode switch: "ptt" (baseline, DEFAULT — existing pipeline
    # byte-identical) | "streaming" (continuous mic, partial transcription,
    # barge-in). PTT is the approved baseline; streaming is opt-in.
    voice_mode: str = "ptt"
    streaming_partial_interval_s: float = 0.6  # incremental STT cadence while a segment is open
    streaming_barge_in_ms: int = 250           # sustained speech required to interrupt TTS
    streaming_barge_min_level: float = 0.25    # echo-guard frame level (rms/4096) while TTS speaks

    # Sandbox code execution (Phase 6) — opt-in; the confirm flow (§5) is an
    # additional safeguard, never a substitute for this default-off rule.
    sandbox_enabled: bool = False
    sandbox_timeout_s: float = 30.0
    sandbox_memory_mb: int = 512
    sandbox_ttl_days: float = 14.0          # scratch run-dir TTL (Q5 purge)
    sandbox_proposal_ttl_s: float = 120.0   # proposal TTL -> timed_out (Q4)

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}:{self.ollama_port}"

    @property
    def ipc_url(self) -> str:
        return f"ws://{self.ipc_host}:{self.ipc_port}"


def load_config() -> DaemonConfig:
    """Load config with optional environment variable overrides."""
    def env_bool(name: str, default: bool) -> bool:
        v = os.environ.get(name)
        if v is None:
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    return DaemonConfig(
        ipc_host=os.environ.get("BS_IPC_HOST", "127.0.0.1"),
        ipc_port=int(os.environ.get("BS_IPC_PORT", "8765")),
        ollama_host=os.environ.get("BS_OLLAMA_HOST", "127.0.0.1"),
        ollama_port=int(os.environ.get("BS_OLLAMA_PORT", "11434")),
        ollama_model=os.environ.get("BS_OLLAMA_MODEL", "qwen2.5:0.5b"),
        tools_enabled=env_bool("BS_TOOLS_ENABLED", True),
        planner_enabled=env_bool("BS_PLANNER_ENABLED", False),
        router_embedding=env_bool("BS_ROUTER_EMBEDDING", False),
        mcp_enabled=env_bool("BS_MCP_ENABLED", False),
        mcp_command=os.environ.get("BS_MCP_COMMAND", ""),
        mcp_idle_timeout_s=float(os.environ.get("BS_MCP_IDLE_TIMEOUT", "60")),
        mcp_tool_prefix=os.environ.get("BS_MCP_TOOL_PREFIX", "mcp."),
        tts_enabled=env_bool("BS_TTS_ENABLED", True),
        tts_voice=os.environ.get("BS_TTS_VOICE", "Microsoft David Desktop"),
        stt_model=os.environ.get("BS_STT_MODEL", "small.en"),
        stt_device=os.environ.get("BS_STT_DEVICE", "cuda"),
        stt_compute_type=os.environ.get("BS_STT_COMPUTE_TYPE", "float16"),
        stt_vocabulary_prompt=os.environ.get(
            "BS_STT_VOCAB_PROMPT",
            "BlankSlate, open notepad, Chrome, Cursor, Visual Studio Code, "
            "switch window, type text, list windows, get time, "
            "hey BlankSlate, Blank Slate assistant",
        ),
        mic_device=int(os.environ["BS_MIC_DEVICE"]) if os.environ.get("BS_MIC_DEVICE") else None,
        mic_gain=float(os.environ.get("BS_MIC_GAIN", "1.0")),
        vad_aggressiveness=int(os.environ.get("BS_VAD_AGGRESSIVENESS", "1")),
        monitor_poll_interval=float(os.environ.get("BS_MONITOR_POLL_INTERVAL", "5.0")),
        monitor_cpu_threshold=float(os.environ.get("BS_MONITOR_CPU_THRESHOLD", "90")),
        monitor_gpu_threshold=float(os.environ.get("BS_MONITOR_GPU_THRESHOLD", "90")),
        monitor_battery_threshold=float(os.environ.get("BS_MONITOR_BATTERY_THRESHOLD", "20")),
        monitor_sustained_samples=int(os.environ.get("BS_MONITOR_SUSTAINED_SAMPLES", "3")),
        vault_ttl_days=float(os.environ.get("BS_VAULT_TTL_DAYS", "14")),
        reminder_interval=float(os.environ.get("BS_REMINDER_INTERVAL", "30")),
        watch_enabled=env_bool("BS_WATCH_ENABLED", True),
        watch_debounce_s=float(os.environ.get("BS_WATCH_DEBOUNCE", "0.5")),
        timeline_enabled=env_bool("BS_TIMELINE_ENABLED", False),
        timeline_interval_s=float(os.environ.get("BS_TIMELINE_INTERVAL", "3.0")),
        timeline_ttl_hours=float(os.environ.get("BS_TIMELINE_TTL_HOURS", "24.0")),
        timeline_hamming_threshold=int(os.environ.get("BS_TIMELINE_HAMMING", "8")),
        timeline_exclude_processes=tuple(
            p.strip() for p in os.environ.get("BS_TIMELINE_EXCLUDE_PROC", "").split(";")
            if p.strip()
        ),
        timeline_exclude_titles=tuple(
            t.strip() for t in os.environ.get("BS_TIMELINE_EXCLUDE_TITLE", "").split(";")
            if t.strip()
        ),
        diagnostics_enabled=env_bool("BS_DIAGNOSTICS_ENABLED", True),
        diagnosis_cooldown_s=float(os.environ.get("BS_DIAGNOSIS_COOLDOWN", "60")),
        diagnosis_llm_timeout_s=float(os.environ.get("BS_DIAGNOSIS_LLM_TIMEOUT", "30")),
        stress_adaptation_enabled=env_bool("BS_STRESS_ADAPTATION_ENABLED", True),
        gesture_enabled=env_bool("BS_GESTURE_ENABLED", False),
        voice_mode=os.environ.get("BS_VOICE_MODE", "ptt"),
        streaming_partial_interval_s=float(
            os.environ.get("BS_STREAMING_PARTIAL_INTERVAL", "0.6")
        ),
        streaming_barge_in_ms=int(os.environ.get("BS_STREAMING_BARGE_IN_MS", "250")),
        streaming_barge_min_level=float(
            os.environ.get("BS_STREAMING_BARGE_LEVEL", "0.25")
        ),
        sandbox_enabled=env_bool("BS_SANDBOX_ENABLED", False),
        sandbox_timeout_s=float(os.environ.get("BS_SANDBOX_TIMEOUT", "30")),
        sandbox_memory_mb=int(os.environ.get("BS_SANDBOX_MEMORY_MB", "512")),
        sandbox_ttl_days=float(os.environ.get("BS_SANDBOX_TTL_DAYS", "14")),
        sandbox_proposal_ttl_s=float(
            os.environ.get("BS_SANDBOX_PROPOSAL_TTL", "120")
        ),
    )
