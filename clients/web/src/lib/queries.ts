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
