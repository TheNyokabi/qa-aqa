import { useState } from "react";
import { useTransition } from "../lib/queries";
import { useAuth } from "../lib/auth";

const TRANSITIONS: Record<string, string[]> = {
  draft: ["in_review", "archived"],
  in_review: ["approved", "draft", "archived"],
  approved: ["archived"],
  archived: [],
};

export default function StateTransitionMenu({
  artefactId,
  currentState,
}: {
  artefactId: string;
  currentState: string;
}) {
  const { user } = useAuth();
  const trans = useTransition(artefactId);
  const [err, setErr] = useState<string | null>(null);
  const canTransition = user?.role === "reviewer" || user?.role === "admin";
  const targets = TRANSITIONS[currentState] ?? [];

  if (!canTransition) {
    return <span className="text-xs text-slate-400">view only</span>;
  }
  if (targets.length === 0) {
    return <span className="text-xs text-slate-400">terminal</span>;
  }

  async function onPick(to: string) {
    setErr(null);
    try {
      await trans.mutateAsync(to);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "transition failed");
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap items-center gap-1">
        {targets.map((t) => (
          <button
            key={t}
            disabled={trans.isPending}
            onClick={() => onPick(t)}
            className="text-xs border border-slate-300 rounded px-2 py-1 hover:bg-slate-100 disabled:opacity-50"
          >
            → {t}
          </button>
        ))}
      </div>
      {err && <div className="text-xs text-red-600 max-w-xs truncate">{err}</div>}
    </div>
  );
}
