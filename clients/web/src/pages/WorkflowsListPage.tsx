import { Link } from "react-router-dom";
import { useWorkflows } from "../lib/queries";

export default function WorkflowsListPage() {
  const { data, isLoading, error } = useWorkflows();
  if (isLoading) return <div className="text-slate-500">Loading workflows…</div>;
  if (error) return <div className="text-red-600">Failed: {String(error)}</div>;
  const workflows = data?.workflows ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Workflows</h1>
        <span className="text-sm text-slate-500">{workflows.length} total</span>
      </div>
      <div className="bg-white rounded border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-600 border-b border-slate-200">
            <tr>
              <th className="text-left px-3 py-2">Workflow ID</th>
              <th className="text-left px-3 py-2">Types</th>
              <th className="text-right px-3 py-2">Artefacts</th>
              <th className="text-left px-3 py-2">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {workflows.map((w) => (
              <tr key={w.workflow_id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                <td className="px-3 py-2 font-mono text-xs">
                  <Link to={`/workflows/${encodeURIComponent(w.workflow_id)}`} className="text-blue-700 hover:underline">
                    {w.workflow_id}
                  </Link>
                </td>
                <td className="px-3 py-2 text-slate-700">{w.types.join(", ")}</td>
                <td className="px-3 py-2 text-right">{w.artefact_count}</td>
                <td className="px-3 py-2 text-slate-500">
                  {w.last_seen ? new Date(w.last_seen).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
            {workflows.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-slate-500">
                  No workflows yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
