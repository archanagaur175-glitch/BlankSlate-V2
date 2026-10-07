import { invoke } from "@tauri-apps/api/core";

interface Props {
  daemonRunning: boolean;
  ollamaRunning: boolean;
  onCancel: () => void;
}

export default function QuitModal({ daemonRunning, ollamaRunning, onCancel }: Props) {
  const services = [
    daemonRunning ? "daemon" : null,
    ollamaRunning ? "Ollama" : null,
  ].filter(Boolean) as string[];
  return (
    <div
      data-testid="quit-modal"
      className="absolute inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
    >
      <div className="mx-2 w-full max-w-[210px] rounded-lg border border-white/10 bg-black/85 p-2 shadow-[0_0_20px_rgba(0,0,0,0.6)]">
        <div className="text-[10px] font-semibold text-white">Quit BlankSlate?</div>
        <div className="mt-0.5 text-[8px] text-white/60">
          {services.length > 0
            ? `Also stop: ${services.join(" + ")}`
            : "No services running"}
        </div>
        <div className="mt-1.5 flex gap-1">
          <button
            data-testid="quit-all"
            onClick={() => invoke("quit_app", { killAll: true })}
            className="flex-1 rounded border border-rose-400/40 bg-rose-500/15 px-1 py-[3px] text-[8px] text-rose-200 hover:bg-rose-500/25"
          >
            Quit all
          </button>
          <button
            data-testid="quit-hud-only"
            onClick={() => invoke("quit_app", { killAll: false })}
            className="flex-1 rounded border border-white/15 bg-white/5 px-1 py-[3px] text-[8px] text-white/80 hover:bg-white/10"
          >
            Quit HUD only
          </button>
          <button
            data-testid="quit-cancel"
            onClick={onCancel}
            className="flex-1 rounded border border-emerald-400/40 bg-emerald-500/15 px-1 py-[3px] text-[8px] text-emerald-200 hover:bg-emerald-500/25"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
