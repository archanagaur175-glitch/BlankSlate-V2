import { useEffect, useRef, useState } from "react";
import CameraIndicator from "./CameraIndicator";
import type { DaemonStatus, DaemonState } from "../types";

interface Props {
  status: DaemonStatus;
  daemonState: DaemonState | null;
  level: number | null;
  live: boolean;
  lastCommand: string | null;
  cameraActive: boolean;
}

const MINI_BARS = 12;

/**
 * Compact HUD widget — 250x100px, always visible in bottom-right corner.
 * Shows status dot, mini waveform, and last command.
 */
export default function CompactWidget({ status, daemonState, level, live, lastCommand, cameraActive }: Props) {
  const [bars, setBars] = useState<number[]>(() => Array(MINI_BARS).fill(0.06));
  const idx = useRef(0);

  useEffect(() => {
    idx.current = 0;
    const id = setInterval(() => {
      const frame = new Array<number>(MINI_BARS);
      for (let i = 0; i < MINI_BARS; i++) {
        let v: number;
        if (live && level !== null) {
          const wave = Math.abs(Math.sin(i * 0.5 + idx.current * 0.2));
          v = 0.1 + level * 0.9 * (0.4 + 0.6 * wave);
        } else {
          const wave = Math.abs(Math.sin(i * 0.4 + idx.current * 0.15));
          v = 0.05 + wave * 0.1;
        }
        frame[i] = Math.min(1, Math.max(0.02, v));
      }
      setBars(frame);
      idx.current += 1;
    }, 60);
    return () => clearInterval(id);
  }, [live, level]);

  const connected = status.connected;

  return (
    <div className="h-full w-full flex flex-col justify-between p-2 select-none">
      <div className="flex items-center gap-1.5">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full border px-1.5 py-[1px] ${
            connected
              ? "border-emerald-400/30 bg-emerald-500/10"
              : "border-rose-400/30 bg-rose-500/10"
          }`}
        >
          <span
            className={`inline-block w-1.5 h-1.5 rounded-full ${
              connected
                ? "bg-emerald-400 shadow-[0_0_6px_#34d399] animate-pulse"
                : "bg-rose-500 shadow-[0_0_6px_#f43f5e]"
            }`}
          />
          <span
            className={`text-[10px] tracking-widest uppercase ${
              connected ? "text-emerald-300" : "text-rose-300"
            }`}
          >
            {connected ? "Online" : "Offline"}
          </span>
        </span>
        <CameraIndicator active={cameraActive} />
        {daemonState?.timeline?.active && (
          <span
            data-testid="timeline-rec-indicator"
            className="inline-flex items-center rounded border border-rose-400/40 bg-rose-500/15 px-1 text-[9px] font-bold tracking-widest text-rose-300 animate-pulse"
            title={
              daemonState?.timeline?.audio
                ? "Timeline recording active (mic)"
                : "Timeline recording active"
            }
          >
            REC
            {daemonState?.timeline?.audio && (
              <span data-testid="timeline-audio-indicator" className="ml-1 font-normal text-cyan-300">
                · MIC
              </span>
            )}
          </span>
        )}
        {daemonState && (
          <span className="ml-auto text-[9px] tracking-wider uppercase text-zinc-500 tabular-nums">
            {daemonState.context_turns} turns
          </span>
        )}
      </div>

      <div className="flex items-end justify-center gap-[2px] h-5">
        {bars.map((v, i) => (
          <div
            key={i}
            className="w-[3px] rounded-full bg-gradient-to-t from-emerald-400/80 to-cyan-400/90 drop-shadow-[0_0_3px_rgba(34,211,238,0.45)] transition-[height] duration-100"
            style={{ height: `${Math.round(v * 100)}%`, opacity: 0.4 + 0.6 * v }}
          />
        ))}
      </div>

      {lastCommand && (
        <div className="truncate rounded-r border-l-2 border-cyan-400/60 bg-white/5 px-1.5 text-[9px] text-zinc-400">
          {lastCommand}
        </div>
      )}
    </div>
  );
}
