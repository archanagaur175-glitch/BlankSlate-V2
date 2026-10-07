import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebviewWindow } from "@tauri-apps/api/webviewWindow";
import StatusBar from "./components/StatusBar";
import Transcript from "./components/Transcript";
import Waveform from "./components/Waveform";
import Composer from "./components/Composer";
import SensitivityControls from "./components/SensitivityControls";
import CompactWidget from "./components/CompactWidget";
import ModeToggle from "./components/ModeToggle";
import OverlayWindow from "./components/OverlayWindow";
import DiagnosisCard from "./components/DiagnosisCard";
import SandboxCard from "./components/SandboxCard";
import QuitModal from "./components/QuitModal";
import { useDaemon } from "./hooks/useDaemon";
import type { DaemonEvent, DaemonState, DaemonStatus, Diagnosis, SandboxProposal, TranscriptEntry } from "./types";

let idSeq = 0;
const nextId = () => `t${Date.now()}-${idSeq++}`;

const COMPACT = { width: 250, height: 100 };
const FULL = { width: 350, height: 700 };

// The `overlay` webview runs the SAME bundle but renders only the floating
// pill (six-feature expansion, Phase 1) — it never touches main-window
// setup, drag, or daemon UI state.
const IS_OVERLAY = (() => {
  try {
    return getCurrentWebviewWindow().label === "overlay";
  } catch {
    return false;
  }
})();

export default function App() {
  const [daemonStatus, setDaemonStatus] = useState<DaemonStatus>({
    connected: false,
    daemon_url: "",
  });
  const [daemonState, setDaemonState] = useState<DaemonState | null>(null);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [level, setLevel] = useState<number | null>(null);
  const [live, setLive] = useState(false);
  const [liveTranscript, setLiveTranscript] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [lastCommand, setLastCommand] = useState<string | null>(null);
  const [onlineMode, setOnlineMode] = useState(false);
  // Proactive diagnoses (Phase 2): awaiting proposals render as a one-line
  // pill (compact) and a Confirm/Dismiss card (full); executed/dismissed
  // transitions remove the entry.
  const [diagnoses, setDiagnoses] = useState<Diagnosis[]>([]);
  // Sandbox proposals (Phase 6b §5): awaiting proposals render as a one-line
  // pill (compact) and a code-viewer Confirm/Dismiss card (full); any
  // sandbox_result (executed/dismissed/timed_out) removes the entry.
  const [sandboxProposals, setSandboxProposals] = useState<SandboxProposal[]>([]);
  // Gesture camera indicator (Phase 4): driven by camera_state broadcasts;
  // rendered by BOTH CompactWidget and StatusBar via CameraIndicator.
  const [cameraActive, setCameraActive] = useState(false);
  // Full-duplex streaming voice (Phase 5): live partial transcription +
  // TTS speaking/interrupted state, rendered in BOTH compact and full views.
  const [streamingPartial, setStreamingPartial] = useState<string | null>(null);
  const [ttsSpeaking, setTtsSpeaking] = useState(false);
  const [ttsInterrupted, setTtsInterrupted] = useState(false);
  const [quitPrompt, setQuitPrompt] = useState<{
    daemon_running: boolean;
    ollama_running: boolean;
  } | null>(null);
  useEffect(() => {
    if (IS_OVERLAY) return;
    let unlisten: (() => void) | undefined;
    listen<{ daemon_running: boolean; ollama_running: boolean }>(
      "close_requested",
      (e) => setQuitPrompt(e.payload),
    )
      .then((fn) => {
        unlisten = fn;
      })
      .catch(() => {});
    return () => {
      unlisten?.();
    };
  }, []);
  // Set tool window + position on mount (main HUD window only — the
  // overlay window is positioned solely by the daemon's overlay messages).
  useEffect(() => {
    if (IS_OVERLAY) return;
    invoke("set_tool_window").catch(() => {});
    invoke("position_window", {
      width: COMPACT.width,
      height: COMPACT.height,
      anchor: "bottom_right",
    }).catch(() => {});
  }, []);

  const resizeWindow = useCallback(async (toFull: boolean) => {
    const size = toFull ? FULL : COMPACT;
    await invoke("position_window", {
      width: size.width,
      height: size.height,
      anchor: "bottom_right",
    });
  }, []);

  useEffect(() => {
    if (IS_OVERLAY) return;
    const unlisten = listen("toggle-expand", () => {
      setExpanded((prev) => {
        const next = !prev;
        resizeWindow(next);
        return next;
      });
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, [resizeWindow]);

  const startDrag = useCallback(async (e: React.MouseEvent) => {
    e.preventDefault();
    await invoke("start_window_drag");
  }, []);

  const handleEvent = useCallback((evt: DaemonEvent) => {
    switch (evt.type) {
      case "pong":
        break;
      case "state":
        setDaemonState({
          status: evt.status,
          started_at: evt.started_at,
          subscribers: evt.subscribers,
          context_turns: evt.context_turns,
          persisted_records: evt.persisted_records,
          timeline: evt.timeline,
        });
        break;
      case "text_reply":
        addAssistant(evt.text, evt.intent);
        break;
      case "assistant_response":
        addAssistant(evt.text, evt.intent);
        break;
      case "audio_level":
        setLevel(Math.min(1, Math.max(0, evt.level)));
        setLive(true);
        break;
      case "transcription":
        setLiveTranscript(evt.text);
        setStreamingPartial(null);
        if (evt.text) setLastCommand(evt.text);
        break;
      case "transcription_partial":
        // Phase 5: incremental (final:false) text while a segment is open.
        setStreamingPartial(evt.text);
        break;
      case "tts_state":
        // Phase 5: speaking window + barge-in interruption flag.
        setTtsSpeaking(evt.speaking);
        setTtsInterrupted(!evt.speaking && evt.interrupted);
        break;
      case "mode_change":
        setOnlineMode(evt.online);
        break;
      case "monitor_alert":
        // Phase 2: only category=diagnosis is handled; cpu/gpu/battery
        // alerts are observational and intentionally dropped here.
        if (evt.category === "diagnosis") {
          const incoming: Diagnosis = {
            id: evt.id,
            anomaly: evt.anomaly,
            summary: evt.summary,
            cause: evt.cause,
            proposal: evt.proposal,
            action: evt.action,
            action_params: evt.action_params,
            status: evt.status,
            relaunched_pid: evt.relaunched_pid,
          };
          setDiagnoses((prev) => {
            const rest = prev.filter((d) => d.id !== incoming.id);
            return incoming.status === "awaiting_confirmation"
              ? [...rest, incoming]
              : rest;
          });
        }
        break;
      case "camera_state":
        setCameraActive(evt.active);
        break;
      case "sandbox_proposal":
        setSandboxProposals((prev) =>
          prev.some((p) => p.id === evt.id)
            ? prev
            : [...prev, {
                id: evt.id,
                script: evt.script,
                purpose: evt.purpose,
                limits: evt.limits,
                expires_at: evt.expires_at,
              }],
        );
        break;
      case "sandbox_result":
        setSandboxProposals((prev) => prev.filter((p) => p.id !== evt.id));
        if (
          evt.status === "executed" &&
          (evt.timed_out || (evt.exit_code !== undefined && evt.exit_code !== 0))
        ) {
          addAssistant(
            `⚠️ Sandbox ${evt.reason ?? evt.status} (exit ${evt.exit_code ?? "?"})`,
            "sandbox",
          );
        }
        break;
      case "gesture_event":
        // Observational: the daemon performs the mapped action itself
        // (wave -> overlay hide); nothing to render here yet.
        break;
      case "error":
        addAssistant(`⚠️ ${evt.detail}`, "error");
        break;
      default:
        break;
    }
  }, []);

  const addAssistant = useCallback((text: string, intent?: string) => {
    const entry: TranscriptEntry = {
      id: nextId(),
      role: "assistant",
      text,
      intent,
      ts: Date.now(),
    };
    setEntries((prev) => [...prev, entry]);
  }, []);

  const { connected, send, sendRaw } = useDaemon(handleEvent, setDaemonStatus);

  if (IS_OVERLAY) {
    return <OverlayWindow />;
  }

  if (!expanded) {
    const awaiting = diagnoses.filter((d) => d.status === "awaiting_confirmation");
    return (
      <div
        className="relative h-screen w-screen flex flex-col hud-root rounded-xl overflow-hidden"
        onMouseDown={startDrag}
      >
        {/* Phase 2 compact pill: one line, latest pending diagnosis. */}
        {awaiting.length > 0 && (
          <div
            data-testid="diagnosis-pill"
            className="absolute inset-x-0 top-0 z-10 truncate border-b border-amber-400/30 bg-amber-500/15 px-2 py-[2px] text-[9px] text-amber-200 backdrop-blur-sm"
          >
            {awaiting[awaiting.length - 1].summary || "Diagnosis ready"}
          </div>
        )}
        {/* Phase 6b compact: one line, latest pending sandbox proposal. */}
        {sandboxProposals.length > 0 && (
          <div
            data-testid="sandbox-pill"
            className="absolute inset-x-0 top-4 z-10 truncate border-b border-violet-400/30 bg-violet-500/15 px-2 py-[2px] text-[9px] text-violet-200 backdrop-blur-sm"
          >
            {sandboxProposals[sandboxProposals.length - 1].purpose || "Script ready"}
          </div>
        )}
        {/* Phase 5 compact: live partial transcription + TTS state. */}
        {streamingPartial && (
          <div
            data-testid="partial-pill"
            className="absolute inset-x-0 bottom-0 z-10 truncate bg-cyan-500/10 px-2 py-[2px] text-[9px] text-cyan-200/90 italic"
          >
            {streamingPartial}
          </div>
        )}
        {(ttsSpeaking || ttsInterrupted) && (
          <div
            data-testid="tts-pill"
            className={`absolute right-1 top-1 z-10 rounded px-1 text-[8px] ${
              ttsSpeaking ? "bg-emerald-500/20 text-emerald-200" : "bg-rose-500/20 text-rose-200"
            }`}
          >
            {ttsSpeaking ? "speaking" : "interrupted"}
          </div>
        )}
        <CompactWidget
          status={daemonStatus}
          daemonState={daemonState}
          level={level}
          live={live}
          lastCommand={lastCommand}
          cameraActive={cameraActive}
        />
        {quitPrompt && (
          <QuitModal
            daemonRunning={quitPrompt.daemon_running}
            ollamaRunning={quitPrompt.ollama_running}
            onCancel={() => setQuitPrompt(null)}
          />
        )}
      </div>
    );
  }

  const awaitingFull = diagnoses.filter((d) => d.status === "awaiting_confirmation");
  return (
    <div
      className="h-screen w-screen flex flex-col hud-root"
      onMouseDown={startDrag}
    >
      <StatusBar status={daemonStatus} daemonState={daemonState} cameraActive={cameraActive} />
      <Transcript entries={entries} />
      {/* Phase 2 full view: suggestion cards with Confirm/Dismiss. */}
      {awaitingFull.map((d) => (
        <DiagnosisCard key={d.id} diagnosis={d} sendRaw={sendRaw} />
      ))}
      {/* Phase 6b full view: sandbox proposal cards with Confirm/Dismiss. */}
      {sandboxProposals.map((p) => (
        <SandboxCard key={p.id} proposal={p} sendRaw={sendRaw} />
      ))}
      {liveTranscript && (
        <div className="px-4 py-2 text-sm text-cyan-300/80 italic border-t border-white/5">
          {liveTranscript}
        </div>
      )}
      {/* Phase 5 full view: partial line + TTS state chip. */}
      {streamingPartial && (
        <div
          data-testid="partial-line"
          className="px-4 py-1 text-xs text-cyan-200/60 italic border-t border-white/5 truncate"
        >
          {streamingPartial}
        </div>
      )}
      {(ttsSpeaking || ttsInterrupted) && (
        <div
          data-testid="tts-state"
          className={`px-4 py-1 text-[10px] border-t border-white/5 ${
            ttsSpeaking ? "text-emerald-300/90" : "text-rose-300/90"
          }`}
        >
          {ttsSpeaking ? "speaking" : "interrupted"}
        </div>
      )}
      <Waveform level={level} live={live} />
      <div
        className="flex items-center justify-center border-t border-white/5"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <ModeToggle online={onlineMode} sendRaw={sendRaw} />
        <SensitivityControls sendRaw={sendRaw} />
      </div>
      <Composer
        onSend={(text) => {
          const entry: TranscriptEntry = {
            id: nextId(),
            role: "user",
            text,
            ts: Date.now(),
          };
          setEntries((prev) => [...prev, entry]);
          send(text);
        }}
        disabled={!connected}
      />
      {quitPrompt && (
        <QuitModal
          daemonRunning={quitPrompt.daemon_running}
          ollamaRunning={quitPrompt.ollama_running}
          onCancel={() => setQuitPrompt(null)}
        />
      )}
    </div>
  );
}
