import { getBlob } from '../core';
import type { ResourceAuthorityOptions } from './files';

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
