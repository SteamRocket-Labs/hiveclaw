import { get, post, put } from '../core';

export type OwnerActionZone = 'full_authority' | 'confirm_first' | 'never_do';
export type OwnerActionId = 'tool.external_effect' | 'tool.local_read' | 'tool.local_write';
export type OwnerActionPolicyActions = Record<OwnerActionId, OwnerActionZone>;

export interface OwnerActionPolicy {
  schema: 'hive.owner_action_policy.v1';
  actions: OwnerActionPolicyActions;
  version: number;
  revision_id: string | null;
  content_hash: string;
  source: string;
  valid: boolean;
  error_code: string | null;
  can_manage: boolean;
}

export interface OwnerActionPolicyUpdate {
  actions: OwnerActionPolicyActions;
  expected_version: number;
}

export interface OwnerActionPolicyRollback {
  target_version: number;
  expected_version: number;
  reason: string;
}

export interface OwnerActionPolicyHistoryItem {
  version: number;
  is_active: boolean;
  change_source: string;
  created_at: string | null;
}

export interface OwnerActionPolicyHistory {
  items: OwnerActionPolicyHistoryItem[];
}

export interface AutonomyQuery {
  lookbackHours?: number;
}

export interface RuntimeTaskQuery {
  taskType?: string;
  triggerId?: string;
  status?: string;
  limit?: number;
  diagnostics?: boolean;
}

export interface RuntimeAuthorityOptions {
  operatorView?: boolean;
  reason?: string;
}

export interface RuntimeWorkLedgerItem {
  id: string;
  title: string;
  content?: string;
  subject?: string;
  description?: string;
  activeForm?: string;
  active_form?: string;
  status: string;
  required?: boolean;
  owner?: string;
  blocks?: string[];
  blockedBy?: string[];
  updated_at?: string | null;
  evidence_refs?: string[];
  command?: string;
}

export interface RuntimeWorkLedgerProgress {
  id: string;
  status: string;
  delta: string;
  output_paths?: string[];
  blocked_reason?: string | null;
  created_at?: string | null;
}

export interface RuntimeWorkLedgerFailure {
  id: string;
  attempt: string;
  error: string;
  next_strategy?: string;
  resolved?: boolean;
  created_at?: string | null;
}

export interface RuntimeWorkLedgerView {
  schema: 'agent_work_ledger_view.v1';
  path?: string | null;
  agent_id?: string | null;
  plan_id?: string | null;
  runtime_task_id?: string | null;
  session_id?: string | null;
  source?: string | null;
  status?: string | null;
  current_phase?: string | null;
  todo_items: RuntimeWorkLedgerItem[];
  verification?: RuntimeWorkLedgerItem[];
  progress?: RuntimeWorkLedgerProgress[];
  failures?: RuntimeWorkLedgerFailure[];
  findings?: Array<{ id: string; summary: string; trust?: string; source_refs?: string[] }>;
  open_questions?: string[];
  evidence_refs?: string[];
  counts?: {
    todos_total?: number;
    todos_complete?: number;
    todos_open?: number;
    verification_pending?: number;
    progress_count?: number;
    failures_open?: number;
  };
  created_at?: string | null;
  updated_at?: string | null;
}

const withParams = (path: string, params: Record<string, string | number | boolean | undefined>) => {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') qs.set(key, String(value));
  }
  const query = qs.toString();
  return query ? `${path}?${query}` : path;
};

const authorityParams = (authority?: RuntimeAuthorityOptions) => {
  if (!authority?.operatorView) return {};
  const reason = String(authority.reason || '').trim();
  if (!reason) throw new Error('Operator View requires an audit reason');
  return { operator_override: true, operator_reason: reason };
};

export const autonomyApi = {
  getOverview: (agentId: string, query: AutonomyQuery = {}) =>
    get<any>(withParams(`/agents/${agentId}/autonomy/overview`, {
      lookback_hours: query.lookbackHours,
    })),
  getDiagnostics: (agentId: string, query: AutonomyQuery = {}) =>
    get<any>(withParams(`/agents/${agentId}/autonomy/diagnostics`, {
      lookback_hours: query.lookbackHours,
    })),
  getActionPolicy: (agentId: string) =>
    get<OwnerActionPolicy>(`/agents/${agentId}/autonomy/action-policy`),
  updateActionPolicy: (agentId: string, input: OwnerActionPolicyUpdate) =>
    put<OwnerActionPolicy>(`/agents/${agentId}/autonomy/action-policy`, input),
  getActionPolicyHistory: (agentId: string, limit = 20) =>
    get<OwnerActionPolicyHistory>(
      withParams(`/agents/${agentId}/autonomy/action-policy/history`, { limit }),
    ),
  rollbackActionPolicy: (agentId: string, input: OwnerActionPolicyRollback) =>
    post<OwnerActionPolicy>(`/agents/${agentId}/autonomy/action-policy/rollback`, input),
  listRuntimeTasks: (agentId: string, query: RuntimeTaskQuery = {}) =>
    get<any[]>(withParams(`/agents/${agentId}/runtime-tasks`, {
      task_type: query.taskType,
      trigger_id: query.triggerId,
      status: query.status,
      limit: query.limit,
      diagnostics: query.diagnostics,
    })),
  getRuntimeArtifact: (
    agentId: string,
    runtimeTaskId: string,
    diagnostics = false,
    authority?: RuntimeAuthorityOptions,
  ) =>
    get<any>(withParams(`/agents/${agentId}/runtime-artifacts/${runtimeTaskId}`, {
      diagnostics: diagnostics || undefined,
      ...authorityParams(authority),
    })),
  getRuntimeWorkLedger: (agentId: string, runtimeTaskId: string, authority?: RuntimeAuthorityOptions) =>
    get<RuntimeWorkLedgerView>(withParams(
      `/agents/${agentId}/runtime-work-ledgers/${runtimeTaskId}`,
      authorityParams(authority),
    )),
  getSessionWorkLedger: (agentId: string, sessionId: string, authority?: RuntimeAuthorityOptions) =>
    get<RuntimeWorkLedgerView>(withParams(
      `/agents/${agentId}/sessions/${sessionId}/work-ledger`,
      authorityParams(authority),
    )),
};
