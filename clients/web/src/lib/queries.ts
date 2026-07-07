import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

export type WorkflowSummary = {
  workflow_id: string;
  tenant_id: string;
  artefact_count: number;
  types: string[];
  first_seen: string | null;
  last_seen: string | null;
};

export type Artefact = {
  id: string;
  type: string;
  state: string;
  version: number;
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  workflow_id: string | null;
  actor: string;
  actor_type: string;
  attestation: Record<string, unknown>;
  compliance_level: string;
  parent_id: string | null;
  created_at: string;
  updated_at: string;
};

export type HistoryEntry = {
  version: number;
  state: string;
  actor: string;
  policy_version: string | null;
  changed_at: string;
  attestation: Record<string, unknown>;
};

// D1.4 — live quota for the current tenant (refreshes every 5s)
export type QuotaStatus = {
  email: string;
  role: string;
  urn: string;
  tenant_id: string;
  quota?: {
    concurrent: { current: number; max: number };
    daily: { current: number; max: number; resets_at: string };
  };
};

export function useQuota() {
  return useQuery({
    queryKey: ["me-quota"],
    queryFn: () => api<QuotaStatus>("/api/me"),
    refetchInterval: 5_000,
  });
}

export function useWorkflows() {
  return useQuery({
    queryKey: ["workflows"],
    queryFn: () => api<{ workflows: WorkflowSummary[] }>("/api/workflows"),
    refetchInterval: 10_000,
  });
}

export function useWorkflowDetail(id: string | undefined) {
  return useQuery({
    enabled: !!id,
    queryKey: ["workflow", id],
    queryFn: () =>
      api<{ workflow_id: string; artefacts_by_type: Record<string, Artefact[]>; total: number }>(
        `/api/workflows/${encodeURIComponent(id!)}`
      ),
    refetchInterval: 10_000,
  });
}

export function useArtefactHistory(id: string | undefined) {
  return useQuery({
    enabled: !!id,
    queryKey: ["history", id],
    queryFn: () => api<HistoryEntry[]>(`/api/artefacts/${encodeURIComponent(id!)}/history`),
  });
}

export function useTransition(artefactId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (to_state: string) =>
      api<Artefact>(`/api/artefacts/${encodeURIComponent(artefactId)}/transition`, {
        method: "POST",
        json: { to_state },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["history", artefactId] });
      qc.invalidateQueries({ queryKey: ["workflow"] });
    },
  });
}

// D3c — Executor

export type ExecuteMode = "simulate" | "scripts" | "playwright_sandbox";

export type ExecuteTestsRequest = {
  test_case_ids: string[];
  mode: ExecuteMode;
  target_url?: string;
  allowed_urls?: string[];
  sandbox_timeout_seconds?: number;
  language?: "playwright" | "robot";
  criticality?: "low" | "medium" | "high" | "safety_critical";
};

export function useStartExecuteTests() {
  return useMutation({
    mutationFn: (body: ExecuteTestsRequest) =>
      api<{ workflow_id: string }>("/api/workflows/execute-tests", {
        method: "POST",
        json: body,
      }),
  });
}

// Convert MinIO s3://bucket/key URLs to BFF media proxy paths.
export function mediaUrl(s3Url: string | undefined): string | null {
  if (!s3Url) return null;
  const m = /^s3:\/\/[^/]+\/(.+)$/.exec(s3Url);
  if (!m) return null;
  return `/api/media?key=${encodeURIComponent(m[1])}`;
}

// D3b — Designer wizard

export type DesignTestsRequest = {
  requirement: {
    id?: string;
    title: string;
    acceptance_criteria: string[];
    [k: string]: unknown;
  };
  criticality: "low" | "medium" | "high" | "safety_critical";
};

export type WorkflowStatus = {
  workflow_id: string;
  status: "RUNNING" | "COMPLETED" | "FAILED" | "CANCELED" | "TERMINATED" | "TIMED_OUT" | "UNKNOWN";
  start_time: string | null;
  close_time: string | null;
  result?: Record<string, unknown>;
  result_error?: string;
};

export function useStartDesignTests() {
  return useMutation({
    mutationFn: (body: DesignTestsRequest) =>
      api<{ workflow_id: string }>("/api/workflows/design-tests", {
        method: "POST",
        json: body,
      }),
  });
}

export function useWorkflowStatus(id: string | undefined) {
  return useQuery({
    enabled: !!id,
    queryKey: ["wf-status", id],
    queryFn: () =>
      api<WorkflowStatus>(`/api/workflow-status/${encodeURIComponent(id!)}`),
    refetchInterval: (q) => {
      const s = (q.state.data as WorkflowStatus | undefined)?.status;
      return s && s !== "RUNNING" ? false : 3_000;
    },
  });
}

// D1.4.1 — Active runs + cancel
export type RunSummary = {
  run_id: string;
  status: "queued" | "running" | "canceled" | "completed" | "failed";
  submitted_at: string | null;
  started_at: string | null;
  workflow_id: string | null;
};

export function useActiveRuns() {
  return useQuery({
    queryKey: ["active-runs"],
    queryFn: () => api<{ runs: RunSummary[] }>("/api/runs?active=true"),
    refetchInterval: 3_000,
  });
}

export function useCancelRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) =>
      api<{ run_id: string; status: string; previous_status: string }>(
        `/api/runs/${encodeURIComponent(runId)}/cancel`,
        { method: "POST" }
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["active-runs"] });
      qc.invalidateQueries({ queryKey: ["me-quota"] });
      qc.invalidateQueries({ queryKey: ["workflow"] });
      qc.invalidateQueries({ queryKey: ["wf-status"] });
    },
  });
}
