import { del, get, put } from '../core';

export interface TeamMemoryEntry {
  tenant_id: string;
  workspace_key: string;
  key: string;
  title: string;
  updated_at: string;
  revision?: number;
  checksum?: string;
  updated_by?: string;
  path: string;
  absolute_path: string;
  snippet?: string;
  content?: string;
  deleted?: boolean;
}

export interface TeamMemoryUpsertRequest {
  workspace_key: string;
  key: string;
  title: string;
  content: string;
  mode?: 'replace' | 'append';
  base_revision?: number | null;
}

function buildQueryString(params: Record<string, string | undefined>): string {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) {
      searchParams.set(key, value);
    }
  }
  const query = searchParams.toString();
  return query ? `?${query}` : '';
}

export const memoryApi = {
  listShared: (workspaceKey: string, tenantId?: string) =>
    get<TeamMemoryEntry[]>(`/enterprise/memory/shared${buildQueryString({ workspace_key: workspaceKey, tenant_id: tenantId })}`),
  getShared: (entryKey: string, workspaceKey: string, tenantId?: string) =>
    get<TeamMemoryEntry>(
      `/enterprise/memory/shared/${encodeURIComponent(entryKey)}${buildQueryString({ workspace_key: workspaceKey, tenant_id: tenantId })}`,
    ),
  searchShared: (workspaceKey: string, query: string, tenantId?: string) =>
    get<TeamMemoryEntry[]>(
      `/enterprise/memory/shared/search${buildQueryString({
        workspace_key: workspaceKey,
        query,
        tenant_id: tenantId,
      })}`,
    ),
  upsertShared: (data: TeamMemoryUpsertRequest, tenantId?: string) =>
    put<TeamMemoryEntry>(`/enterprise/memory/shared${buildQueryString({ tenant_id: tenantId })}`, data),
  deleteShared: (entryKey: string, workspaceKey: string, tenantId?: string) =>
    del<{ deleted: boolean; key: string; workspace_key: string }>(
      `/enterprise/memory/shared/${encodeURIComponent(entryKey)}${buildQueryString({ workspace_key: workspaceKey, tenant_id: tenantId })}`,
    ),
};
