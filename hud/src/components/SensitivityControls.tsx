import { useState } from "react";

interface Props {
  sendRaw: (msg: Record<string, unknown>) => void;
}

export default function SensitivityControls({ sendRaw }: Props) {
  const [gain, setGain] = useState(1.0);
  const [vad, setVad] = useState(1);
  const [expanded, setExpanded] = useState(false);

  const handleGain = (val: number) => {
    setGain(val);
    sendRaw({ type: "set_mic_gain", gain: val });
  };

  const handleVad = (val: number) => {
    setVad(val);
    sendRaw({ type: "set_vad_aggressiveness", level: val });
  };

  if (!expanded) {
    return (
      <button
        onClick={() => setExpanded(true)}
        className="px-3 py-1 text-[10px] tracking-widest uppercase text-zinc-400 hover:text-cyan-300 border border-white/10 rounded bg-white/5 hover:bg-white/10 hover:border-cyan-400/30 transition-colors"
      >
        Sensitivity
      </button>
    );
  }

  return (
    <div className="flex items-center gap-4 px-3 py-2 border border-white/10 rounded bg-white/5">
      <button
        onClick={() => setExpanded(false)}
        className="text-[10px] text-zinc-500 hover:text-zinc-300"
      >
        ✕
      </button>

      {/* Mic Gain slider */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] tracking-widest uppercase text-zinc-500 w-12">
          Gain
        </span>
        <input
          type="range"
          min={0.1}
          max={5.0}
          step={0.1}
          value={gain}
          onChange={(e) => handleGain(parseFloat(e.target.value))}
          className="w-20 h-1 accent-cyan-400 bg-zinc-700 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-cyan-400 [&::-webkit-slider-thumb]:appearance-none"
        />
        <span className="text-[10px] text-cyan-300 w-8 text-right">
          {gain.toFixed(1)}x
        </span>
      </div>

      {/* VAD Aggressiveness slider */}
      <div className="flex items-center gap-2">
        <span className="text-[10px] tracking-widest uppercase text-zinc-500 w-12">
          VAD
        </span>
        <input
          type="range"
          min={0}
          max={3}
          step={1}
          value={vad}
          onChange={(e) => handleVad(parseInt(e.target.value))}
          className="w-20 h-1 accent-emerald-400 bg-zinc-700 rounded-full appearance-none cursor-pointer [&::-webkit-slider-thumb]:w-3 [&::-webkit-slider-thumb]:h-3 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-emerald-400 [&::-webkit-slider-thumb]:appearance-none"
        />
        <span className="text-[10px] text-emerald-300 w-8 text-right">
          {["Min", "Low", "Mid", "High"][vad]}
        </span>
      </div>
    </div>
  );
}
