import { Link } from "react-router-dom";
import { useWorkflows } from "../lib/queries";

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
