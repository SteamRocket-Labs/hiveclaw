import { get, patch, post } from '../core';

export interface UserQuotaUpdate {
  quota_tokens_per_day?: number | null;
  quota_tokens_per_month?: number | null;
}

export interface ManagedUser {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: string;
  is_active: boolean;
  quota_tokens_per_day?: number | null;
  quota_tokens_per_month?: number | null;
  tokens_used_today: number;
  tokens_used_month: number;
  tokens_used_total: number;
  agents_count: number;
  feishu_open_id?: string | null;
  created_at?: string | null;
  source?: string;
}

export interface UserOffboardingPreview {
  user_id: string;
  display_name: string;
  is_active: boolean;
  owned_agents: Array<{ id: string; name: string; status: string; agent_class: string }>;
  eligible_successors: Array<{ id: string; display_name: string; email: string; role: string }>;
  default_successor_id?: string | null;
  revocations: {
    agent_permissions: number;
    resource_permissions: number;
    knowledge_grants: number;
    refresh_tokens: number;
    external_principals: number;
    local_bridge_connections: number;
    runtime_tasks: number;
    pending_approvals: number;
  };
  blockers: string[];
}

export interface UserOffboardingReceipt {
  status: 'deactivated' | 'already_inactive';
  user_id: string;
  successor_user_id: string;
  transferred_agent_ids: string[];
  transferred_agent_count: number;
  revocations: UserOffboardingPreview['revocations'];
  request_id: string;
}

const tenantQuery = (tenantId?: string) => {
  if (!tenantId) return '';
  return `?tenant_id=${encodeURIComponent(tenantId)}`;
};

export const usersApi = {
  list: (tenantId?: string) => get<ManagedUser[]>(`/users/${tenantQuery(tenantId)}`),
  updateQuota: (userId: string, data: UserQuotaUpdate) => patch<ManagedUser>(`/users/${userId}/quota`, data),
  updateRole: (userId: string, role: string, tenantId?: string) =>
    patch<ManagedUser>(`/users/${userId}/role${tenantQuery(tenantId)}`, { role }),
  previewOffboarding: (userId: string, tenantId?: string) =>
    get<UserOffboardingPreview>(`/users/${userId}/offboarding-preview${tenantQuery(tenantId)}`),
  offboard: (
    userId: string,
    data: { successor_user_id: string; expected_agent_ids: string[]; reason: string; request_id: string },
    tenantId?: string,
  ) => post<UserOffboardingReceipt>(`/users/${userId}/offboard${tenantQuery(tenantId)}`, data),
};
