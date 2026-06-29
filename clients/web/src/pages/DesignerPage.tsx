import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useStartDesignTests } from "../lib/queries";

export default function DesignerPage() {
  const nav = useNavigate();
  const start = useStartDesignTests();
  const [title, setTitle] = useState("User login");
  const [reqId, setReqId] = useState("R-" + Math.random().toString(36).slice(2, 8));
  const [criteria, setCriteria] = useState(
    "Valid credentials log the user in and return 200\nInvalid credentials return 401"
  );
  const [criticality, setCriticality] = useState<"low" | "medium" | "high" | "safety_critical">(
    "low"
  );
  const [err, setErr] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      const ac = criteria
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await start.mutateAsync({
        requirement: { id: reqId, title, acceptance_criteria: ac },
        criticality,
      });
      nav(`/run/${encodeURIComponent(res.workflow_id)}`);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "failed");
    }
  }

  return (
    <div className="space-y-4 max-w-2xl">
      <h1 className="text-2xl font-semibold tracking-tight">Design tests</h1>
      <p className="text-sm text-slate-500">
        Submit a requirement and the agent will generate test cases for review.
      </p>

      <form onSubmit={onSubmit} className="space-y-4 bg-white rounded border border-slate-200 p-5">
        <div className="space-y-1">
          <label className="text-sm font-medium text-slate-700">Requirement ID</label>
          <input
            value={reqId}
            onChange={(e) => setReqId(e.target.value)}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm font-mono"
            required
          />
        </div>
        <div className="space-y-1">
          <label className="text-sm font-medium text-slate-700">Title</label>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm"
            required
          />
        </div>
        <div className="space-y-1">
          <label className="text-sm font-medium text-slate-700">
            Acceptance criteria <span className="text-slate-400 font-normal">(one per line)</span>
          </label>
          <textarea
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            rows={5}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm font-mono"
            required
          />
        </div>
        <div className="space-y-1">
          <label className="text-sm font-medium text-slate-700">Criticality</label>
          <select
            value={criticality}
            onChange={(e) => setCriticality(e.target.value as typeof criticality)}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm"
          >
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high (will run critic if cloud key present)</option>
            <option value="safety_critical">safety_critical</option>
          </select>
        </div>
        {err && <div className="text-sm text-red-600">{err}</div>}
        <button
          type="submit"
          disabled={start.isPending}
          className="bg-slate-900 text-white rounded px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {start.isPending ? "Starting…" : "Start workflow"}
        </button>
      </form>
    </div>
  );
}
