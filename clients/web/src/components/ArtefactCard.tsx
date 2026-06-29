import type { Artefact } from "../lib/queries";
import { mediaUrl } from "../lib/queries";

const STATE_COLOR: Record<string, string> = {
  draft: "bg-slate-100 text-slate-700",
  in_review: "bg-amber-100 text-amber-800",
  approved: "bg-emerald-100 text-emerald-800",
  archived: "bg-slate-200 text-slate-500",
};

const RUN_STATUS_COLOR: Record<string, string> = {
  pass: "bg-emerald-100 text-emerald-800",
  fail: "bg-red-100 text-red-800",
  error: "bg-red-100 text-red-800",
  timeout: "bg-amber-100 text-amber-800",
};

function ExecutionMedia({ payload }: { payload: Record<string, unknown> }) {
  const screenshots = (payload.screenshots as string[] | undefined) ?? [];
  const consoleLog = payload.console_log_url as string | undefined;
  const status = String(payload.status ?? "");
  const errMsg = payload.error_message as string | undefined;
  const sClass = RUN_STATUS_COLOR[status] ?? "bg-slate-100 text-slate-700";

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className={"px-2 py-0.5 rounded uppercase tracking-wider " + sClass}>{status || "—"}</span>
        {payload.duration_ms !== undefined && (
          <span className="text-slate-500">{Math.round((payload.duration_ms as number) / 100) / 10}s</span>
        )}
        {payload.mode !== undefined && <span className="text-slate-500">mode: {String(payload.mode)}</span>}
      </div>
      {errMsg && (
        <pre className="text-xs bg-red-50 border border-red-100 rounded p-2 overflow-auto max-h-32 text-red-700 whitespace-pre-wrap">
          {errMsg}
        </pre>
      )}
      {screenshots.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          {screenshots.map((s, i) => {
            const url = mediaUrl(s);
            if (!url) return null;
            return (
              <a key={i} href={url} target="_blank" rel="noopener noreferrer">
                <img
                  src={url}
                  alt={`screenshot ${i + 1}`}
                  className="rounded border border-slate-200 hover:border-slate-400 transition"
                  loading="lazy"
                />
              </a>
            );
          })}
        </div>
      )}
      {consoleLog && (
        <div className="text-xs">
          <a
            href={mediaUrl(consoleLog) ?? "#"}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-700 hover:underline"
          >
            console.log →
          </a>
        </div>
      )}
    </div>
  );
}

export default function ArtefactCard({
  artefact,
  actions,
}: {
  artefact: Artefact;
  actions?: React.ReactNode;
}) {
  const stateClass = STATE_COLOR[artefact.state] ?? "bg-slate-100 text-slate-700";
  const isExecution =
    artefact.type === "execution_result" &&
    (artefact.payload as Record<string, unknown>).mode === "playwright_sandbox";
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
      {isExecution && <ExecutionMedia payload={artefact.payload as Record<string, unknown>} />}
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
