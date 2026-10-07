// Shared types for the HUD.

export interface DaemonStatus {
  connected: boolean;
  daemon_url: string;
}

/** Timeline session indicator (Phase 4 security design §4; §8 audio). */
export interface TimelineState {
  active: boolean;
  started_at: number | null;
  frames: number;
  audio: boolean;
  transcripts: number;
}

export interface DaemonState {
  status: string;
  started_at: number;
  subscribers: number;
  context_turns: number;
  persisted_records: number;
  timeline?: TimelineState;
}

/** A single transcript entry (user query or assistant reply). */
export interface TranscriptEntry {
  id: string;
  role: "user" | "assistant";
  text: string;
  intent?: string;
  tools?: string[];
  ts: number;
}

/** Diagnosis lifecycle (Phase 2): awaiting -> executed | dismissed. */
export type DiagnosisStatus = "awaiting_confirmation" | "executed" | "dismissed";

/** One proactive diagnosis proposal from the daemon. */
export interface Diagnosis {
  id: string;
  anomaly: Record<string, unknown>;
  summary: string;
  cause: string;
  proposal: string;
  action: string;
  action_params: Record<string, unknown>;
  status: DiagnosisStatus;
  relaunched_pid?: number;
}

/** Sandbox proposal lifecycle (Phase 6b §5): executed | dismissed | timed_out. */
export type SandboxStatus = "executed" | "dismissed" | "timed_out";

/** One pending sandbox execution proposal from the daemon. */
export interface SandboxProposal {
  id: string;
  script: string;
  purpose: string;
  limits: { timeout_s: number; memory_mb: number };
  expires_at: number;
}

/** Raw daemon→HUD bridge payloads. */
export type DaemonEvent =
  | { type: "pong" }
  | { type: "state"; status: string; started_at: number; subscribers: number; context_turns: number; persisted_records: number; timeline?: TimelineState }
  | { type: "text_reply"; text: string; intent: string }
  | { type: "assistant_response"; text: string; intent: string }
  | { type: "transcription"; text: string }
  | { type: "audio_level"; level: number }
  | { type: "mode_change"; online: boolean }
  | { type: "overlay"; action: "show" | "hide"; x?: number; y?: number; w?: number; h?: number; text?: string; target_title?: string }
  | { type: "monitor_alert"; category: "diagnosis"; id: string; anomaly: Record<string, unknown>; summary: string; cause: string; proposal: string; action: string; action_params: Record<string, unknown>; status: DiagnosisStatus; relaunched_pid?: number }
  | { type: "monitor_alert"; category: "cpu" | "gpu" | "battery"; value?: number; threshold?: number; samples?: number; percent?: number; ts?: number }
  | { type: "camera_state"; active: boolean }
  | { type: "gesture_event"; gesture: string; action: string | null }
  | { type: "transcription_partial"; text: string; final: boolean }
  | { type: "tts_state"; speaking: boolean; interrupted: boolean }
  | { type: "sandbox_proposal"; id: string; script: string; purpose: string; limits: { timeout_s: number; memory_mb: number }; expires_at: number }
  | { type: "sandbox_result"; id: string; status: SandboxStatus; purpose?: string; exit_code?: number; timed_out?: boolean; reason?: string; duration_s?: number; stdout?: string; stderr?: string; scratch?: string }
  | { type: "error"; detail: string };
