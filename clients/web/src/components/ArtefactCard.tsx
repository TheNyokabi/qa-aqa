import type { Artefact } from "../lib/queries";

const STATE_COLOR: Record<string, string> = {
  draft: "bg-slate-100 text-slate-700",
  in_review: "bg-amber-100 text-amber-800",
  approved: "bg-emerald-100 text-emerald-800",
  archived: "bg-slate-200 text-slate-500",
};

export default function ArtefactCard({
  artefact,
  actions,
}: {
  artefact: Artefact;
  actions?: React.ReactNode;
}) {
  const stateClass = STATE_COLOR[artefact.state] ?? "bg-slate-100 text-slate-700";
  return (
    <div className="bg-white rounded border border-slate-200 p-4 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="text-xs font-mono text-slate-500">{artefact.id}</div>
          <div className="text-sm text-slate-700">
            <span className="font-medium">{artefact.type}</span>
            <span className="text-slate-400 ml-2">v{artefact.version}</span>
            <span className={"ml-2 px-2 py-0.5 rounded text-xs " + stateClass}>{artefact.state}</span>
          </div>
        </div>
        {actions}
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-slate-500 hover:text-slate-700">payload</summary>
        <pre className="bg-slate-50 rounded p-2 mt-1 overflow-auto max-h-64 text-slate-700">
          {JSON.stringify(artefact.payload, null, 2)}
        </pre>
      </details>
      <div className="text-xs text-slate-500">
        actor: <span className="font-mono">{artefact.actor}</span>
      </div>
    </div>
  );
}
