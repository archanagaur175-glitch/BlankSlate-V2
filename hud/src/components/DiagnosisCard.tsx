// Diagnosis suggestion card (Phase 2) — full-view only: shows one proposal
// with Confirm (diagnose_confirm -> daemon executes) and Dismiss
// (diagnose_dismiss -> closed, nothing runs). The compact view gets a
// one-line pill rendered by App.tsx instead.
import type { Diagnosis } from "../types";

interface Props {
  diagnosis: Diagnosis;
  sendRaw: (msg: Record<string, unknown>) => void;
}

export default function DiagnosisCard({ diagnosis, sendRaw }: Props) {
  return (
    <div
      data-testid="diagnosis-card"
      className="border-t border-amber-400/30 bg-amber-500/10 backdrop-blur-sm px-4 py-2 text-xs"
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-semibold text-amber-300 truncate">
            {diagnosis.summary || "Diagnosis ready"}
          </div>
          <div className="text-white/60 truncate">Cause: {diagnosis.cause}</div>
          <div className="text-white/80 mt-0.5">{diagnosis.proposal}</div>
        </div>
        <div className="flex shrink-0 gap-1.5 mt-0.5">
          <button
            data-testid="diagnosis-confirm"
            className="rounded border border-emerald-400/40 bg-emerald-500/15 px-2 py-1 text-[10px] font-medium text-emerald-300 hover:bg-emerald-500/30"
            onClick={() => sendRaw({ type: "diagnose_confirm", id: diagnosis.id })}
          >
            Confirm
          </button>
          <button
            data-testid="diagnosis-dismiss"
            className="rounded border border-rose-400/40 bg-rose-500/15 px-2 py-1 text-[10px] font-medium text-rose-300 hover:bg-rose-500/30"
            onClick={() => sendRaw({ type: "diagnose_dismiss", id: diagnosis.id })}
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}
