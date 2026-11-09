import { del, get, put } from '../core';

export interface VaultRemoval { fallback: 'company' | 'configuration'; review_required?: boolean }
export interface VaultRow {
  kind: string; id: string; name: string; scope: 'personal' | 'bot' | 'company';
  configured: boolean; read_only: boolean; fields: Array<{ key: string; label: string }>; field?: string | null;
  version?: number | null; bots: Array<{ id: string; name: string }>; removal: VaultRemoval;
  connection_revision?: string;
}
export interface VaultChange { removed: boolean; removal: VaultRemoval }
const path = (row: VaultRow) => `/vault/${encodeURIComponent(row.kind)}/${encodeURIComponent(row.id)}${row.field ? `?field=${encodeURIComponent(row.field)}` : ''}`;
export const vaultApi = {
  list: () => get<VaultRow[]>('/vault'),
  replace: (row: VaultRow, values: Record<string, string>) => put<VaultChange>(path(row), { values, version: row.version ?? null,
    ...(row.connection_revision ? { connection_revision: row.connection_revision } : {}) }),
  remove: (row: VaultRow) => del<VaultChange>(path(row) + (row.version != null ? `${row.field ? '&' : '?'}version=${row.version}` : '')),
};
