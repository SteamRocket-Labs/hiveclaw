// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  getSetting: vi.fn(),
  getLegacyCompanyFilesStatus: vi.fn(),
  getStats: vi.fn(),
  getTenant: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: unknown) => typeof fallback === 'string' ? fallback : key,
  }),
}));

vi.mock('../stores', () => {
  const state = {
    user: { id: 'platform-admin-1', role: 'platform_admin' },
    setUser: vi.fn(),
  };
  const useAuthStore = ((selector: (input: typeof state) => unknown) => selector(state)) as unknown as typeof import('../stores').useAuthStore;
  Object.assign(useAuthStore, { getState: () => state });
  return { useAuthStore };
});

vi.mock('../api/domains/enterprise', () => ({
  enterpriseApi: {
    getSetting: mocks.getSetting,
    getLegacyCompanyFilesStatus: mocks.getLegacyCompanyFilesStatus,
    getStats: mocks.getStats,
  },
}));

vi.mock('../api/domains/system', () => ({
  systemApi: {
    getTenant: mocks.getTenant,
  },
}));

vi.mock('../api/domains/auth', () => ({ authApi: {} }));
vi.mock('../api/domains/notifications', () => ({ notificationsApi: {} }));

import EnterpriseSettings from './EnterpriseSettings';

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <EnterpriseSettings forcedTab="info" hideTabs />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.setItem('current_tenant_id', 'tenant-1');
  mocks.getSetting.mockResolvedValue({ key: 'company_intro_tenant-1', value: { content: 'PRIVATE-COMPANY-BODY' } });
  mocks.getLegacyCompanyFilesStatus.mockResolvedValue({
    available: true,
    file_count: 2,
    total_bytes: 42,
    excluded_symlink_count: 0,
    read_only: true,
    retired: true,
    surface_kind: 'legacy_company_files_quarantine',
    company_kb_available: false,
    agent_consumable: false,
  });
  mocks.getStats.mockResolvedValue({ total_users: 2, running_agents: 1, total_agents: 1, pending_approvals: 0 });
  mocks.getTenant.mockResolvedValue({ id: 'tenant-1', name: 'Tenant One', slug: 'tenant-one', is_active: true, timezone: 'UTC' });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

describe('EnterpriseSettings platform-admin company-content boundary', () => {
  it('does not request or mount company business content on the default info route', async () => {
    renderSettings();

    await waitFor(() => expect(mocks.getTenant).toHaveBeenCalled());
    expect(mocks.getSetting).not.toHaveBeenCalled();
    expect(mocks.getLegacyCompanyFilesStatus).not.toHaveBeenCalled();
    expect(mocks.getStats).not.toHaveBeenCalled();
    expect(screen.getByText('Tenant configuration only')).toBeTruthy();
    expect(screen.queryByText('PRIVATE-COMPANY-BODY')).toBeNull();
    expect(screen.queryByText('Broadcast Notification')).toBeNull();
  });
});
