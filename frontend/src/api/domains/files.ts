/**
 * Files domain adapter — agent workspace file management.
 */

import { del, get, getBlob, post, put, upload } from '../core';
import { uploadFileWithProgress } from '../core/upload-progress';

export interface FileInfo {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size: number;
  is_dir: boolean;
  [key: string]: unknown;
}

export interface FileContent {
  path: string;
  content: string;
  uses_snapshot?: boolean;
  legacy_current_file_fallback?: boolean;
  workspace_changed?: boolean;
  snapshot_hash?: string | null;
  content_hash?: string | null;
  authority_source?: string | null;
  operator_view?: boolean;
}

export type FileVersionState = 'available' | 'deleted' | 'unavailable';

export interface FileVersionCurrentState {
  exists: boolean;
  content_hash: string | null;
  size: number;
}

export interface FileVersionSummary {
  version_id: string;
  created_at: string;
  state: FileVersionState;
  size: number;
  content_hash: string | null;
  restorable: boolean;
}

export interface FileVersionPage {
  path: string;
  current: FileVersionCurrentState;
  versions: FileVersionSummary[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
  coverage_complete: boolean;
}

export interface FileVersionContent {
  path: string;
  version_id: string;
  state: FileVersionState;
  content: string | null;
  content_hash: string | null;
  size: number;
  is_binary: boolean;
}

export interface FileVersionRestoreRequest {
  expected_current_exists: boolean;
  expected_current_hash: string | null;
}

export interface FileVersionRestoreResult {
  status: 'restored' | 'unchanged';
  path: string;
  version_id: string;
  current: FileVersionCurrentState;
}

export interface ResourceAuthorityOptions {
  operatorView?: boolean;
  reason?: string;
}

function authorityParams(authority?: ResourceAuthorityOptions): URLSearchParams {
  const params = new URLSearchParams();
  if (authority?.operatorView) {
    const reason = String(authority.reason || '').trim();
    if (!reason) throw new Error('Operator View requires an audit reason');
    params.set('operator_view', 'true');
    params.set('operator_reason', reason);
  }
  return params;
}

function withAuthority(path: string, authority?: ResourceAuthorityOptions): string {
  const params = authorityParams(authority);
  if (!params.size) return path;
  return `${path}${path.includes('?') ? '&' : '?'}${params.toString()}`;
}

export const fileApi = {
  list: (agentId: string, path?: string, authority?: ResourceAuthorityOptions) => {
    const params = authorityParams(authority);
    if (path) params.set('path', path);
    const qs = params.size ? `?${params.toString()}` : '';
    return get<FileInfo[]>(`/agents/${agentId}/files/${qs}`);
  },
  read: (agentId: string, path: string, authority?: ResourceAuthorityOptions) =>
    get<FileContent>(withAuthority(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, authority)),
  versions: (
    agentId: string,
    path: string,
    offset = 0,
    limit = 20,
    authority?: ResourceAuthorityOptions,
  ) =>
    get<FileVersionPage>(withAuthority(
      `/agents/${agentId}/files/versions?path=${encodeURIComponent(path)}&offset=${offset}&limit=${limit}`,
      authority,
    )),
  readVersion: (
    agentId: string,
    path: string,
    versionId: string,
    authority?: ResourceAuthorityOptions,
  ) =>
    get<FileVersionContent>(withAuthority(
      `/agents/${agentId}/files/versions/${encodeURIComponent(versionId)}/content?path=${encodeURIComponent(path)}`,
      authority,
    )),
  restoreVersion: (
    agentId: string,
    path: string,
    versionId: string,
    request: FileVersionRestoreRequest,
    authority?: ResourceAuthorityOptions,
  ) =>
    post<FileVersionRestoreResult>(withAuthority(
      `/agents/${agentId}/files/versions/${encodeURIComponent(versionId)}/restore?path=${encodeURIComponent(path)}`,
      authority,
    ), request),
  downloadVersion: (
    agentId: string,
    path: string,
    versionId: string,
    authority?: ResourceAuthorityOptions,
  ) =>
    getBlob(withAuthority(
      `/agents/${agentId}/files/versions/${encodeURIComponent(versionId)}/download?path=${encodeURIComponent(path)}`,
      authority,
    )),
  readArtifact: (agentId: string, artifactId: string, authority?: ResourceAuthorityOptions) =>
    get<FileContent>(withAuthority(
      `/agents/${agentId}/files/artifacts/${encodeURIComponent(artifactId)}/content`,
      authority,
    )),
  write: (agentId: string, path: string, content: string, authority?: ResourceAuthorityOptions) =>
    put<void>(withAuthority(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, authority), { content }),
  remove: (agentId: string, path: string, authority?: ResourceAuthorityOptions) =>
    del(withAuthority(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, authority)),
  delete: (agentId: string, path: string, authority?: ResourceAuthorityOptions) =>
    del(withAuthority(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, authority)),
  download: (agentId: string, path: string, authority?: ResourceAuthorityOptions) =>
    getBlob(withAuthority(`/agents/${agentId}/files/download?path=${encodeURIComponent(path)}`, authority)),
  downloadArtifact: (agentId: string, artifactId: string, authority?: ResourceAuthorityOptions) =>
    getBlob(withAuthority(
      `/agents/${agentId}/files/artifacts/${encodeURIComponent(artifactId)}/download`,
      authority,
    )),
  upload: (agentId: string, file: File, path?: string, onProgress?: (pct: number) => void, authority?: ResourceAuthorityOptions) => {
    const params = authorityParams(authority);
    if (path) params.set('path', path);
    const suffix = params.size ? `?${params.toString()}` : '';
    if (onProgress) {
      return uploadFileWithProgress(`/agents/${agentId}/files/upload${suffix}`, file, onProgress).promise;
    }
    return upload<any>(withAuthority(`/agents/${agentId}/files/upload`, authority), file, path ? { path } : undefined);
  },
  importSkill: (agentId: string, skillId: string) =>
    post<any>(`/agents/${agentId}/files/import-skill`, { skill_id: skillId }),
  importFromClawHub: (agentId: string, slug: string) =>
    post<any>(`/agents/${agentId}/files/import-from-clawhub`, { slug }),
  importFromUrl: (agentId: string, url: string) =>
    post<any>(`/agents/${agentId}/files/import-from-url`, { url }),
};
