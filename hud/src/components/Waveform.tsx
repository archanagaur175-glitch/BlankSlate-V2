import { useEffect, useRef, useState } from "react";

interface Props {
  /** Current audio level 0..1 from the daemon (frosting; may be absent). */
  level: number | null;
  /** Whether the daemon reported a live (non-idle) level. */
  live: boolean;
}

const BAR_COUNT = 48;

/**
 * Audio-reactive waveform. When the daemon supplies a real level we drive the
 * bars from it; otherwise we animate a gentle idle shimmer so the HUD stays
 * visually alive even before any mic data flows.
 */
export default function Waveform({ level, live }: Props) {
  const [bars, setBars] = useState<number[]>(() => Array(BAR_COUNT).fill(0.06));
  const idx = useRef(0);

  useEffect(() => {
    idx.current = 0;
    const id = setInterval(() => {
      const frame = new Array<number>(BAR_COUNT);
      for (let i = 0; i < BAR_COUNT; i++) {
        let v: number;
        if (live && level !== null) {
          // Smooth envelope across the visual width, seeded by real level.
          const wave = Math.abs(Math.sin(i * 0.35 + idx.current * 0.18));
          v = 0.08 + level * 0.9 * (0.4 + 0.6 * wave);
        } else {
          // Idle shimmer.
          const wave = Math.abs(Math.sin(i * 0.3 + idx.current * 0.12));
          v = 0.05 + wave * 0.12;
        }
        frame[i] = Math.min(1, Math.max(0.02, v));
      }
      setBars(frame);
      idx.current += 1;
    }, 60);
    return () => clearInterval(id);
  }, [live, level]);

  return (
    <div className="flex items-end justify-center gap-[3px] h-14 px-4">
      {bars.map((v, i) => (
        <div
          key={i}
          className="w-[4px] rounded-full bg-gradient-to-t from-emerald-400/80 to-cyan-400/90 drop-shadow-[0_0_3px_rgba(34,211,238,0.45)] transition-[height] duration-100"
          style={{ height: `${Math.round(v * 100)}%`, opacity: 0.4 + 0.6 * v }}
        />
      ))}
    </div>
  );
}
