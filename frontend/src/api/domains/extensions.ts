/**
 * Agent extensions domain adapter — Skills + MCP Servers (server-first).
 *
 * Replaces the legacy pack surface. `/agents/{id}/extensions` is the single
 * source of truth for Agent Detail extension state; the enterprise routes manage
 * tenant MCP server records with stable server identity. No DTO here carries a
 * `pack` / `pack_name` field. See docs/agent-extension-surface-skill-mcp.md
 * §7.2–7.4, §8.4.
 */

import { del, get, post, put } from '../core';

export type McpToolMode = 'auto' | 'approval' | 'deny';

export interface ExtensionSkill {
  id: string;
  name: string;
  source: string;
  status: string;
}

/** One MCP server as seen from a single agent (server-level controls). */
export interface AgentMcpServer {
  id: string;
  name: string;
  status: string;
  enabled: boolean;
  tool_count: number;
  default_tool_mode: McpToolMode | string;
  always_load: boolean;
}

export interface AgentMcpServerTool {
  tool_id: string | null;
  tool_name: string;
  display_name: string;
  mode: McpToolMode;
  effective_mode: McpToolMode;
}

export interface AgentExtensions {
  skills: ExtensionSkill[];
  mcp_servers: AgentMcpServer[];
  plugins?: Array<InstalledPlugin & { enabled: boolean }>;
  external_activations?: ExternalExtensionActivationSummary[];
}

/** Tenant-managed MCP server record (company admin, server-first). */
export interface McpServerRecord {
  id: string;
  name: string;
  server_key: string;
  status: string;
  auth_status: string;
  transport: string;
  tool_count: number;
  agent_count: number;
  agents: Array<{ id: string; name: string; enabled: boolean }>;
}

export interface McpAssignmentResult {
  id: string;
  agent_id: string;
  server_id: string;
  enabled: boolean;
  default_tool_mode: McpToolMode | string;
  always_load: boolean;
}

export interface McpBackfillSummary {
  [key: string]: unknown;
}

export interface InstalledPlugin {
  id: string;
  plugin_key: string;
  version: string;
  status: string;
  source_kind: string;
  lockfile?: Record<string, unknown>;
}

export interface AgentPluginAssignmentResult {
  id: string;
  agent_id: string;
  plugin_key: string;
  enabled: boolean;
}

/** Tenant MCP import request: a direct URL or a Smithery server id. */
export interface McpImportRequest {
  server_id?: string;
  mcp_url?: string;
  server_name?: string;
  config?: Record<string, unknown>;
}

export interface ExternalCapabilityReviewSummary {
  id: string;
  source_format: string;
  source_uri: string;
  source_ref?: string | null;
  source_hash?: string;
  normalized_name: string;
  status: string;
  admission_class?: string;
  admission_report?: Record<string, unknown>;
  governance_projection?: Record<string, unknown>;
  normalized_manifest?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
}

export interface ExternalCapabilitySnapshotSummary {
  id: string;
  review_id?: string;
  snapshot_key?: string;
  source_format: string;
  source_uri: string;
  source_ref?: string | null;
  source_hash?: string;
  normalized_name: string;
  status: string;
  admission_class?: string;
  admission_report?: Record<string, unknown>;
  governance_projection?: Record<string, unknown>;
  component_manifest?: Record<string, unknown>;
  catalog_entries?: ExternalExtensionCatalogEntry[];
  approved_at?: string;
  created_at?: string;
}

export interface ExternalCapabilityReviewResult extends ExternalCapabilityReviewSummary {
  snapshot?: ExternalCapabilitySnapshotSummary | null;
}

export interface ExternalExtensionActivationSummary {
  id: string;
  snapshot_id: string;
  status: string;
  activation_scope?: string;
  session_id?: string | null;
  normalized_name?: string;
  source_format?: string;
  source_uri?: string;
  component_types?: Record<string, unknown>;
  selected_components?: string[];
  activation_result?: Record<string, unknown>;
  activated_at?: string;
  expires_at?: string | null;
}

export interface ExternalExtensionActivationResult {
  activation: ExternalExtensionActivationSummary;
  snapshot: ExternalCapabilitySnapshotSummary;
  result: Record<string, unknown>;
}

export interface ExternalExtensionActivationRequest {
  component_qualified_names?: string[] | null;
  credential_handles?: Record<string, string>;
}

export interface ExternalExtensionTryInChatRequest extends ExternalExtensionActivationRequest {
  session_id: string;
  expires_in_minutes?: number;
}

export interface ExternalExtensionCatalogEntry {
  id: string;
  tenant_id?: string;
  snapshot_id: string;
  component_type: string;
  component_name: string;
  qualified_name: string;
  policy: string;
  status: string;
  source_format?: string;
  source_uri?: string;
  metadata?: Record<string, unknown>;
}

export interface ExternalMarketplaceSource {
  id: string;
  name: string;
  source_type: string;
  source_uri: string;
  status: string;
  sync_status?: string;
  last_sync_error?: string | null;
  config?: Record<string, unknown>;
  last_sync_at?: string | null;
}

export interface ExternalMarketplaceSourceRequest {
  name: string;
  source_type: string;
  source_uri: string;
  status?: string;
  config?: Record<string, unknown>;
}

export interface ExternalMarketplaceEntry {
  id: string;
  source_id: string | null;
  external_key: string;
  display_name: string;
  description?: string | null;
  source_format: string;
  source_uri: string;
  source_ref?: string | null;
  status: string;
  manifest?: Record<string, unknown>;
  compatibility?: Record<string, unknown>;
  review_id?: string | null;
  snapshot_id?: string | null;
  last_seen_at?: string | null;
}

export const extensionsApi = {
  /** Agent Detail source of truth: skills + MCP servers for one agent. */
  getAgentExtensions: (agentId: string) => get<AgentExtensions>(`/agents/${agentId}/extensions`),

  /** MCP servers assigned to one agent (server-level rollup). */
  getAgentMcpServers: (agentId: string) => get<AgentMcpServer[]>(`/agents/${agentId}/mcp-servers`),

  /** Per-tool policy modes for one assigned MCP server. */
  getAgentMcpServerTools: (agentId: string, serverId: string) =>
    get<AgentMcpServerTool[]>(`/agents/${agentId}/mcp-servers/${serverId}/tools`),

  /** Assign or update one agent's connection to an MCP server. */
  setAgentMcpAssignment: (
    agentId: string,
    serverId: string,
    data: { enabled: boolean; default_tool_mode?: McpToolMode | string; always_load?: boolean },
  ) => put<McpAssignmentResult>(`/agents/${agentId}/mcp-servers/${serverId}`, data),

  /** Set one advanced per-tool policy override for an assigned MCP server. */
  setAgentMcpToolPolicy: (
    agentId: string,
    serverId: string,
    toolName: string,
    data: { mode: McpToolMode },
  ) =>
    put<AgentMcpServerTool>(
      `/agents/${agentId}/mcp-servers/${serverId}/tools/${encodeURIComponent(toolName)}/policy`,
      data,
    ),

  /** Company admin: tenant MCP server records, server-first with stable identity. */
  listEnterpriseMcpServers: () => get<McpServerRecord[]>('/enterprise/mcp-servers'),

  /** Company admin: stage an external MCP server through Trust Gate review. */
  importEnterpriseMcpServer: (body: McpImportRequest) =>
    post<ExternalCapabilityReviewResult>('/enterprise/mcp-servers/import', body),

  /** Company admin: list Trust Gate review records for external capabilities. */
  listExternalCapabilityReviews: () =>
    get<ExternalCapabilityReviewSummary[]>('/enterprise/external-capabilities/reviews'),

  /** Company admin: list approved external capability catalog entries. */
  listExternalExtensionCatalog: () =>
    get<ExternalExtensionCatalogEntry[]>('/enterprise/external-capabilities/catalog'),

  /** Company admin: list discovery sources. Marketplace sources never activate runtime directly. */
  listMarketplaceSources: () =>
    get<ExternalMarketplaceSource[]>('/enterprise/external-capabilities/marketplace-sources'),

  /** Company admin: create one discovery source. */
  createMarketplaceSource: (body: ExternalMarketplaceSourceRequest) =>
    post<ExternalMarketplaceSource>('/enterprise/external-capabilities/marketplace-sources', body),

  /** Company admin: sync one discovery source into marketplace entries. */
  syncMarketplaceSource: (sourceId: string) =>
    post<{ source_id: string; sync_status?: string; entries_seen?: number; entries_created?: number; entries_updated?: number }>(
      `/enterprise/external-capabilities/marketplace-sources/${sourceId}/sync`,
    ),

  /** Company admin: list discoverable marketplace entries. */
  listMarketplaceEntries: () =>
    get<ExternalMarketplaceEntry[]>('/enterprise/external-capabilities/marketplace-entries'),

  /** Company admin: submit one marketplace entry into Trust Gate review. */
  submitMarketplaceEntryForReview: (entryId: string) =>
    post<{ entry: ExternalMarketplaceEntry; review: ExternalCapabilityReviewSummary }>(
      `/enterprise/external-capabilities/marketplace-entries/${entryId}/submit-review`,
    ),

  /** Company admin: approve one staged external capability snapshot. */
  approveExternalCapabilityReview: (reviewId: string) =>
    post<ExternalCapabilityReviewResult>(`/enterprise/external-capabilities/reviews/${reviewId}/approve`),

  /** Company admin: reject one staged external capability review. */
  rejectExternalCapabilityReview: (reviewId: string, body: { reason?: string | null } = {}) =>
    post<{ review_id: string; status: string; reason?: string }>(
      `/enterprise/external-capabilities/reviews/${reviewId}/reject`,
      body,
    ),

  /** Company admin: revoke one approved external capability snapshot. */
  revokeExternalCapabilitySnapshot: (snapshotId: string) =>
    post<{ snapshot_id: string; status: string; catalog_entries_revoked?: number; activations_revoked?: number }>(
      `/enterprise/external-capabilities/snapshots/${snapshotId}/revoke`,
    ),

  /** Agent owner/admin: list approved catalog entries available to this agent. */
  listAgentExternalExtensionCatalog: (agentId: string) =>
    get<ExternalExtensionCatalogEntry[]>(`/agents/${agentId}/external-extensions/catalog`),

  /** Agent owner/admin: activate one approved external snapshot for this agent. */
  activateExternalExtension: (
    agentId: string,
    snapshotId: string,
    body?: ExternalExtensionActivationRequest,
  ) =>
    body
      ? post<ExternalExtensionActivationResult>(`/agents/${agentId}/external-extensions/${snapshotId}/activate`, body)
      : post<ExternalExtensionActivationResult>(`/agents/${agentId}/external-extensions/${snapshotId}/activate`),

  /** Agent owner/admin: temporarily activate one approved external snapshot inside one chat session. */
  tryExternalExtensionInChat: (agentId: string, snapshotId: string, body: ExternalExtensionTryInChatRequest) =>
    post<ExternalExtensionActivationResult>(`/agents/${agentId}/external-extensions/${snapshotId}/try`, body),

  /** Agent owner/admin: deactivate one active external snapshot for this agent. */
  deactivateExternalExtension: (agentId: string, snapshotId: string) =>
    post<ExternalExtensionActivationResult>(`/agents/${agentId}/external-extensions/${snapshotId}/deactivate`),

  /** Company admin: delete one tenant MCP server record by its stable id. */
  deleteEnterpriseMcpServer: (serverId: string) =>
    del<{ status: string; server_id: string }>(`/enterprise/mcp-servers/${serverId}`),

  /** Company admin: trigger the MCP backfill for the current tenant (idempotent). */
  backfillEnterpriseMcpServers: () => post<McpBackfillSummary>('/enterprise/mcp-servers/backfill'),

  /** Agent-scoped plugin enable/disable. */
  setAgentPluginAssignment: (agentId: string, pluginKey: string, data: { enabled: boolean }) =>
    put<AgentPluginAssignmentResult>(`/agents/${agentId}/plugins/${encodeURIComponent(pluginKey)}`, data),
};
