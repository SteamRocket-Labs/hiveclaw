import { get, patch, post } from '../core';

export interface RuntimeBudgetPolicy {
  id: string;
  tenant_id: string | null;
  name: string;
  enabled: boolean;
  priority: number;
  scope_type: string;
  source: string | null;
  profile: string | null;
  agent_id: string | null;
  trigger_id: string | null;
  enforcement_mode: 'observe' | 'enforce' | string;
  fail_mode: string;
  max_tokens: number | null;
  max_cache_miss_tokens: number | null;
  max_subagents: number | null;
  max_delegations: number | null;
  max_background_tasks: number | null;
  max_continuation_wakes: number | null;
  max_provider_calls: number | null;
  default_child_token_reservation: number;
  default_llm_call_token_reservation: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface RuntimeBudgetPolicyWrite {
  name: string;
  enabled?: boolean;
  priority?: number;
  scope_type?: string;
  source?: string | null;
  profile?: string | null;
  agent_id?: string | null;
  trigger_id?: string | null;
  enforcement_mode?: 'observe' | 'enforce' | string;
  fail_mode?: string;
  max_tokens?: number | null;
  max_cache_miss_tokens?: number | null;
  max_subagents?: number | null;
  max_delegations?: number | null;
  max_background_tasks?: number | null;
  max_continuation_wakes?: number | null;
  max_provider_calls?: number | null;
  default_child_token_reservation?: number;
  default_llm_call_token_reservation?: number;
}

export interface RuntimeBudgetRun {
  id: string;
  tenant_id: string | null;
  root_run_kind: string;
  root_run_key: string;
  source: string | null;
  profile: string | null;
  status: string;
  enforcement_mode: string;
  terminal_reason: string | null;
  user_status: string;
  user_reason: string;
  user_next_action: string;
  created_at: string | null;
  expires_at: string | null;
  completed_at: string | null;
}

export interface RuntimeBudgetEvent {
  id: string;
  tenant_id: string | null;
  event_type: string;
  reservation_key: string | null;
  allowed: boolean | null;
  would_deny: boolean;
  reason: string | null;
  user_message: string;
  created_at: string | null;
}

export interface RuntimeBudgetTenantModeResult {
  tenant_id: string;
  enforcement_mode: string;
  updated_policies: number;
}

export const runtimeBudgetApi = {
  listPolicies: () => get<RuntimeBudgetPolicy[]>('/runtime-budgets/policies'),
  createPolicy: (payload: RuntimeBudgetPolicyWrite) =>
    post<RuntimeBudgetPolicy>('/runtime-budgets/policies', payload),
  updatePolicy: (policyId: string, payload: Partial<RuntimeBudgetPolicyWrite>) =>
    patch<RuntimeBudgetPolicy>(`/runtime-budgets/policies/${policyId}`, payload),
  listRuns: (params: { status?: string; agentId?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.status) query.set('status', params.status);
    if (params.agentId) query.set('agent_id', params.agentId);
    if (params.limit) query.set('limit', String(params.limit));
    const suffix = query.toString() ? `?${query.toString()}` : '';
    return get<RuntimeBudgetRun[]>(`/runtime-budgets/runs${suffix}`);
  },
  listEvents: (runId: string, limit = 100) =>
    get<RuntimeBudgetEvent[]>(`/runtime-budgets/runs/${runId}/events?limit=${limit}`),
  cancelRun: (runId: string, reason: string) =>
    post<RuntimeBudgetRun>(`/runtime-budgets/runs/${runId}/cancel`, { reason }),
  approveOverrun: (
    runId: string,
    payload: {
      reason: string;
      enforcement_mode?: 'observe' | 'enforce' | string;
      max_subagents?: number | null;
      max_cache_miss_tokens?: number | null;
      max_tokens?: number | null;
    },
  ) => post<RuntimeBudgetRun>(`/runtime-budgets/runs/${runId}/approve-overrun`, payload),
  setTenantEnforcementMode: (payload: { enforcement_mode: 'observe' | 'enforce' | string; reason: string }) =>
    post<RuntimeBudgetTenantModeResult>('/runtime-budgets/tenant/enforcement-mode', payload),
};
