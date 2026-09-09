import { get, post } from '../core';

export interface A2AArtifactRef {
  artifact_id: string;
  name: string;
  path: string;
  content_hash: string;
  producer_agent_id: string;
  producer_session_id: string;
}

export interface A2AGraphPreview {
  definition: Record<string, unknown>;
  definition_hash: string;
  args: Record<string, unknown>;
  node_order: string[];
  admissible: boolean;
  collaboration_checks: { node_id: string; allowed: boolean; reason?: string }[];
}

export interface A2AGraphRun {
  run_id: string;
  status: string;
  reason?: string;
  definition: Record<string, unknown>;
  definition_hash: string;
  args: Record<string, unknown>;
  session_id: string;
  budget_run_id?: string;
  steps: {
    id: string;
    type: string;
    status: string;
    error?: string;
    journal: {
      agent_id?: string;
      session_id?: string;
      task_id?: string;
      attempt?: number;
      artifacts?: A2AArtifactRef[];
    };
  }[];
}

export type A2AGraphAction = 'resume' | 'approve' | 'reject' | 'retry' | 'cancel';
const base = (agentId: string) => `/agents/${agentId}/a2a-workflows`;

export const a2aWorkflowApi = {
  preview: (agentId: string, sessionId: string, definition: Record<string, unknown>, args: Record<string, unknown>) =>
    post<A2AGraphPreview>(`${base(agentId)}/preview`, { session_id: sessionId, definition, args }),
  start: (agentId: string, sessionId: string, runId: string, preview: A2AGraphPreview) =>
    post<{ run_id: string; status: string; replayed: boolean }>(`${base(agentId)}/runs`, {
      session_id: sessionId, run_id: runId, definition: preview.definition,
      definition_hash: preview.definition_hash, args: preview.args,
    }),
  list: (agentId: string, sessionId: string) =>
    get<{ run_id: string; name: string; status: string }[]>(`${base(agentId)}/runs?session_id=${encodeURIComponent(sessionId)}`),
  read: (agentId: string, runId: string) => get<A2AGraphRun>(`${base(agentId)}/runs/${runId}`),
  control: (agentId: string, runId: string, action: A2AGraphAction, nodeId?: string) =>
    post<A2AGraphRun>(`${base(agentId)}/runs/${runId}/control`, { action, node_id: nodeId }),
};
