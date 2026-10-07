// Sandbox proposal card (Phase 6b) — full-view only: shows the proposed
// script in a read-only code viewer with Confirm (sandbox_confirm -> the
// daemon's single spawn point executes it) and Dismiss (sandbox_dismiss ->
// closed, nothing runs). The compact view gets a one-line pill in App.tsx.
import type { SandboxProposal } from "../types";

interface Props {
  proposal: SandboxProposal;
  sendRaw: (msg: Record<string, unknown>) => void;
}

export default function SandboxCard({ proposal, sendRaw }: Props) {
  const secs = Math.max(0, Math.round(proposal.expires_at - Date.now() / 1000));
  return (
    <div
      data-testid="sandbox-card"
      className="border-t border-violet-400/30 bg-violet-500/10 backdrop-blur-sm px-4 py-2 text-xs"
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-semibold text-violet-300 truncate">
            {proposal.purpose || "Script ready"}
          </div>
          <div className="text-white/60 truncate">
            timeout {proposal.limits.timeout_s}s | memory {proposal.limits.memory_mb} MB | expires in {secs}s
          </div>
        </div>
        <div className="flex shrink-0 gap-1.5 mt-0.5">
          <button
            data-testid="sandbox-confirm"
            className="rounded border border-emerald-400/40 bg-emerald-500/15 px-2 py-1 text-[10px] font-medium text-emerald-300 hover:bg-emerald-500/30"
            onClick={() => sendRaw({ type: "sandbox_confirm", id: proposal.id })}
          >
            Confirm
          </button>
          <button
            data-testid="sandbox-dismiss"
            className="rounded border border-rose-400/40 bg-rose-500/15 px-2 py-1 text-[10px] font-medium text-rose-300 hover:bg-rose-500/30"
            onClick={() => sendRaw({ type: "sandbox_dismiss", id: proposal.id })}
          >
            Dismiss
          </button>
        </div>
      </div>
      <pre
        data-testid="sandbox-script"
        className="mt-1 max-h-24 overflow-auto rounded border border-white/10 bg-black/50 px-2 py-1 text-[10px] text-white/70 whitespace-pre-wrap break-all"
      >
        {proposal.script}
      </pre>
    </div>
  );
}
