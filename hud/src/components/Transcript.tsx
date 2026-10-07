import { useEffect, useRef } from "react";
import type { TranscriptEntry } from "../types";

interface Props {
  entries: TranscriptEntry[];
}

export default function Transcript({ entries }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries.length]);

  return (
    <div className="flex-1 overflow-y-auto px-4 py-3 flex flex-col gap-3 min-h-0">
      {entries.length === 0 && (
        <div className="flex items-center justify-center h-full text-zinc-600 text-sm tracking-widest uppercase">
          Awaiting input…
        </div>
      )}
      {entries.map((e) =>
        e.role === "user" ? (
          <UserBubble key={e.id} entry={e} />
        ) : (
          <AssistantCard key={e.id} entry={e} />
        ),
      )}
      <div ref={endRef} />
    </div>
  );
}

function UserBubble({ entry }: { entry: TranscriptEntry }) {
  return (
    <div className="self-end max-w-[80%]">
      <div className="text-[9px] tracking-widest uppercase text-zinc-500 mb-1 text-right">
        You
      </div>
      <div className="px-3 py-2 rounded-2xl rounded-br-sm bg-emerald-950/40 border border-emerald-400/30 text-zinc-100 text-sm shadow-[0_0_14px_rgba(52,211,153,0.10)]">
        {entry.text}
      </div>
    </div>
  );
}

function AssistantCard({ entry }: { entry: TranscriptEntry }) {
  return (
    <div className="self-start max-w-[85%]">
      <div className="text-[9px] tracking-widest uppercase text-zinc-500 mb-1">
        BlankSlate{entry.intent ? ` · ${entry.intent}` : ""}
      </div>
      <div className="px-3 py-2 rounded-2xl rounded-bl-sm bg-cyan-950/20 border border-cyan-400/25 text-zinc-100 text-sm whitespace-pre-wrap shadow-[0_0_14px_rgba(34,211,238,0.08)]">
        {entry.text}
      </div>
      {entry.tools && entry.tools.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-1.5">
          {entry.tools.map((t) => (
            <span
              key={t}
              className="text-[9px] px-1.5 py-0.5 rounded bg-violet-500/10 border border-violet-400/25 text-violet-300"
            >
              ⚙ {t}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
