/**
 * Admin domain adapter — platform-level company management + settings.
 */

import { get, post, put } from '../core';

export interface Company {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  user_count?: number;
  agent_count?: number;
  org_admin_email?: string | null;
  admin_invitation_code?: string;
  created_at: string;
  [key: string]: any;
}

export interface PlatformSettings {
  allow_self_registration: boolean;
  default_user_quota: Record<string, unknown>;
}

export interface MetricsLeaderboard {
  top_companies?: unknown[];
  top_agents?: unknown[];
}

export interface RuntimeReconciliationTask {
  task_id: string;
  tenant_id?: string | null;
  task_type?: string | null;
  status: string;
  parent_agent_id?: string | null;
  child_agent_id?: string | null;
  child_agent_name?: string | null;
  trace_id?: string | null;
  parent_session_id?: string | null;
  child_session_id?: string | null;
  reason?: string | null;
  side_effect_risk?: string | null;
  retry_allowed?: boolean;
  tool_effect_reconciliation_required?: boolean;
  unsettled_tool_effect_count?: number;
  supported_actions?: RuntimeReconciliationAction[];
  result_summary?: string | null;
  metadata?: Record<string, unknown>;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export type RuntimeReconciliationAction = 'mark_resolved' | 'archive' | 'retry' | 'acknowledge_tool_effect';

/**
 * Truthful server receipt of POST /admin/runtime-reconciliation/projection-repair.
 * The repair is exact-code and idempotent: it preserves
 * status=needs_reconciliation and never resolves/archives/retries a task, so the
 * receipt (examined candidates + repaired task ids) is the operator-facing
 * result, not a task status change.
 */
export interface RuntimeProjectionRepairReceipt {
  examined: number;
  repaired_task_ids: string[];
}

export const adminApi = {
  listCompanies: () => get<Company[]>('/admin/companies'),
  createCompany: (data: { name: string; slug?: string }) => post<Company>('/admin/companies', data),
  toggleCompany: (id: string) => put<void>(`/admin/companies/${id}/toggle`),
  assignUserToTenant: (tenantId: string, data: { email: string; role: 'org_admin' | 'member' }) =>
    put<{ status: string; user_id: string; tenant_id: string; role: string; reauthentication_required: boolean }>(
      `/tenants/${encodeURIComponent(tenantId)}/assign-user`,
      data,
    ),
  getPlatformSettings: () => get<PlatformSettings>('/admin/platform-settings'),
  updatePlatformSettings: (data: Partial<PlatformSettings>) => put<PlatformSettings>('/admin/platform-settings', data),
  getMetricsTimeseries: (params: { startDate: string; endDate: string }) =>
    get<unknown[]>(`/admin/metrics/timeseries?start_date=${encodeURIComponent(params.startDate)}&end_date=${encodeURIComponent(params.endDate)}`),
  getMetricsLeaderboards: () => get<MetricsLeaderboard>('/admin/metrics/leaderboards'),
  listRuntimeReconciliation: (params: {
    tenantId: string;
    status?: string;
    limit?: number;
    agentId?: string;
  }) => {
    const query = new URLSearchParams({
      tenant_id: params.tenantId,
      status: params.status || 'needs_reconciliation',
      limit: String(params.limit || 50),
    });
    if (params.agentId) query.set('agent_id', params.agentId);
    return get<RuntimeReconciliationTask[]>(`/admin/runtime-reconciliation?${query.toString()}`);
  },
  getRuntimeReconciliation: (taskId: string, params: { tenantId: string }) =>
    get<RuntimeReconciliationTask>(`/admin/runtime-reconciliation/${taskId}?tenant_id=${encodeURIComponent(params.tenantId)}`),
  applyRuntimeReconciliationAction: (
    taskId: string,
    params: { tenantId: string; action: RuntimeReconciliationAction; reason: string },
  ) =>
    post<RuntimeReconciliationTask>(
      `/admin/runtime-reconciliation/${taskId}/action?tenant_id=${encodeURIComponent(params.tenantId)}`,
      { action: params.action, reason: params.reason },
    ),
  repairRuntimeReconciliationProjections: (params: { tenantId: string; limit?: number }) => {
    const query = new URLSearchParams({
      tenant_id: params.tenantId,
      limit: String(params.limit ?? 100),
    });
    // The endpoint takes no request body; the core post helper omits the body
    // when it is undefined.
    return post<RuntimeProjectionRepairReceipt>(`/admin/runtime-reconciliation/projection-repair?${query.toString()}`);
  },
};
