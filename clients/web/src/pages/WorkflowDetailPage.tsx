import { Link, useLocation } from "react-router-dom";
import { useWorkflowDetail } from "../lib/queries";
import ArtefactCard from "../components/ArtefactCard";
import StateTransitionMenu from "../components/StateTransitionMenu";

export default function WorkflowDetailPage() {
  const loc = useLocation();
  // The workflow_id contains colons; React Router's :param doesn't survive that.
  // Pull it from pathname after the "/workflows/" prefix.
  const id = decodeURIComponent(loc.pathname.replace(/^\/workflows\//, ""));
  const { data, isLoading, error } = useWorkflowDetail(id);

  if (isLoading) return <div className="text-slate-500">Loading…</div>;
  if (error) return <div className="text-red-600">Failed: {String(error)}</div>;
  if (!data) return null;

  const byType = data.artefacts_by_type;
  const order = ["requirement", "test_case", "execution_result", "approval_policy", "critique_policy"];
  const types = Object.keys(byType).sort((a, b) => order.indexOf(a) - order.indexOf(b));

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link to="/workflows" className="text-sm text-blue-700 hover:underline">
            ← Workflows
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight mt-1 font-mono">{data.workflow_id}</h1>
          <p className="text-sm text-slate-500">{data.total} artefacts</p>
        </div>
      </div>

      {types.map((t) => (
        <section key={t} className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-500">{t}</h2>
          <div className="space-y-2">
            {byType[t].map((a) => (
              <ArtefactCard
                key={a.id}
                artefact={a}
                actions={<StateTransitionMenu artefactId={a.id} currentState={a.state} />}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
