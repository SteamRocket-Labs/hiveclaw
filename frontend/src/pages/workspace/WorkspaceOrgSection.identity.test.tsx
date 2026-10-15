// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance, type i18n as I18nInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/core';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const api = vi.hoisted(() => ({
  getSetting: vi.fn(),
  getDepartments: vi.fn(),
  getOrgMembers: vi.fn(),
  updateSetting: vi.fn(),
  syncOrg: vi.fn(),
  dryRunIdentityLinks: vi.fn(),
  getFeishuRuntimeStatus: vi.fn(),
}));

vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: api }));
vi.mock('../../api/domains/tools', () => ({ toolsApi: { getFeishuRuntimeStatus: api.getFeishuRuntimeStatus } }));

import WorkspaceOrgSection from './WorkspaceOrgSection';

const words = en.enterprise.org;
let i18n: I18nInstance;

const trustedReport = {
  schema: 'hive.identity_dry_run.v1',
  tenant_id: 'tenant-1',
  source_revision: '136332b2165f50e610ee07f509cc748cf42d653e',
  applied: false,
  inputs: { stored_rows: true, incoming_provider_subjects: true },
  counts: {
    trusted: 1,
    conflict: 1,
    unregistered: 0,
    inactive_user: 0,
    cross_tenant: 1,
    name_collision: 0,
    department_conflict: 0,
    email_only_candidate: 0,
  },
  department_proposals: [{ name: 'Research', action: 'create', applied: false }],
  rows: [
    {
      object_kind: 'org_member',
      object_id: 'member-ada',
      disposition: 'trusted',
      reason: 'provider_open_id',
      user_id: 'user-ada',
      global_unique_blocked: false,
    },
    {
      object_kind: 'org_member',
      object_id: 'ou_foreign',
      disposition: 'cross_tenant',
      reason: 'cross_tenant_subject',
      user_id: null,
      global_unique_blocked: true,
    },
    {
      object_kind: 'org_member',
      object_id: 'member-local',
      disposition: 'conflict',
      reason: 'local_subject_collision',
      user_id: null,
      global_unique_blocked: true,
    },
  ],
};

beforeAll(async () => {
  i18n = createInstance();
  await i18n.init({
    lng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
});

function mount(tenantId = 'tenant-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <WorkspaceOrgSection selectedTenantId={tenantId} />
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getSetting.mockResolvedValue({ value: { app_id: 'cli_test' } });
  api.getDepartments.mockResolvedValue([]);
  api.getOrgMembers.mockResolvedValue([]);
  api.updateSetting.mockResolvedValue({});
  api.syncOrg.mockResolvedValue({});
  api.getFeishuRuntimeStatus.mockResolvedValue(null);
});

afterEach(() => {
  cleanup();
});

describe('organization identity check', () => {
  it('shows trusted and blocked rows from the dry-run without saving or syncing', async () => {
    let resolveReport: (value: typeof trustedReport) => void = () => {};
    api.dryRunIdentityLinks.mockReturnValue(new Promise((resolve) => {
      resolveReport = resolve;
    }));
    mount();
    const secret = document.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(secret, { target: { value: 'typed-secret-not-saved' } });
    fireEvent.click(screen.getByRole('button', { name: words.identityCheck }));
    expect((screen.getByRole('button', { name: words.identityChecking }) as HTMLButtonElement).disabled).toBe(true);
    resolveReport(trustedReport);
    expect(await screen.findByText(words.identityAppliedNone)).toBeTruthy();
    expect(screen.getAllByText(words.disposition.trusted, { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText(words.reason.provider_open_id)).toBeTruthy();
    expect(screen.getByText(words.remedy.trusted)).toBeTruthy();
    expect(screen.getByText('Matched user user-ada')).toBeTruthy();
    expect(screen.getByText(words.identityBlocked)).toBeTruthy();
    expect(screen.getByText(words.identityBlockedLocal)).toBeTruthy();
    expect(screen.getByText(words.reason.local_subject_collision)).toBeTruthy();
    expect(screen.getByText('Departments that could be created later: 1. They were not created.')).toBeTruthy();
    expect(screen.getByText('Ready to link later 1')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sync Now' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /repair|apply link/i })).toBeNull();
    expect(api.dryRunIdentityLinks).toHaveBeenCalledWith('tenant-1');
    expect(api.updateSetting).not.toHaveBeenCalled();
    expect(api.syncOrg).not.toHaveBeenCalled();
  });

  it('keeps a provider failure visible and retries into a real report', async () => {
    api.dryRunIdentityLinks
      .mockRejectedValueOnce(new ApiError(503, words.identityUnavailable, { applied: false, retryable: true }))
      .mockResolvedValueOnce({ ...trustedReport, department_proposals: [] });
    mount();
    fireEvent.click(screen.getByRole('button', { name: words.identityCheck }));
    expect((await screen.findByRole('alert')).textContent).toContain(words.identityUnavailable);
    expect(screen.queryByText(words.identityAppliedNone)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: words.identityRetry }));
    expect(await screen.findByText(words.identityAppliedNone)).toBeTruthy();
    expect(api.dryRunIdentityLinks).toHaveBeenCalledTimes(2);
  });

  it('shows an empty complete check without inventing rows', async () => {
    api.dryRunIdentityLinks.mockResolvedValue({
      ...trustedReport,
      counts: Object.fromEntries(Object.keys(trustedReport.counts).map((key) => [key, 0])),
      rows: [],
      department_proposals: [],
    });
    mount();
    fireEvent.click(screen.getByRole('button', { name: words.identityCheck }));
    expect(await screen.findByText(words.identityEmpty)).toBeTruthy();
    expect(screen.getByText(words.identityAppliedNone)).toBeTruthy();
    expect(screen.queryByText(words.reason.provider_open_id)).toBeNull();
  });

  it('drops an in-flight result when the selected company changes', async () => {
    let resolveReport: (value: typeof trustedReport) => void = () => {};
    api.dryRunIdentityLinks.mockReturnValue(new Promise((resolve) => {
      resolveReport = resolve;
    }));
    const view = mount('tenant-1');
    fireEvent.click(screen.getByRole('button', { name: words.identityCheck }));
    expect(screen.getByRole('button', { name: words.identityChecking })).toBeTruthy();
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <I18nextProvider i18n={i18n}>
          <WorkspaceOrgSection selectedTenantId="tenant-2" />
        </I18nextProvider>
      </QueryClientProvider>,
    );
    resolveReport(trustedReport);
    await waitFor(() => {
      expect((screen.getByRole('button', { name: words.identityCheck }) as HTMLButtonElement).disabled).toBe(false);
    });
    expect(screen.queryByText(words.identityAppliedNone)).toBeNull();
    expect(screen.queryByText('member-ada')).toBeNull();
  });
});
