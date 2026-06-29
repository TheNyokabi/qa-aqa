import { Link, useLocation } from "react-router-dom";
import { useWorkflowDetail, useWorkflowStatus } from "../lib/queries";
import ArtefactCard from "../components/ArtefactCard";
import StateTransitionMenu from "../components/StateTransitionMenu";

const STATUS_COLOR: Record<string, string> = {
  RUNNING: "bg-blue-100 text-blue-800",
  COMPLETED: "bg-emerald-100 text-emerald-800",
  FAILED: "bg-red-100 text-red-800",
  CANCELED: "bg-slate-100 text-slate-700",
  TERMINATED: "bg-slate-100 text-slate-700",
  TIMED_OUT: "bg-amber-100 text-amber-800",
  UNKNOWN: "bg-slate-100 text-slate-500",
};

export default function WorkflowRunPage() {
  const loc = useLocation();
  const id = decodeURIComponent(loc.pathname.replace(/^\/run\//, ""));
  const status = useWorkflowStatus(id);
  const detail = useWorkflowDetail(id);

  const st = status.data?.status ?? "UNKNOWN";
  const stClass = STATUS_COLOR[st] ?? "bg-slate-100";
  const testCases = detail.data?.artefacts_by_type.test_case ?? [];

  return (
    <div className="space-y-6">
      <div>
        <Link to="/designer" className="text-sm text-blue-700 hover:underline">
          ← Designer
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight mt-1 font-mono break-all">{id}</h1>
        <div className="mt-2 flex items-center gap-3 text-sm">
          <span className={"px-2 py-1 rounded text-xs uppercase tracking-wider " + stClass}>
            {st}
          </span>
          {status.data?.start_time && (
            <span className="text-slate-500">
              started {new Date(status.data.start_time).toLocaleString()}
            </span>
          )}
          {status.data?.close_time && (
            <span className="text-slate-500">
              · ended {new Date(status.data.close_time).toLocaleString()}
            </span>
          )}
        </div>
      </div>

      {st === "RUNNING" && (
        <div className="bg-white rounded border border-slate-200 p-4 text-sm text-slate-500">
          The agent is working. Status polls every few seconds.
        </div>
      )}

      {status.data?.result_error && (
        <div className="bg-red-50 border border-red-200 rounded p-4 text-sm text-red-700">
          {status.data.result_error}
        </div>
      )}

      {testCases.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-500">
            Generated test cases ({testCases.length})
          </h2>
          <div className="space-y-2">
            {testCases.map((a) => (
              <ArtefactCard
                key={a.id}
                artefact={a}
                actions={<StateTransitionMenu artefactId={a.id} currentState={a.state} />}
              />
            ))}
          </div>
        </section>
      )}

      {st === "COMPLETED" && testCases.length === 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded p-4 text-sm text-amber-800">
          Workflow completed but no test_case artefacts found yet. The list view may take a moment
          to update.
        </div>
      )}
    </div>
  );
}
