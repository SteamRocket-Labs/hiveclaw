import { get, getBlob, put } from '../core';
import type { ResourceAuthorityOptions } from './files';

export interface OfficeContentField { node_path: string; value: string; name: string }
export interface OfficeContentModel {
  path: string;
  content_hash: string;
  kind: 'docx' | 'xlsx' | 'pptx';
  read_only?: boolean;
  fields: OfficeContentField[];
}
export interface OfficeContentChanges {
  expected_current_hash: string;
  changes: Array<{ node_path: string; value: string }>;
}
export interface OfficeContentReceipt { status: string; path: string; content_hash: string; content_revision?: number; validation_issue_count?: number | null }

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

export const officeApi = {
  readContent: (agentId: string, path: string) =>
    get<OfficeContentModel>(`/agents/${agentId}/office/content?${new URLSearchParams({ path })}`),
  writeContent: (agentId: string, path: string, changes: OfficeContentChanges) =>
    put<OfficeContentReceipt>(`/agents/${agentId}/office/content?${new URLSearchParams({ path })}`, changes),
  getWorkspacePreview: (agentId: string, path: string, authority?: ResourceAuthorityOptions) => {
    const params = authorityParams(authority);
    params.set('path', path);
    return getBlob(`/agents/${agentId}/office/preview?${params.toString()}`);
  },

  getArtifactPreview: (agentId: string, artifactId: string, authority?: ResourceAuthorityOptions) => {
    const params = authorityParams(authority);
    const query = params.size ? `?${params.toString()}` : '';
    return getBlob(`/agents/${agentId}/office/artifacts/${encodeURIComponent(artifactId)}/preview${query}`);
  },
};
