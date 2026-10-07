import { useState, type FormEvent } from "react";

interface Props {
  onSend: (text: string) => void;
  disabled: boolean;
}

export default function Composer({ onSend, disabled }: Props) {
  const [value, setValue] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue("");
  };

  return (
    <form onSubmit={submit} onMouseDown={(e) => e.stopPropagation()} className="px-4 pb-4 pt-2 flex items-center gap-2 border-t border-white/10">
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={disabled ? "daemon offline…" : "Ask something…"}
        disabled={disabled}
        className="flex-1 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-cyan-400/60 focus:shadow-[0_0_0_1px_rgba(34,211,238,0.25)] transition-colors disabled:opacity-50"
        autoFocus
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="px-4 py-2 rounded-lg bg-cyan-400/90 text-black text-sm font-semibold hover:bg-cyan-300 disabled:opacity-30 disabled:cursor-not-allowed transition-all shadow-[0_0_10px_rgba(34,211,238,0.35)]"
      >
        Send
      </button>
    </form>
  );
}
