/**
 * Tools domain adapter — agent tool configuration.
 *
 * Wraps the /api/tools/* endpoints used by AgentDetail.
 */

import { get, put, post, del } from '../core';

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
}

export interface ToolDetail {
  id: string;
  name: string;
  display_name: string;
  description: string;
  parameters_schema: Record<string, unknown>;
  config: Record<string, unknown>;
  config_schema: Record<string, unknown>;
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
  channel_configured?: boolean;
  office_access?: boolean;
}

export const toolsApi = {
  /** Global tool catalog */
  listCatalog: (tenantId?: string) => get<unknown[]>(`/tools${tenantId ? `?tenant_id=${tenantId}` : ''}`),
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
    put<void>(`/tools/agents/${agentId}/tool-config/${toolId}`, { config }),

  /** Single tool detail */
  getDetail: (toolId: string) => get<ToolDetail>(`/tools/agent-tool/${toolId}`),
  removeAgentTool: (toolId: string) => del(`/tools/agent-tool/${toolId}`),

  /** Test email */
  testEmail: (data: Record<string, unknown>) => post<{ success: boolean }>('/tools/test-email', data),
};
