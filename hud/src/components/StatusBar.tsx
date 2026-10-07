import { useState } from "react";
import CameraIndicator from "./CameraIndicator";
import { getStoredTheme, setTheme, type Theme } from "../theme";
import type { DaemonState, DaemonStatus } from "../types";

interface Props {
  status: DaemonStatus;
  daemonState: DaemonState | null;
  cameraActive: boolean;
}

export default function StatusBar({ status, daemonState, cameraActive }: Props) {
  const connected = status.connected;
  const [theme, setThemeState] = useState<Theme>(() => getStoredTheme());
  const toggleTheme = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    setThemeState(next);
  };
  return (
    <div className="flex items-center justify-between px-4 py-2 border-b border-white/10 gap-3">
      <div
        className={`inline-flex items-center gap-2 rounded-full border px-2 py-0.5 ${
          connected
            ? "border-emerald-400/30 bg-emerald-500/10"
            : "border-rose-400/30 bg-rose-500/10"
        }`}
      >
        <span
          className={`inline-block w-2 h-2 rounded-full ${
            connected ? "bg-emerald-400 shadow-[0_0_8px_#34d399]" : "bg-rose-500 shadow-[0_0_8px_#f43f5e]"
          }`}
        />
        <span
          className={`text-[11px] tracking-widest uppercase ${
            connected ? "text-emerald-300" : "text-rose-300"
          }`}
        >
          {connected ? "Daemon · Online" : "Daemon · Offline"}
        </span>
      </div>

      <div className="flex items-center gap-3 text-[10px] tracking-widest uppercase text-zinc-500">
        <CameraIndicator active={cameraActive} />
        {daemonState?.timeline?.active && (
          <span
            data-testid="timeline-rec-indicator"
            className="px-2 py-0.5 rounded border border-rose-400/40 bg-rose-500/15 text-rose-300 animate-pulse"
          >
            <span className="text-rose-400">●</span> Timeline REC
            {daemonState?.timeline?.audio && (
              <span
                data-testid="timeline-audio-indicator"
                className="ml-1 text-cyan-300"
                title="Mic contributing to timeline"
              >
                · MIC
              </span>
            )}
          </span>
        )}
        {daemonState && (
          <>
            <StatusChip label="Model" value={daemonState.status} />
            <StatusChip label="Turns" value={daemonState.context_turns.toString()} />
            <StatusChip label="Persisted" value={daemonState.persisted_records.toString()} />
          </>
        )}
        <StatusChip label="Clients" value={daemonState ? daemonState.subscribers.toString() : "–"} />
        <button
          type="button"
          data-testid="theme-toggle"
          onClick={toggleTheme}
          title="Toggle dark / light theme"
          className="px-2 py-0.5 rounded bg-white/5 border border-white/10 hover:border-cyan-400/30 hover:bg-white/10 transition-colors cursor-pointer"
        >
          <span className="text-zinc-500">Theme</span>{" "}
          <span className="text-cyan-300">{theme}</span>
        </button>
      </div>
    </div>
  );
}

function StatusChip({ label, value }: { label: string; value: string }) {
  return (
    <span className="px-2 py-0.5 rounded bg-white/5 border border-white/10">
      <span className="text-zinc-500">{label}</span>{" "}
      <span className="text-cyan-300">{value}</span>
    </span>
  );
}
