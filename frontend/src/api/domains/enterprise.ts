/**
 * Enterprise domain adapter — LLM, org, audit, settings, invitations.
 */

import { get, getBlob, post, put, patch, del } from '../core';

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
  reasoning_mode?: string | null;
  reasoning_effort?: string | null;
  reasoning_budget_tokens?: number | null;
  reasoning_display?: string | null;
  preserve_reasoning?: boolean | null;
  text_verbosity?: string | null;
  provider_options?: Record<string, unknown> | null;
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

export interface SecurityAuditEvent {
  id: string;
  event_type: string;
  severity: string;
  actor_type: string;
  actor_id?: string | null;
  tenant_id: string;
  resource_type?: string | null;
  resource_id?: string | null;
  action: string;
  details: Record<string, unknown>;
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

export interface LegacyCompanyFilesStatus {
  available: boolean;
  file_count: number;
  total_bytes: number;
  excluded_symlink_count: number;
  read_only: true;
  retired: true;
  surface_kind: 'legacy_company_files_quarantine';
  company_kb_available: false;
  agent_consumable: false;
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

export interface IdentityDryRunRow {
  object_kind: string;
  object_id: string;
  disposition: string;
  reason: string;
  user_id: string | null;
  global_unique_blocked: boolean;
  provenance?: {
    source?: string;
    observed_at?: string;
    origin?: string;
    applied?: boolean;
    subject_kind?: string | null;
    match_subject?: string | null;
    claimed_root_user_id?: string | null;
  };
}

export interface IdentityDryRunReport {
  schema: string;
  tenant_id: string;
  source_revision: string;
  applied: boolean;
  inputs: { stored_rows: boolean; incoming_provider_subjects: boolean };
  counts: Record<string, number>;
  rows: IdentityDryRunRow[];
  department_proposals?: Array<{ name?: string; action?: string; applied?: boolean }>;
}

export interface IdentityLinkRecord {
  id: string;
  org_member_id: string;
  user_id: string;
  provider_id: string | null;
  subject_kind: string | null;
  status: string;
  stored_status: string;
  version: string;
  source: string | null;
  created_by_user_id: string | null;
  valid: boolean;
  reason: string;
}

export interface IdentityLinkMember {
  org_member_id: string;
  label: string;
  subject_kinds: string[];
  reason: string;
  valid: boolean;
  links: IdentityLinkRecord[];
}

export interface IdentityLinkList {
  members: IdentityLinkMember[];
  accounts: Array<{ user_id: string; label: string; is_active: boolean }>;
  providers: Array<{ id: string; provider_type: string; name: string; is_active: boolean }>;
}

export interface IdentityLinkResult {
  applied: boolean;
  code: string;
  reason: string;
  message: string;
  retryable: boolean;
  link: IdentityLinkRecord | null;
}

export interface IdentityBackfillRow {
  org_member_id: string;
  disposition: string;
  reason: string;
  user_id: string | null;
  subject_kinds: string[];
  selectable: boolean;
}

export interface IdentityBackfillPreview {
  schema: string;
  tenant_id: string;
  applied: boolean;
  counts: Record<string, number>;
  rows: IdentityBackfillRow[];
}

export interface CutoverCountGroup {
  applied?: number;
  created?: number;
  mapped?: number;
  revoked?: number;
  changed?: number;
  noop: number;
  skipped: number;
  conflict: number;
  unavailable: number;
}

export interface IdentityCutoverResult {
  schema: string;
  tenant_id: string;
  status: string;
  applied: boolean;
  counts: {
    link_backfill: CutoverCountGroup;
    department_mappings: CutoverCountGroup;
    user_assignments: CutoverCountGroup;
  };
  rows: Array<Record<string, unknown>>;
}

export interface DepartmentDirectoryReport {
  schema: string;
  tenant_id: string;
  applied: boolean;
  org_departments: Array<{
    org_department_id: string;
    name: string;
    provider_department_id: string | null;
    link: { id: string; department_id: string | null; status: string } | null;
    proposal: { action: string; selectable: boolean; reason: string } | null;
    same_name_department_ids: string[];
  }>;
  departments: Array<{ id: string; name: string; parent_id: string | null }>;
  links: Array<{ id: string; org_department_id: string; department_id: string | null; status: string }>;
}

export interface DepartmentLinkSelection {
  org_department_id?: string;
  action: 'create' | 'link' | 'repair' | 'revoke';
  department_id?: string;
  link_id?: string;
}

export interface CutoverJournalReport {
  schema: string;
  tenant_id: string;
  applied: boolean;
  rows: Array<{
    id: string;
    object_kind: string;
    object_id: string;
    before: Record<string, unknown> | null;
    after: Record<string, unknown> | null;
    applied_at: string | null;
    actor_id?: string | null;
    actor_label?: string | null;
  }>;
}

export interface IdentityLinkCreateBody {
  org_member_id: string;
  user_id: string;
  provider_id: string;
  subject_kind: string;
}

export interface IdentityLinkRevokeBody {
  expected_version: string;
  expected_user_id: string;
  expected_provider_id: string;
  expected_subject_kind: string;
  expected_status: string;
}

export interface LLMProviderSpec {
  provider: string;
  display_name: string;
  protocol: string;
  default_base_url?: string | null;
  supports_tool_choice: boolean;
  default_max_tokens: number;
  max_input_tokens?: number;
  reasoning_strategy?: string;
  supported_reasoning_modes?: string[];
  supported_reasoning_efforts?: string[];
  supports_reasoning_budget?: boolean;
  supports_reasoning_preservation?: boolean;
  supports_text_verbosity?: boolean;
  supports_tools_with_reasoning?: boolean;
  recommended_models?: Array<Record<string, unknown>>;
}

export interface LLMTestResult {
  success: boolean;
  latency_ms?: number;
  error?: string;
  provider_success?: boolean;
  audit_status?: 'result_persistence_failed';
  retryable?: boolean;
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

export interface CapabilityDefinition {
  capability: string;
  tools: string[];
}

export interface CapabilityPolicy {
  id: string;
  capability: string;
  agent_id?: string | null;
  allowed: boolean;
  requires_approval: boolean;
  conditions: Record<string, unknown>;
}

export interface CapabilityPolicyUpdate {
  capability: string;
  agent_id?: string | null;
  allowed: boolean;
  requires_approval: boolean;
  conditions?: Record<string, unknown>;
}

export type CapabilityPolicyQuery = string | { agentId?: string | null; tenantId?: string | null } | undefined;

function capabilityPolicyQuerySuffix(query?: CapabilityPolicyQuery): string {
  const qs = new URLSearchParams();
  if (typeof query === 'string') {
    qs.set('agent_id', query);
  } else if (query) {
    if (query.agentId) qs.set('agent_id', query.agentId);
    if (query.tenantId) qs.set('tenant_id', query.tenantId);
  }
  const text = qs.toString();
  return text ? `?${text}` : '';
}

function tenantQuerySuffix(tenantId?: string | null): string {
  return tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : '';
}

export const enterpriseApi = {
  /** LLM models */
  listLLMModels: (tenantId?: string) => get<LLMModel[]>(`/enterprise/llm-models${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  llmModels: (tenantId?: string) => get<LLMModel[]>(`/enterprise/llm-models${tenantId ? `?tenant_id=${tenantId}` : ''}`),
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
  getSecurityAuditEvents: (tenantId?: string) =>
    get<PaginatedResponse<SecurityAuditEvent>>(
      `/enterprise/audit?page_size=200${tenantId ? `&tenant_id=${encodeURIComponent(tenantId)}` : ''}`,
    ),

  /** Stats & quotas */
  getStats: (tenantId?: string) => get<EnterpriseStats>(`/enterprise/stats${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  getTenantQuotas: () => get<Record<string, unknown>>('/enterprise/tenant-quotas'),
  updateTenantQuotas: (data: Record<string, unknown>) => patch<void>('/enterprise/tenant-quotas', data),

  /** Memory config */
  getMemoryConfig: (tenantId?: string) =>
    get<MemoryConfig>(`/enterprise/memory/config${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  updateMemoryConfig: (data: MemoryConfig, tenantId?: string) =>
    put<MemoryConfig>(`/enterprise/memory/config${tenantId ? `?tenant_id=${tenantId}` : ''}`, data),

  /** Capability policies */
  listCapabilityDefinitions: () => get<CapabilityDefinition[]>('/enterprise/capabilities/definitions'),
  listCapabilityPolicies: (query?: CapabilityPolicyQuery) =>
    get<CapabilityPolicy[]>(`/enterprise/capabilities${capabilityPolicyQuerySuffix(query)}`),
  upsertCapabilityPolicy: (data: CapabilityPolicyUpdate, tenantId?: string | null) =>
    put<CapabilityPolicy>(`/enterprise/capabilities${tenantQuerySuffix(tenantId)}`, { conditions: {}, ...data }),
  deleteCapabilityPolicy: (policyId: string, tenantId?: string | null) =>
    del(`/enterprise/capabilities/${policyId}${tenantQuerySuffix(tenantId)}`),

  /** Invitation codes */
  listInvitationCodes: (params?: string) =>
    get<PaginatedResponse<InvitationCode>>(`/enterprise/invitation-codes${params ? `?${params}` : ''}`),
  createInvitationCode: (data: { max_uses?: number; count?: number }) =>
    post<InvitationCodeBatchResult>('/enterprise/invitation-codes', data),
  deleteInvitationCode: (id: string) => del(`/enterprise/invitation-codes/${id}`),
  exportInvitationCodesCsv: () => getBlob('/enterprise/invitation-codes/export'),

  /** System settings */
  getSetting: (key: string, tenantId?: string) =>
    get<SystemSetting>(`/enterprise/system-settings/${key}${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  updateSetting: (key: string, value: Record<string, unknown>, tenantId?: string) =>
    put<SystemSetting>(`/enterprise/system-settings/${key}${tenantId ? `?tenant_id=${tenantId}` : ''}`, { value }),

  /** Read-only recovery surface for files left by the retired shared-folder feature. */
  getLegacyCompanyFilesStatus: (tenantId?: string) =>
    get<LegacyCompanyFilesStatus>(`/enterprise/legacy-company-files/status${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  exportLegacyCompanyFiles: (tenantId?: string) =>
    getBlob(`/enterprise/legacy-company-files/export${tenantId ? `?tenant_id=${tenantId}` : ''}`),

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
  syncOrg: (tenantId?: string, provider?: 'dingtalk' | 'wecom', expectedUserId?: string) => {
    const query = new URLSearchParams();
    if (tenantId) query.set('tenant_id', tenantId);
    if (provider) query.set('provider', provider);
    if (expectedUserId) query.set('expected_user_id', expectedUserId);
    return post<Record<string, unknown>>(`/enterprise/org/sync${query.size ? `?${query}` : ''}`);
  },
  dryRunIdentityLinks: (tenantId?: string) =>
    post<IdentityDryRunReport>(
      `/enterprise/org/identity-links/dry-run${tenantId ? `?tenant_id=${tenantId}` : ''}`,
    ),
  listIdentityLinks: (tenantId?: string) =>
    get<IdentityLinkList>(`/enterprise/org/identity-links${tenantQuerySuffix(tenantId)}`),
  createIdentityLink: (body: IdentityLinkCreateBody, tenantId?: string) =>
    post<IdentityLinkResult>(`/enterprise/org/identity-links${tenantQuerySuffix(tenantId)}`, body),
  revokeIdentityLink: (linkId: string, body: IdentityLinkRevokeBody, tenantId?: string) =>
    post<IdentityLinkResult>(`/enterprise/org/identity-links/${linkId}/revoke${tenantQuerySuffix(tenantId)}`, body),
  replaceIdentityLink: (
    linkId: string,
    body: IdentityLinkRevokeBody & Omit<IdentityLinkCreateBody, 'org_member_id'>,
    tenantId?: string,
  ) => post<IdentityLinkResult>(
    `/enterprise/org/identity-links/${linkId}/replace${tenantQuerySuffix(tenantId)}`,
    body,
  ),
  previewIdentityBackfill: (tenantId?: string) =>
    post<IdentityBackfillPreview>(`/enterprise/org/identity-links/backfill/preview${tenantQuerySuffix(tenantId)}`),
  applyIdentityBackfill: (orgMemberIds: string[], tenantId?: string) =>
    post<IdentityCutoverResult>(
      `/enterprise/org/identity-links/backfill${tenantQuerySuffix(tenantId)}`,
      { org_member_ids: orgMemberIds },
    ),
  listDepartmentLinks: (tenantId?: string) =>
    get<DepartmentDirectoryReport>(`/enterprise/org/department-links${tenantQuerySuffix(tenantId)}`),
  applyDepartmentLinks: (selections: DepartmentLinkSelection[], tenantId?: string) =>
    post<IdentityCutoverResult>(`/enterprise/org/department-links${tenantQuerySuffix(tenantId)}`, { selections }),
  applyDepartmentCutover: (orgMemberIds: string[], tenantId?: string) =>
    post<IdentityCutoverResult>(`/enterprise/org/department-cutover${tenantQuerySuffix(tenantId)}`, { org_member_ids: orgMemberIds }),
  repairDepartmentAssignment: (userId: string, departmentId: string, tenantId?: string) =>
    post<IdentityCutoverResult>(
      `/enterprise/org/department-assignments/repair${tenantQuerySuffix(tenantId)}`,
      { user_id: userId, department_id: departmentId },
    ),
  listCutoverJournal: (tenantId?: string) =>
    get<CutoverJournalReport>(`/enterprise/org/cutover-journal${tenantQuerySuffix(tenantId)}`),
  rollbackCutover: (journalIds: string[], tenantId?: string) =>
    post<IdentityCutoverResult>(
      `/enterprise/org/cutover-journal/rollback${tenantQuerySuffix(tenantId)}`,
      { journal_ids: journalIds },
    ),

  /** Approvals */
  listApprovals: (tenantId?: string, businessTeamId?: string) => {
    const query = new URLSearchParams();
    if (tenantId) query.set('tenant_id', tenantId);
    if (businessTeamId) query.set('business_team_id', businessTeamId);
    return get<unknown[]>(`/enterprise/approvals${query.size ? `?${query}` : ''}`);
  },
  grantApprovalReviewer: (id: string, body: { request_id: string; user_id: string }) =>
    post<{ id: string; revoked: boolean }>(`/enterprise/approvals/${id}/reviewers`, body),
  revokeApprovalReviewer: (id: string, grantId: string) =>
    del<{ id: string; revoked: boolean }>(`/enterprise/approvals/${id}/reviewers/${grantId}`),
  resolveApproval: (id: string, data: { action: string }) =>
    post<unknown>(`/enterprise/approvals/${id}/resolve`, data),
};
