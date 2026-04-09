import { get, patch } from '../core';

export interface UserQuotaUpdate {
  quota_message_limit?: number;
  quota_message_period?: string;
  quota_max_agents?: number;
  quota_agent_ttl_hours?: number;
}

export const usersApi = {
  list: (tenantId?: string) => get<unknown[]>(`/users/${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  updateQuota: (userId: string, data: UserQuotaUpdate) => patch<unknown>(`/users/${userId}/quota`, data),
  updateRole: (userId: string, role: string) => patch<unknown>(`/org/users/${userId}`, { role }),
};
