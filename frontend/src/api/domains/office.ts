import { getBlob, post } from '../core';
import type { ResourceAuthorityOptions } from './files';

export type OfficeKind = 'docx' | 'xlsx' | 'pptx';
export interface OfficeDocumentCreateInput {
  path: string;
  kind: OfficeKind;
  template_path?: string;
}

export interface OfficeDocumentCreateResponse {
  status: 'ok';
  path: string;
  kind: OfficeKind;
  size: number;
}

function authorityParams(authority?: ResourceAuthorityOptions): URLSearchParams {
  const params = new URLSearchParams();
  if (authority?.operatorView) {
    params.set('operator_view', 'true');
    params.set('operator_reason', authority.reason || 'Agent workspace administration');
  }
  return params;
}

export const officeApi = {
  createDocument: (agentId: string, data: OfficeDocumentCreateInput) =>
    post<OfficeDocumentCreateResponse>(`/agents/${agentId}/office/documents`, data),

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
