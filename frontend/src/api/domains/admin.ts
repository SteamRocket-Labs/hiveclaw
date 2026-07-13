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

export type RuntimeReconciliationAction = 'mark_resolved' | 'archive' | 'retry';

export interface RuntimeRecoveryEvidenceTarget {
  agent_id: string;
  session_id: string;
  runtime_task_id: string;
  source: string;
  expected_manifest_state?: 'present' | 'missing' | 'corrupt' | 'nonregular' | 'identity_mismatch';
  expected_manifest_ref?: string;
  expected_sha256?: string;
  expected_checkpoint_seq?: number | string;
  expected_claim_version?: number | string;
  expected_claim_worker_id?: string;
}

export interface RuntimeRecoveryEvidenceFrame {
  runtime_task_id: string;
  tool_call_id: string;
  tool_name: string;
  status?: string;
  event_type?: string;
  reason?: string;
  source: string;
}

export interface RuntimeRecoveryEvidence {
  schema: string;
  digest: string;
  evidence_complete: boolean;
  incomplete_reasons: string[];
  targets: RuntimeRecoveryEvidenceTarget[];
  frames: RuntimeRecoveryEvidenceFrame[];
}

export interface RuntimeReconciliationFrameDecision {
  runtime_task_id: string;
  tool_call_id: string;
  tool_name: string;
  decision: RuntimeReconciliationAction;
}

export interface RuntimeReconciliationOperation {
  schema: string;
  operation_id: string;
  status: 'prepared' | 'failed' | 'completed';
  action: RuntimeReconciliationAction;
  reason: string;
  actor_user_id: string;
  resumed_by_user_id?: string;
  evidence_digest: string;
  frame_decisions: RuntimeReconciliationFrameDecision[];
  group_root_task_id: string;
  group_member_task_ids: string[];
  prepared_at?: string;
  resumed_at?: string;
  failed_at?: string;
  completed_at?: string;
  error?: string;
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
  result_summary?: string | null;
  metadata?: Record<string, unknown>;
  recovery_evidence?: RuntimeRecoveryEvidence;
  reconciliation_operation?: RuntimeReconciliationOperation | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface RuntimeNotificationDeliveryReconciliation {
  delivery_id: string;
  tenant_id: string;
  source_kind: string;
  source_run_id: string;
  agent_id?: string | null;
  parent_agent_id?: string | null;
  parent_user_id?: string | null;
  task_type?: string;
  status: 'dead_letter' | 'pending' | 'processing' | 'delivered';
  execution_terminal_status: string;
  delivery_only: true;
  does_not_rerun_execution?: true;
  retryable: boolean;
  attempt_count: number;
  last_error?: string | null;
  parent_session_id?: string | null;
  child_session_id?: string | null;
  authority_snapshot?: {
    valid?: boolean;
    tenant_id?: string | null;
    agent_id?: string | null;
    parent_agent_id?: string | null;
    parent_user_id?: string | null;
    parent_session_id?: string | null;
    child_session_id?: string | null;
    [key: string]: unknown;
  };
  reconciliation?: Record<string, unknown>;
  summary?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export const adminApi = {
  listCompanies: () => get<Company[]>('/admin/companies'),
  createCompany: (data: { name: string; slug?: string }) => post<Company>('/admin/companies', data),
  toggleCompany: (id: string) => put<void>(`/admin/companies/${id}/toggle`),
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
    params: {
      tenantId: string;
      action: RuntimeReconciliationAction;
      reason: string;
      confirmed: true;
      evidenceDigest: string;
      frameDecisions: RuntimeReconciliationFrameDecision[];
      operationId?: string;
    },
  ) =>
    post<RuntimeReconciliationTask>(
      `/admin/runtime-reconciliation/${taskId}/action?tenant_id=${encodeURIComponent(params.tenantId)}`,
      {
        action: params.action,
        reason: params.reason,
        confirmed: params.confirmed,
        evidence_digest: params.evidenceDigest,
        frame_decisions: params.frameDecisions,
        ...(params.operationId ? { operation_id: params.operationId } : {}),
      },
    ),
  listRuntimeNotificationDeliveries: (params: {
    tenantId: string;
    status?: 'dead_letter';
    limit?: number;
  }) => {
    const query = new URLSearchParams({
      tenant_id: params.tenantId,
      status: params.status || 'dead_letter',
      limit: String(params.limit || 50),
    });
    return get<RuntimeNotificationDeliveryReconciliation[]>(
      `/admin/runtime-notification-deliveries?${query.toString()}`,
    );
  },
  retryRuntimeNotificationDelivery: (
    deliveryId: string,
    params: { tenantId: string; reason: string; confirmed: true },
  ) => post<RuntimeNotificationDeliveryReconciliation>(
    `/admin/runtime-notification-deliveries/${deliveryId}/retry?tenant_id=${encodeURIComponent(params.tenantId)}`,
    { reason: params.reason, confirmed: params.confirmed },
  ),
  listWorkflowCompletionDeliveries: (params: {
    tenantId: string;
    status?: 'dead_letter';
    limit?: number;
  }) => {
    const query = new URLSearchParams({
      tenant_id: params.tenantId,
      status: params.status || 'dead_letter',
      limit: String(params.limit || 50),
    });
    return get<RuntimeNotificationDeliveryReconciliation[]>(
      `/admin/workflow-completion-deliveries?${query.toString()}`,
    );
  },
  retryWorkflowCompletionDelivery: (
    deliveryId: string,
    params: { tenantId: string; reason: string; confirmed: true },
  ) => post<RuntimeNotificationDeliveryReconciliation>(
    `/admin/workflow-completion-deliveries/${deliveryId}/retry?tenant_id=${encodeURIComponent(params.tenantId)}`,
    { reason: params.reason, confirmed: params.confirmed },
  ),
};
