import { get, post } from '../core';

export interface ExternalPrincipal {
  id: string;
  provider: string;
  installation_ref: string;
  channel_config_id: string | null;
  subject_id: string;
  display_name: string;
  linked_user_id: string | null;
  status: 'active' | 'revoked';
  first_seen_at: string;
  last_seen_at: string;
  linked_at: string | null;
  revoked_at: string | null;
}

export interface ExternalPrincipalListOptions {
  tenantId?: string;
  provider?: string;
  status?: 'active' | 'revoked';
  linked?: boolean;
}

const tenantSuffix = (tenantId?: string) => {
  const params = new URLSearchParams();
  if (tenantId) params.set('tenant_id', tenantId);
  const query = params.toString();
  return query ? `?${query}` : '';
};

export const externalPrincipalsApi = {
  list: (options: ExternalPrincipalListOptions = {}) => {
    const params = new URLSearchParams();
    if (options.tenantId) params.set('tenant_id', options.tenantId);
    if (options.provider) params.set('provider', options.provider);
    if (options.status) params.set('status', options.status);
    if (options.linked !== undefined) params.set('linked', String(options.linked));
    const query = params.toString();
    return get<ExternalPrincipal[]>(`/enterprise/external-principals${query ? `?${query}` : ''}`);
  },
  link: (principalId: string, userId: string, reason: string, tenantId?: string) =>
    post<ExternalPrincipal>(
      `/enterprise/external-principals/${encodeURIComponent(principalId)}/link${tenantSuffix(tenantId)}`,
      { user_id: userId, reason },
    ),
  unlink: (principalId: string, reason: string, tenantId?: string) =>
    post<ExternalPrincipal>(
      `/enterprise/external-principals/${encodeURIComponent(principalId)}/unlink${tenantSuffix(tenantId)}`,
      { reason },
    ),
};
