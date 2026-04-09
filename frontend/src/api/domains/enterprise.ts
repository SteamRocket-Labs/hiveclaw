/**
 * Enterprise domain adapter — LLM, org, audit, settings, invitations.
 */

import { get, getBlob, post, put, patch, del, upload } from '../core';

export interface LLMModel {
  id: string;
  provider: string;
  model: string;
  label: string;
  enabled: boolean;
  supports_vision: boolean;
  tenant_id?: string;
  base_url?: string;
  api_key_masked?: string;
  max_output_tokens?: number | null;
  max_input_tokens?: number | null;
  temperature?: number | null;
  created_at?: string;
}

export interface EnterpriseInfo {
  id: string;
  info_type: string;
  content: Record<string, any>;
  version: number;
}

export interface AuditLog {
  id: string;
  user_id?: string;
  agent_id?: string;
  action: string;
  details: Record<string, any>;
  created_at: string;
}

export interface InvitationCode {
  id: string;
  code: string;
  max_uses: number;
  used_count: number;
  is_active: boolean;
  created_at: string;
}

export interface SystemSetting {
  key: string;
  value: Record<string, any>;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface InvitationCodeBatchResult {
  created: number;
  codes: string[];
}

export interface OrgMember {
  id: string;
  name: string;
  email?: string;
  title?: string;
  department_path?: string;
  avatar_url?: string;
}

export interface LLMProviderSpec {
  provider: string;
  display_name: string;
  protocol: string;
  default_base_url?: string | null;
  supports_tool_choice: boolean;
  default_max_tokens: number;
}

export interface LLMTestResult {
  success: boolean;
  latency_ms?: number;
  error?: string;
}

export interface EnterpriseStats {
  total_users: number;
  running_agents: number;
  total_agents: number;
  pending_approvals: number;
  [key: string]: number;
}

export interface MemoryConfig {
  summary_model_id: string | null;
  rerank_model_id: string | null;
  compress_threshold: number;
  keep_recent: number;
  extract_to_viking: boolean;
}

export const enterpriseApi = {
  /** LLM models */
  listLLMModels: (tenantId?: string) => get<LLMModel[]>(`/enterprise/llm-models${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  llmModels: (tenantId?: string) => get<LLMModel[]>(`/enterprise/llm-models${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  templates: () => get<unknown[]>('/role-templates'),
  createLLMModel: (data: Partial<LLMModel> & { api_key?: string; tenant_id?: string }, tenantId?: string) =>
    post<LLMModel>(`/enterprise/llm-models${tenantId ? `?tenant_id=${tenantId}` : ''}`, data),
  updateLLMModel: (id: string, data: Partial<LLMModel> & { api_key?: string }) =>
    put<LLMModel>(`/enterprise/llm-models/${id}`, data),
  deleteLLMModel: (id: string, force = false) => del(`/enterprise/llm-models/${id}${force ? '?force=true' : ''}`),
  testLLM: (data: Record<string, unknown>) => post<LLMTestResult>('/enterprise/llm-test', data),
  getLLMProviders: () => get<LLMProviderSpec[]>('/enterprise/llm-providers'),
  setDefaultModel: (modelId: string, tenantId?: string) =>
    put<{ status: string }>(`/enterprise/llm-models/default${tenantId ? `?tenant_id=${tenantId}` : ''}`, { model_id: modelId }),

  /** Enterprise info */
  getInfo: () => get<EnterpriseInfo[]>('/enterprise/info'),
  updateInfo: (infoType: string, data: Record<string, unknown>) =>
    put<EnterpriseInfo>(`/enterprise/info/${infoType}`, data),

  /** Audit */
  getAuditLogs: (params?: string) => get<AuditLog[]>(`/enterprise/audit-logs${params ? `?${params}` : ''}`),

  /** Stats & quotas */
  getStats: (tenantId?: string) => get<EnterpriseStats>(`/enterprise/stats${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  getTenantQuotas: () => get<Record<string, unknown>>('/enterprise/tenant-quotas'),
  updateTenantQuotas: (data: Record<string, unknown>) => patch<void>('/enterprise/tenant-quotas', data),

  /** Memory config */
  getMemoryConfig: (tenantId?: string) =>
    get<MemoryConfig>(`/enterprise/memory/config${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  updateMemoryConfig: (data: MemoryConfig, tenantId?: string) =>
    put<MemoryConfig>(`/enterprise/memory/config${tenantId ? `?tenant_id=${tenantId}` : ''}`, data),

  /** Invitation codes */
  listInvitationCodes: (params?: string) =>
    get<PaginatedResponse<InvitationCode>>(`/enterprise/invitation-codes${params ? `?${params}` : ''}`),
  createInvitationCode: (data: { max_uses?: number; count?: number }) =>
    post<InvitationCodeBatchResult>('/enterprise/invitation-codes', data),
  deleteInvitationCode: (id: string) => del(`/enterprise/invitation-codes/${id}`),
  exportInvitationCodesCsv: () => getBlob('/enterprise/invitation-codes/export'),

  /** System settings */
  getSetting: (key: string) => get<SystemSetting>(`/enterprise/system-settings/${key}`),
  updateSetting: (key: string, value: Record<string, unknown>) =>
    put<SystemSetting>(`/enterprise/system-settings/${key}`, { value }),

  /** Knowledge base */
  kbFiles: (tenantId?: string) => get<any[]>(`/enterprise/knowledge-base/files${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  kbRead: (path: string) => get<any>(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`),
  kbWrite: (path: string, content: string) => put<any>('/enterprise/knowledge-base/content', { path, content }),
  kbDelete: (path: string) => del(`/enterprise/knowledge-base/content?path=${encodeURIComponent(path)}`),
  kbUpload: (file: File, path?: string) =>
    upload('/enterprise/knowledge-base/upload', file, path ? { path } : undefined),

  /** OIDC */
  getOIDCConfig: () => get<Record<string, unknown>>('/enterprise/oidc-config'),
  updateOIDCConfig: (data: Record<string, unknown>) => put<void>('/enterprise/oidc-config', data),

  /** Org */
  getDepartments: (tenantId?: string) => get<Record<string, any>[]>(`/enterprise/org/departments${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  getOrgMembers: (params?: { search?: string; departmentId?: string; tenantId?: string }) => {
    const qs = new URLSearchParams();
    if (params?.search) qs.set('search', params.search);
    if (params?.departmentId) qs.set('department_id', params.departmentId);
    if (params?.tenantId) qs.set('tenant_id', params.tenantId);
    const query = qs.toString();
    const suffix = query ? `?${query}` : '';
    return get<OrgMember[]>(`/enterprise/org/members${suffix}`);
  },
  syncOrg: (tenantId?: string) => post<Record<string, unknown>>(`/enterprise/org/sync${tenantId ? `?tenant_id=${tenantId}` : ''}`),

  /** Approvals */
  listApprovals: (tenantId?: string) => get<unknown[]>(`/enterprise/approvals${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  resolveApproval: (id: string, data: { action: string }) =>
    post<unknown>(`/enterprise/approvals/${id}/resolve`, data),
};
