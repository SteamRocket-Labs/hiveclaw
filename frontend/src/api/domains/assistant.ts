/**
 * My Assistant binding. Reads and writes go through the shared API client.
 */

import { get, post } from '../core';

export interface AssistantScopeEcho {
  expected_user_id?: string;
  expected_tenant_id?: string;
}

export interface AssistantAgent {
  id: string;
  name: string;
  role_description: string;
  status: string;
  primary_model_id: string | null;
  model_label: string | null;
}

export interface AssistantCandidate {
  id: string;
  name: string;
  role_description: string;
  selected: false;
}

export interface AssistantEntryTarget {
  kind: 'session' | 'employee' | 'setup';
  path: string;
  agent_id: string | null;
  session_id: string | null;
}

export interface AssistantBinding {
  schema: 'hive.assistant_binding.v1';
  state: string;
  scope: { user_id: string; tenant_id: string | null };
  agent: AssistantAgent | null;
  readiness: {
    ready: boolean;
    worker: string;
    model: string;
    workspace: string;
    blockers: string[];
  };
  next_actions: string[];
  entry: AssistantEntryTarget;
  candidates: AssistantCandidate[];
  binding_grants_access: false;
  applied: boolean;
  created: boolean;
  legacy_provenance: string;
  history_retained: boolean;
  prior_agent_id: string | null;
  message: string;
  rejection?: string;
  blocked_by?: string;
}

function scopeQuery(scope?: AssistantScopeEcho): string {
  const params = new URLSearchParams();
  if (scope?.expected_user_id) params.set('expected_user_id', scope.expected_user_id);
  if (scope?.expected_tenant_id) params.set('expected_tenant_id', scope.expected_tenant_id);
  const text = params.toString();
  return text ? `?${text}` : '';
}

export const assistantApi = {
  get: (scope?: AssistantScopeEcho) => get<AssistantBinding>(`/assistant${scopeQuery(scope)}`),
  open: (body: AssistantScopeEcho & { idempotency_key?: string; replace_unavailable?: boolean }) =>
    post<AssistantBinding>('/assistant/open', body),
  select: (body: AssistantScopeEcho & { agent_id: string }) => post<AssistantBinding>('/assistant/select', body),
};
