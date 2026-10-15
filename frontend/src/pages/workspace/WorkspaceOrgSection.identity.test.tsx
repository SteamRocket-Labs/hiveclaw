// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance, type i18n as I18nInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/core';
import { feishuConflictMessage } from '../../api/domains/auth';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const api = vi.hoisted(() => ({
  getSetting: vi.fn(),
  getDepartments: vi.fn(),
  getOrgMembers: vi.fn(),
  updateSetting: vi.fn(),
  syncOrg: vi.fn(),
  dryRunIdentityLinks: vi.fn(),
  listIdentityLinks: vi.fn(),
  createIdentityLink: vi.fn(),
  revokeIdentityLink: vi.fn(),
  replaceIdentityLink: vi.fn(),
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
  api.listIdentityLinks.mockResolvedValue({ members: [], accounts: [], providers: [] });
  api.createIdentityLink.mockResolvedValue({ applied: false, code: 'unregistered', reason: 'unregistered', message: '', retryable: false, link: null });
  api.revokeIdentityLink.mockResolvedValue({ applied: false, code: 'revoked', reason: 'revoked', message: '', retryable: false, link: null });
  api.replaceIdentityLink.mockResolvedValue({ applied: false, code: 'active', reason: 'active', message: '', retryable: false, link: null });
});

afterEach(() => {
  cleanup();
});

describe('organization identity check', () => {
  it('shows trusted and blocked rows from the dry-run without saving or syncing', async () => {
    let resolveReport: (value: typeof trustedReport) => void = () => {};
    api.listIdentityLinks.mockResolvedValue({ members: [], accounts: [], providers: [] });
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

describe('organization sync result', () => {
  function renderAt(tenantId: string) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    const view = render(
      <QueryClientProvider client={client}>
        <I18nextProvider i18n={i18n}>
          <WorkspaceOrgSection selectedTenantId={tenantId} />
        </I18nextProvider>
      </QueryClientProvider>,
    );
    return { ...view, client, invalidate };
  }

  async function clickSync() {
    await screen.findByDisplayValue('cli_test');
    const button = screen.getByRole('button', { name: 'Sync Now' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
  }

  it('shows applied and skipped rows and opens the identity repair card', async () => {
    api.listIdentityLinks.mockResolvedValue({
      members: [{
        org_member_id: 'member-1',
        label: 'Ada',
        subject_kinds: ['provider_open_id'],
        reason: 'unregistered',
        valid: false,
        links: [],
      }],
      accounts: [],
      providers: [],
    });
    const view = renderAt('tenant-1');
    api.syncOrg.mockResolvedValue({
      status: 'complete',
      departments: 2,
      members: 4,
      applied: 2,
      skipped: 2,
      conflicts: 1,
      rows: [{ reason: 'email_only_candidate', disposition: 'email_only_candidate', applied: false }],
    });
    await clickSync();
    await waitFor(() => {
      expect(document.querySelector('.ws-org-sync-result')?.textContent).toContain('Sync complete: 2 departments, 2 members imported');
    });
    const result = document.querySelector('.ws-org-sync-result');
    expect(result?.textContent).toContain('Applied 2. Skipped 2, including 1 that need repair.');
    expect(result?.textContent).toContain('The email matches one user, and no provider subject matched.');
    const repair = screen.getByRole('link', { name: 'Review identity repair' });
    expect(repair.getAttribute('href')).toBe('#identity-repair-person');
    expect(document.querySelector('.ws-org-sync-result')?.className).toContain('is-attention');
    await waitFor(() => {
      const keys = view.invalidate.mock.calls.map((call) => call[0]?.queryKey);
      expect(keys).toContainEqual(['org-identity-links', 'tenant-1']);
      expect(keys).toContainEqual(['company-knowledge-grant-identities']);
    });
  });

  it('keeps an unfinished sync unsaved and retries it', async () => {
    const view = renderAt('tenant-1');
    api.syncOrg.mockResolvedValue({
      status: 'unavailable',
      code: 'incomplete',
      error: 'The directory snapshot did not finish. Nothing was saved.',
      applied: 0,
    });
    await clickSync();
    await waitFor(() => {
      expect(document.querySelector('.ws-org-sync-result')?.textContent).toContain('The directory snapshot did not finish. Nothing was saved.');
    });
    expect(document.querySelector('.ws-org-sync-result')?.className).toContain('is-error');
    expect(view.invalidate.mock.calls.map((call) => call[0]?.queryKey)).not.toContainEqual(['org-identity-links', 'tenant-1']);
    api.syncOrg.mockResolvedValue({ status: 'complete', departments: 1, members: 1, applied: 1, skipped: 0, conflicts: 0 });
    fireEvent.click(screen.getByRole('button', { name: 'Retry sync' }));
    await waitFor(() => {
      expect(document.querySelector('.ws-org-sync-result')?.textContent).toContain('Sync complete: 1 departments, 1 members imported');
    });
    expect(document.querySelector('.ws-org-sync-result')?.className).toContain('is-ok');
    expect(api.syncOrg).toHaveBeenCalledTimes(2);
  });

  it('shows an incoming-only conflict without a stored person or an imported count', async () => {
    const view = renderAt('tenant-1');
    api.syncOrg.mockResolvedValue({
      status: 'complete',
      departments: 0,
      members: 1,
      applied: 0,
      skipped: 1,
      conflicts: 1,
      users_created: 0,
      rows: [{
        object_id: 'ou-2c303cee8b',
        disposition: 'email_only_candidate',
        reason: 'email_only_candidate',
        applied: false,
        department_ids: ['0'],
        email: 'hidden@example.test',
      }],
    });
    await clickSync();
    await waitFor(() => {
      expect(document.querySelector('.ws-org-sync-result')?.textContent).toContain('Sync finished. Applied 0. Nothing was imported.');
    });
    const result = document.querySelector('.ws-org-sync-result');
    expect(result?.textContent).toContain('Applied 0. Skipped 1, including 1 that need repair.');
    expect(result?.textContent).toContain('The email matches one user, and no provider subject matched.');
    expect(result?.textContent).not.toContain('members imported');
    expect(result?.textContent).not.toContain('ou-2c303cee8b');
    expect(result?.textContent).not.toContain('hidden@example.test');
    expect(result?.textContent).toContain('That person opens Account Settings and chooses Link Feishu Account. Then sync again.');
    expect(result?.querySelector('a[href="#identity-check"]')).toBeNull();
    expect(screen.queryByRole('link', { name: 'Review identity repair' })).toBeNull();
    expect(view.invalidate.mock.calls.map((call) => call[0]?.queryKey)).toContainEqual(['org-identity-links', 'tenant-1']);
  });

  it('shows recovery only for the unapplied row of a mixed sync result', async () => {
    renderAt('tenant-1');
    api.syncOrg.mockResolvedValue({
      status: 'complete',
      departments: 0,
      members: 2,
      applied: 1,
      skipped: 1,
      conflicts: 1,
      users_created: 1,
      rows: [
        {
          object_id: 'new-o-d1be53088c',
          disposition: 'unregistered',
          reason: 'unregistered',
          applied: true,
          department_ids: ['0'],
        },
        {
          object_id: 'ou-d1be53088c',
          disposition: 'email_only_candidate',
          reason: 'email_only_candidate',
          applied: false,
          department_ids: ['0'],
        },
      ],
    });
    await clickSync();
    await waitFor(() => {
      expect(document.querySelector('.ws-org-sync-result')?.textContent).toContain('Sync complete: 0 departments, 1 members imported');
    });
    const text = document.querySelector('.ws-org-sync-result')?.textContent ?? '';
    expect(text).toContain('Applied 1. Skipped 1, including 1 that need repair.');
    expect(text).toContain('The email matches one user, and no provider subject matched.');
    expect(text).not.toContain('This directory row was skipped. No account was changed.');
    expect(text.match(/This directory person was not stored/g) ?? []).toHaveLength(1);
    expect(text).not.toContain('new-o-d1be53088c');
    expect(text).not.toContain('ou-d1be53088c');
    expect(screen.queryByRole('link', { name: 'Review identity repair' })).toBeNull();
  });

  it('does not treat an unrelated stored person as recovery for an incoming row', async () => {
    api.listIdentityLinks.mockResolvedValue({
      members: [{
        org_member_id: 'member-unrelated',
        label: 'Other',
        subject_kinds: ['provider_open_id'],
        reason: 'unregistered',
        valid: false,
        links: [],
      }],
      accounts: [],
      providers: [],
    });
    renderAt('tenant-1');
    api.syncOrg.mockResolvedValue({
      status: 'complete',
      departments: 0,
      members: 1,
      applied: 0,
      skipped: 1,
      conflicts: 1,
      rows: [
        { object_id: 'ou-incoming-only', reason: 'email_only_candidate', disposition: 'email_only_candidate', applied: false },
        { object_id: 'member-unrelated', reason: 'unregistered', disposition: 'unregistered', applied: false },
      ],
    });
    await clickSync();
    await waitFor(() => {
      expect(document.querySelector('.ws-org-sync-result')?.textContent).toContain('Link Feishu Account');
    });
    const result = document.querySelector('.ws-org-sync-result');
    expect(result?.textContent).toContain('That person opens Account Settings and chooses Link Feishu Account. Then sync again.');
    expect(result?.textContent).not.toContain('ou-incoming-only');
    const repair = screen.getByRole('link', { name: 'Review identity repair' });
    expect(repair.getAttribute('href')).toBe('#identity-repair-person');
    expect(result?.querySelectorAll('a[href="#identity-repair-person"]')).toHaveLength(1);
  });

  it('drops a late sync result when the company changes', async () => {
    let resolveSync: (value: object) => void = () => {};
    api.syncOrg.mockReturnValue(new Promise((resolve) => {
      resolveSync = resolve;
    }));
    const view = renderAt('tenant-1');
    await clickSync();
    expect(screen.getByRole('button', { name: 'Syncing...' })).toBeTruthy();
    view.rerender(
      <QueryClientProvider client={view.client}>
        <I18nextProvider i18n={i18n}>
          <WorkspaceOrgSection selectedTenantId="tenant-2" />
        </I18nextProvider>
      </QueryClientProvider>,
    );
    resolveSync({ status: 'complete', departments: 9, members: 9, applied: 9, skipped: 0, conflicts: 0 });
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Sync Now' })).toBeTruthy();
    });
    expect(screen.queryByText('Sync complete: 9 departments, 9 members imported')).toBeNull();
    expect(screen.queryByText('The directory snapshot did not finish. Nothing was saved.')).toBeNull();
  });
});

describe('feishu conflict copy', () => {
  it('uses the catalog sentence for a safe auth conflict', () => {
    const text = feishuConflictMessage('email_only_candidate', (key, fallback) => i18n.t(key, fallback));
    expect(text).toBe(en.auth.feishu.emailOnlyCandidate);
    const account = feishuConflictMessage(
      'external_identity_conflict',
      (key, fallback) => i18n.t(key, fallback),
      'account',
    );
    expect(account).toBe(en.account.feishuExternalIdentityConflict);
  });
});
