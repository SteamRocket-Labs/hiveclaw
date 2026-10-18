/**
 * System domain adapter — version, health, tenants, onboarding.
 *
 * Fixes breakpoint: /api/version → mapped to /api/health
 */

import { del, get, post, put } from '../core';

export interface HealthInfo {
  status: string;
  version: string;
}

export interface TenantInfo {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  timezone?: string;
}

export interface JoinResult {
  tenant: TenantInfo;
  role: string;
  access_token: string;
}

export interface CompanyOnboardingStatus {
  tenant_id: string;
  tenant_name: string;
  authenticated_user_id: string;
  configuration_only: true;
  employees: Array<{ id: string; name: string; agent_type: string }>;
  items: Array<{ key: 'sso' | 'llm' | 'agent' | 'channel' | 'org'; title: string; completed: boolean; required: boolean; link: string | null }>;
  completed: number;
  total: number;
}

export interface DeleteTenantResult {
  fallback_tenant_id: string | null;
  needs_company_setup: boolean;
}

export const systemApi = {
  /** Maps the old /api/version call to /api/health which actually exists */
  getVersion: () => get<HealthInfo>('/health'),

  /** Tenant management */
  getTenant: (tenantId: string) => get<TenantInfo & Record<string, unknown>>(`/tenants/${tenantId}`),
  updateTenant: (tenantId: string, data: Record<string, unknown>) =>
    put<TenantInfo & Record<string, unknown>>(`/tenants/${tenantId}`, data),
  deleteTenant: (tenantId: string) => del<DeleteTenantResult>(`/tenants/${tenantId}`),

  /** Tenant self-service */
  createTenant: (data: { name: string; slug?: string }) => post<TenantInfo>('/tenants/self-create', data),
  joinTenant: (data: { invitation_code: string }) => post<JoinResult>('/tenants/join', data),
  getRegistrationConfig: () => get<{ invitation_code_required: boolean }>('/tenants/registration-config'),

  /** Onboarding */
  getOnboardingStatus: (tenantId?: string) => get<CompanyOnboardingStatus>(`/enterprise/onboarding-status${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''}`),
};
