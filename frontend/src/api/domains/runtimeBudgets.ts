import { del, get, patch, post } from '../core';

export type BudgetLimitName = 'max_tokens' | 'max_cache_miss_tokens' | 'max_subagents' | 'max_team_sessions'
  | 'max_delegations' | 'max_background_tasks' | 'max_continuation_wakes' | 'max_provider_calls';

export interface BudgetIncreaseRequest {
  id: string;
  approval_episode_id: string;
  business_team_id: string;
  requester_user_id: string;
  requester_name?: string | null;
  limits: Partial<Record<BudgetLimitName, number>>;
  reason: string;
  status: 'pending' | 'approved' | 'rejected' | 'withdrawn' | 'superseded';
  created_at: string;
  can_withdraw?: boolean;
}

export interface TeamBudgetRun {
  id: string;
  agent_id: string;
  agent_name: string;
  status: string;
  user_status: string;
  approval_episode_id: string | null;
  created_at: string;
  limits: Record<BudgetLimitName, number | null>;
  usage: Record<string, { used: number; reserved: number }>;
  increase_requests: BudgetIncreaseRequest[];
}

export interface TeamBudgetRuns {
  tenant_id: string;
  business_team_id: string;
  runs: TeamBudgetRun[];
  next_offset: number | null;
}

export interface BudgetIncreaseInput {
  request_id: string;
  approval_episode_id: string;
  limits: Partial<Record<BudgetLimitName, number>>;
  reason: string;
}

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
  max_team_sessions: number | null;
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
  max_team_sessions?: number | null;
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
  approval_episode_id: string | null;
  enforcement_mode: string;
  terminal_reason: string | null;
  user_status: string;
  user_reason: string;
  user_next_action: string;
  created_at: string | null;
  expires_at: string | null;
  completed_at: string | null;
  increase_requests?: BudgetIncreaseRequest[];
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

export interface BudgetTransitionDelivery {
  id: string;
  tenant_id: string;
  budget_run_id: string;
  budget_event_id: string;
  transition: string;
  channel: string;
  status: 'pending' | 'processing' | 'delivered' | 'dead_letter' | 'needs_reconciliation' | string;
  attempt_count: number;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}

export const runtimeBudgetApi = {
  listTeamRuns: (teamId: string, offset = 0, limit = 10) =>
    get<TeamBudgetRuns>(`/business-teams/${teamId}/runtime-budget-runs?offset=${offset}&limit=${limit}`),
  getTeamRun: (teamId: string, runId: string) =>
    get<TeamBudgetRun>(`/business-teams/${teamId}/runtime-budget-runs/${runId}`),
  requestIncrease: (teamId: string, runId: string, body: BudgetIncreaseInput) =>
    post<BudgetIncreaseRequest>(`/business-teams/${teamId}/runtime-budget-runs/${runId}/increase-requests`, body),
  withdrawIncrease: (teamId: string, runId: string, requestId: string) =>
    del<BudgetIncreaseRequest>(`/business-teams/${teamId}/runtime-budget-runs/${runId}/increase-requests/${requestId}`),
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
      approval_episode_id: string;
      increase_request_id?: string;
      reason: string;
      enforcement_mode?: 'observe' | 'enforce' | string;
      max_subagents?: number | null;
      max_team_sessions?: number | null;
      max_cache_miss_tokens?: number | null;
      max_tokens?: number | null;
      max_delegations?: number | null;
      max_background_tasks?: number | null;
      max_continuation_wakes?: number | null;
      max_provider_calls?: number | null;
    },
  ) => post<RuntimeBudgetRun>(`/runtime-budgets/runs/${runId}/approve-overrun`, payload),
  rejectOverrun: (runId: string, payload: { approval_episode_id: string; increase_request_id?: string; reason: string }) =>
    post<RuntimeBudgetRun>(`/runtime-budgets/runs/${runId}/reject-overrun`, payload),
  listTransitionDeliveries: (params: { runId?: string; status?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.runId) query.set('budget_run_id', params.runId);
    if (params.status) query.set('status', params.status);
    if (params.limit) query.set('limit', String(params.limit));
    const suffix = query.toString() ? `?${query.toString()}` : '';
    return get<BudgetTransitionDelivery[]>(`/runtime-budgets/transition-deliveries${suffix}`);
  },
  resolveTransitionDelivery: (
    deliveryId: string,
    payload: { action: 'retry' | 'mark_delivered'; reason: string },
  ) => post<BudgetTransitionDelivery>(`/runtime-budgets/transition-deliveries/${deliveryId}/resolve`, payload),
  setTenantEnforcementMode: (payload: { enforcement_mode: 'observe' | 'enforce' | string; reason: string }) =>
    post<RuntimeBudgetTenantModeResult>('/runtime-budgets/tenant/enforcement-mode', payload),
};
