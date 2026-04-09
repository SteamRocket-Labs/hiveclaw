/**
 * Files domain adapter — agent workspace file management.
 */

import { get, post, put, del, upload } from '../core';
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
}

export const fileApi = {
  list: (agentId: string, path?: string) => {
    const qs = path ? `?path=${encodeURIComponent(path)}` : '';
    return get<FileInfo[]>(`/agents/${agentId}/files/${qs}`);
  },
  read: (agentId: string, path: string) =>
    get<FileContent>(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`),
  write: (agentId: string, path: string, content: string) =>
    put<void>(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`, { content }),
  remove: (agentId: string, path: string) =>
    del(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`),
  delete: (agentId: string, path: string) =>
    del(`/agents/${agentId}/files/content?path=${encodeURIComponent(path)}`),
  downloadUrl: (agentId: string, path: string) => {
    const token = localStorage.getItem('token');
    return `/api/agents/${agentId}/files/download?path=${encodeURIComponent(path)}&token=${token}`;
  },
  upload: (agentId: string, file: File, path?: string, onProgress?: (pct: number) => void) => {
    if (onProgress) {
      return uploadFileWithProgress(`/agents/${agentId}/files/upload${path ? `?path=${encodeURIComponent(path)}` : ''}`, file, onProgress).promise;
    }
    return upload<any>(`/agents/${agentId}/files/upload`, file, path ? { path } : undefined);
  },
  download: (agentId: string, path: string) =>
    get<Blob>(`/agents/${agentId}/files/download?path=${encodeURIComponent(path)}`),
  importSkill: (agentId: string, skillId: string) =>
    post<any>(`/agents/${agentId}/files/import-skill`, { skill_id: skillId }),
  importFromClawHub: (agentId: string, slug: string) =>
    post<any>(`/agents/${agentId}/files/import-from-clawhub`, { slug }),
  importFromUrl: (agentId: string, url: string) =>
    post<any>(`/agents/${agentId}/files/import-from-url`, { url }),
};
