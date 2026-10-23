/**
 * Tools domain adapter — agent tool configuration.
 *
 * Wraps the /api/tools/* endpoints used by AgentDetail.
 */

import { get, put, post, del } from '../core';
import type { McpAccount } from './extensions';
import type { EmployeeRoleSettings } from './agents';

export interface SessionRoleConfiguration extends EmployeeRoleSettings {
  target_agent_id: string;
  target_agent_name: string;
  state: 'waiting' | 'ready';
  receipt_ref?: string | null;
  replayed?: boolean;
}

export interface ToolProviderAuth {
  mode?: 'no_key_default' | 'optional_key' | 'key_required' | string;
  keyless_supported?: boolean;
  credential_optional?: boolean;
  key_required?: boolean;
  label?: string;
  description?: string;
}

export interface ToolCatalogRow {
  id: string;
  name?: string;
  display_name?: string;
  description?: string;
  type?: string;
  category?: string;
  enabled?: boolean;
  is_default?: boolean;
  config?: Record<string, unknown>;
  config_schema?: { fields?: Array<Record<string, unknown>> };
  governance_taxonomy?: Record<string, unknown> | null;
  mcp_server_name?: string | null;
  mcp_tool_name?: string | null;
  mcp_server_url?: string | null;
  mcp_metadata_fingerprint?: string | null;
  tenant_id?: string | null;
  provider_auth?: ToolProviderAuth | null;
}

export interface AgentTool {
  id: string;
  tool_id: string;
  name: string;
  display_name: string;
  description: string;
  type: string;
  category: string;
  enabled: boolean;
  config: Record<string, unknown>;
  global_config?: Record<string, unknown>;
  agent_config?: Record<string, unknown>;
  config_schema?: { fields?: Array<Record<string, unknown>> };
  mcp_server_name?: string | null;
  mcp_tool_name?: string | null;
  provider_auth?: ToolProviderAuth | null;
}

export interface ToolDetail {
  id: string;
  name: string;
  display_name: string;
  description: string;
  parameters_schema: Record<string, unknown>;
  config: Record<string, unknown>;
  config_schema: Record<string, unknown>;
  provider_auth?: ToolProviderAuth | null;
}

export interface SessionToolConfiguration {
  state: 'waiting' | 'saved';
  receipt_ref: string | null;
  target_agent_id: string;
  target_agent_name: string;
  replayed?: boolean;
  tool: AgentTool;
  can_manage_company_mailbox?: boolean;
  can_manage_company_api_account?: boolean;
  api_account_selection?: SessionToolConfiguration['mailbox_selection'];
  mailbox_selection?: { account_id: string | null; account_version?: number | null; grant_version?: number | null } | null;
}

export interface ToolAccount {
  connection_revision?: string;
  connection?: { base_url: string; method: string; path: string };
  id: string | null;
  owner_user_id: string | null;
  name: string;
  version?: number;
  revision?: string;
  is_configured: boolean;
  account_use_enabled: boolean;
  grant_version?: number;
  config?: Record<string, unknown>;
  config_schema?: AgentTool['config_schema'];
}

export interface SessionChannelConfiguration {
  state: 'waiting' | 'ready';
  receipt_ref?: string | null;
  replayed?: boolean;
  target_agent_id: string;
  target_agent_name: string;
  configuration: { id: string; updated_at: string; channel_type: string; scope: 'employee' | 'company' } | null;
}

export interface SessionMcpConfiguration {
  state: 'waiting' | 'ready';
  receipt_ref?: string | null;
  replayed?: boolean;
  target_agent_id: string;
  target_agent_name: string;
  server: { id: string; name: string };
  revision: string;
  authorization: 'configured' | 'pending' | 'unknown' | 'required';
  usage: { enabled: boolean; default_tool_mode: string; always_load: boolean };
  usable_tool_count: number;
  can_authorize: boolean;
  account?: McpAccount;
  accounts?: McpAccount[];
  account_use_enabled?: boolean;
  can_grant_account?: boolean;
  can_manage_employee?: boolean;
  selection_unavailable?: boolean;
  permitted_tool_count?: number;
}

export interface CategoryConfig {
  [key: string]: unknown;
}

export interface FeishuRuntimeStatus {
  ok: boolean;
  scope: 'global' | 'agent';
  message: string;
  cli_enabled: boolean;
  cli_available: boolean;
  cli_bin: string;
  docs_read_ready: boolean;
  base_tasks_ready: boolean;
  cardkit_dependency_ready?: boolean;
  cardkit_verified?: boolean | null;
  cardkit_last_error?: string | null;
  cardkit_probe_supported?: boolean;
  cardkit_ready?: boolean;
  tenant_channel_configured?: boolean;
  channel_configured?: boolean;
  office_access?: boolean;
}

export const toolsApi = {
  /** Global tool catalog */
  listCatalog: (tenantId?: string) => get<ToolCatalogRow[]>(`/tools${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  listAgentInstalled: (tenantId?: string) =>
    get<unknown[]>(`/tools/agent-installed${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  createTool: (data: Record<string, unknown>) => post<unknown>('/tools', data),
  updateGlobalTool: (toolId: string, data: Record<string, unknown>) => put<unknown>(`/tools/${toolId}`, data),
  deleteGlobalTool: (toolId: string) => del(`/tools/${toolId}`),
  testMcp: (data: Record<string, unknown>) => post<unknown>('/tools/test-mcp', data),

  /** List tools with per-agent config (preferred), falls back to basic list */
  listWithConfig: (agentId: string) => get<AgentTool[]>(`/tools/agents/${agentId}/with-config`),
  list: (agentId: string) => get<AgentTool[]>(`/tools/agents/${agentId}`),

  /** Toggle tool enabled/disabled */
  updateTools: (agentId: string, data: { tools: Array<{ tool_id: string; enabled: boolean }> }) =>
    put<void>(`/tools/agents/${agentId}`, data),

  /** Category-level config (e.g., email, feishu) */
  getCategoryConfig: (agentId: string, category: string) =>
    get<CategoryConfig>(`/tools/agents/${agentId}/category-config/${category}`),
  updateCategoryConfig: (agentId: string, category: string, config: Record<string, unknown>) =>
    put<void>(`/tools/agents/${agentId}/category-config/${category}`, config),
  testCategory: (agentId: string, category: string) =>
    post<FeishuRuntimeStatus | { success: boolean }>(`/tools/agents/${agentId}/category-config/${category}/test`),
  getFeishuRuntimeStatus: () => get<FeishuRuntimeStatus>('/tools/runtime/feishu-status'),
  getAgentFeishuRuntimeStatus: (agentId: string) => get<FeishuRuntimeStatus>(`/tools/agents/${agentId}/runtime/feishu-status`),

  /** Per-tool config */
  updateToolConfig: (agentId: string, toolId: string, config: Record<string, unknown>) =>
    put<{ ok: boolean; tool: AgentTool }>(`/tools/agents/${agentId}/tool-config/${toolId}`, { config }),
  listApiAccounts: (agentId: string, toolId: string) => get<ToolAccount[]>(`/agents/${agentId}/custom-api-connectors/${toolId}/accounts`),
  createApiAccount: (agentId: string, toolId: string, id: string, name: string) => post<ToolAccount>(`/agents/${agentId}/custom-api-connectors/${toolId}/accounts`, { id, name }),
  getApiAccount: (id: string) => get<ToolAccount>(`/custom-api-accounts/${id}`),
  saveApiAccount: (id: string, expectedVersion: number, config: Record<string, unknown>, connectionRevision: string) =>
    put<ToolAccount>(`/custom-api-accounts/${id}`, { expected_version: expectedVersion, config, connection_revision: connectionRevision }),
  setApiAccountGrant: (id: string, agentId: string, requesterId: string, enabled: boolean, expectedVersion: number) =>
    put<{ enabled: boolean; version: number }>(`/custom-api-accounts/${id}/grants/${agentId}/${requesterId}`, { enabled, expected_version: expectedVersion }),
  revokeApiAccount: (id: string, expectedVersion: number) => post<ToolAccount>(`/custom-api-accounts/${id}/revoke`, { expected_version: expectedVersion }),
  confirmSessionApiAccount: (agentId: string, sessionId: string, questionId: string, account: ToolAccount) =>
    post<SessionToolConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/configuration/api-account`, {
      account_id: account.id, account_version: account.version, grant_version: account.grant_version || undefined, company_revision: account.revision,
    }),
  listMailboxes: (agentId: string) => get<ToolAccount[]>(`/agents/${agentId}/email-accounts`),
  createMailbox: (agentId: string, id: string, name: string) => post<ToolAccount>(`/agents/${agentId}/email-accounts`, { id, name }),
  getMailbox: (id: string) => get<ToolAccount>(`/email-accounts/${id}`),
  saveMailbox: (id: string, expectedVersion: number, config: Record<string, unknown>, sourceAgentId: string) =>
    put<ToolAccount>(`/email-accounts/${id}`, { expected_version: expectedVersion, config, source_agent_id: sourceAgentId }),
  setMailboxGrant: (id: string, agentId: string, requesterId: string, enabled: boolean, expectedVersion: number) =>
    put<{ enabled: boolean; version: number }>(`/email-accounts/${id}/grants/${agentId}/${requesterId}`, { enabled, expected_version: expectedVersion }),
  revokeMailbox: (id: string, expectedVersion: number) => post<ToolAccount>(`/email-accounts/${id}/revoke`, { expected_version: expectedVersion }),
  confirmSessionMailbox: (agentId: string, sessionId: string, questionId: string, account: ToolAccount) =>
    post<SessionToolConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/configuration/mailbox`, {
      account_id: account.id, account_version: account.version, grant_version: account.grant_version || undefined, company_revision: account.revision,
    }),
  getSessionToolConfiguration: (agentId: string, sessionId: string, questionId: string) =>
    get<SessionToolConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/configuration`),
  getSessionRoleConfiguration: (agentId: string, sessionId: string, questionId: string) =>
    get<SessionRoleConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/role`),
  confirmSessionRoleConfiguration: (agentId: string, sessionId: string, questionId: string, roleRevision: string) =>
    post<SessionRoleConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/role/confirm`, { role_revision: roleRevision }),
  saveSessionToolConfiguration: (agentId: string, sessionId: string, questionId: string, config: Record<string, unknown>) =>
    put<SessionToolConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/configuration`, { config }),
  getSessionMcpConfiguration: (agentId: string, sessionId: string, questionId: string, accountId?: string) =>
    get<SessionMcpConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/mcp${accountId ? `?account_id=${encodeURIComponent(accountId)}` : ''}`),
  selectSessionMcpAccount: (agentId: string, sessionId: string, questionId: string, accountId: string) =>
    post<SessionMcpConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/mcp/select`, { account_id: accountId }),
  confirmSessionMcpConfiguration: (agentId: string, sessionId: string, questionId: string, data: { revision: string; account_id?: string }) =>
    post<SessionMcpConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/mcp/confirm`, data),
  getSessionChannelConfiguration: (agentId: string, sessionId: string, questionId: string) =>
    get<SessionChannelConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/channel`),
  confirmSessionChannelConfiguration: (agentId: string, sessionId: string, questionId: string, data: { config_id: string; updated_at: string }) =>
    post<SessionChannelConfiguration>(`/tools/agents/${agentId}/sessions/${sessionId}/questions/${questionId}/channel/confirm`, data),

  /** Single tool detail */
  getDetail: (toolId: string) => get<ToolDetail>(`/tools/agent-tool/${toolId}`),
  removeAgentTool: (toolId: string) => del(`/tools/agent-tool/${toolId}`),

  /** Test email */
  testEmail: (data: Record<string, unknown>) => post<{ success: boolean }>('/tools/test-email', data),
};
