import { Link } from "react-router-dom";
import {
  useActiveRuns,
  useCancelRun,
  useWorkflows,
  type RunSummary,
} from "../lib/queries";

function ActiveRunsCard() {
  const { data, isLoading } = useActiveRuns();
  const cancel = useCancelRun();
  const runs: RunSummary[] = data?.runs ?? [];
  if (isLoading && runs.length === 0) return null;
  return (
    <section className="bg-white rounded border border-slate-200 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-600">
          Active runs
        </h2>
        <span className="text-xs text-slate-400">
          Refreshes every 3s
        </span>
      </div>
      {runs.length === 0 ? (
        <p className="text-sm text-slate-500">No active runs.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {runs.map((r) => (
            <li
              key={r.run_id}
              className="flex items-center justify-between gap-3 py-2 text-sm"
            >
              <div className="flex min-w-0 items-center gap-3">
                <code
                  className="font-mono text-xs text-slate-700"
                  title={r.run_id}
                >
                  {r.run_id.slice(0, 12)}
                </code>
                <span
                  className={
                    "rounded px-2 py-0.5 text-xs uppercase tracking-wide " +
                    (r.status === "running"
                      ? "bg-emerald-100 text-emerald-800"
                      : "bg-amber-100 text-amber-800")
                  }
                >
                  {r.status}
                </span>
                {r.workflow_id && (
                  <span className="truncate text-xs text-slate-500">
                    {r.workflow_id}
                  </span>
                )}
              </div>
              <button
                onClick={() => {
                  if (
                    window.confirm(
                      `Cancel run ${r.run_id.slice(0, 12)}? This will terminate the sandbox immediately.`
                    )
                  ) {
                    cancel.mutate(r.run_id);
                  }
                }}
                disabled={cancel.isPending}
                className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:border-rose-500 hover:text-rose-700 disabled:opacity-50"
              >
                Cancel
              </button>
            </li>
          ))}
        </ul>
      )}
      {cancel.isError && (
        <p className="mt-2 text-xs text-rose-600">
          Cancel failed: {(cancel.error as Error)?.message ?? "unknown error"}
        </p>
      )}
    </section>
  );
}

export default function MonitorPage() {
  const { data, isLoading, error } = useWorkflows();
  if (isLoading) return <div className="text-slate-500">Loading…</div>;
  if (error) return <div className="text-red-600">Failed: {String(error)}</div>;
  const all = data?.workflows ?? [];
  const exec = all.filter((w) => w.workflow_id.includes(":execute-tests:"));

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Executor monitor</h1>
        <Link
          to="/workflows"
          className="text-sm text-blue-700 hover:underline"
        >
          All workflows →
        </Link>
      </div>
      <p className="text-sm text-slate-500">
        Recent execute-tests workflows. Click a workflow to see the per-test execution_result
        artefacts including screenshots from playwright_sandbox runs.
      </p>
      <ActiveRunsCard />
      <div className="bg-white rounded border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 border-b border-slate-200">
            <tr>
              <th className="text-left px-3 py-2">Workflow ID</th>
              <th className="text-right px-3 py-2">execution_results</th>
              <th className="text-left px-3 py-2">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {exec.map((w) => {
              const erCount = w.types.includes("execution_result") ? w.artefact_count : 0;
              return (
                <tr key={w.workflow_id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                  <td className="px-3 py-2 font-mono text-xs">
                    <Link
                      to={`/workflows/${encodeURIComponent(w.workflow_id)}`}
                      className="text-blue-700 hover:underline"
                    >
                      {w.workflow_id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-right">{erCount}</td>
                  <td className="px-3 py-2 text-slate-500">
                    {w.last_seen ? new Date(w.last_seen).toLocaleString() : "—"}
                  </td>
                </tr>
              );
            })}
            {exec.length === 0 && (
              <tr>
                <td colSpan={3} className="px-3 py-6 text-center text-slate-500">
                  No execute-tests workflows yet. Run one from the Designer page (after design-tests
                  has produced test_cases) or via the CLI.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
