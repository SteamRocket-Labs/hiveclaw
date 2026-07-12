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

export interface ResourceAuthorityOptions {
  operatorView?: boolean;
  reason?: string;
}

function authorityParams(authority?: ResourceAuthorityOptions): URLSearchParams {
  const params = new URLSearchParams();
  if (authority?.operatorView) {
    params.set('operator_view', 'true');
    params.set('operator_reason', authority.reason || 'Agent workspace administration');
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
