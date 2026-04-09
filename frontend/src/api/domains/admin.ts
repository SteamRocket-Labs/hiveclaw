/**
 * Admin domain adapter — platform-level company management + settings.
 */

import { get, post, put } from '../core';

export interface Company {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  user_count?: number;
  agent_count?: number;
  admin_invitation_code?: string;
  created_at: string;
  [key: string]: any;
}

export interface PlatformSettings {
  allow_self_registration: boolean;
  default_user_quota: Record<string, unknown>;
}

export interface MetricsLeaderboard {
  top_companies?: unknown[];
  top_agents?: unknown[];
}

export const adminApi = {
  listCompanies: () => get<Company[]>('/admin/companies'),
  createCompany: (data: { name: string; slug?: string }) => post<Company>('/admin/companies', data),
  toggleCompany: (id: string) => put<void>(`/admin/companies/${id}/toggle`),
  getPlatformSettings: () => get<PlatformSettings>('/admin/platform-settings'),
  updatePlatformSettings: (data: Partial<PlatformSettings>) => put<PlatformSettings>('/admin/platform-settings', data),
  getMetricsTimeseries: (params: { startDate: string; endDate: string }) =>
    get<unknown[]>(`/admin/metrics/timeseries?start_date=${encodeURIComponent(params.startDate)}&end_date=${encodeURIComponent(params.endDate)}`),
  getMetricsLeaderboards: () => get<MetricsLeaderboard>('/admin/metrics/leaderboards'),
};
