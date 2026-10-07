interface Props {
  online: boolean;
  sendRaw: (msg: Record<string, unknown>) => void;
}

export default function ModeToggle({ online, sendRaw }: Props) {
  const toggle = () => {
    sendRaw({ type: "set_online_mode", enabled: !online });
  };

  return (
    <button
      onClick={toggle}
      className={`flex items-center gap-1.5 px-2 py-1 text-[10px] tracking-widest uppercase rounded border transition-all ${
        online
          ? "text-emerald-300 border-emerald-400/40 bg-emerald-500/15 hover:bg-emerald-500/25 shadow-[0_0_8px_rgba(52,211,153,0.12)]"
          : "text-amber-300 border-amber-400/40 bg-amber-500/15 hover:bg-amber-500/25"
      }`}
    >
      <span
        className={`inline-block w-1.5 h-1.5 rounded-full ${
          online ? "bg-emerald-400 shadow-[0_0_4px_#34d399]" : "bg-amber-400 shadow-[0_0_4px_#fbbf24]"
        }`}
      />
      {online ? "Online" : "Offline"}
    </button>
  );
}
