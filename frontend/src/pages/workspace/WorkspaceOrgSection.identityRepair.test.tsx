// @vitest-environment jsdom

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
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

const directory = {
  members: [
    {
      org_member_id: 'member-ada',
      label: 'Ada Person',
      subject_kinds: ['provider_open_id', 'provider_union_id'],
      reason: 'unregistered',
      valid: false,
      links: [],
    },
    {
      org_member_id: 'member-blank',
      label: 'Blank Person',
      subject_kinds: [],
      reason: 'missing_provider_subject',
      valid: false,
      links: [],
    },
  ],
  accounts: [
    { user_id: 'user-ada', label: 'Ada', is_active: true },
    { user_id: 'user-bea', label: 'Bea', is_active: true },
  ],
  providers: [
    { id: 'provider-home', provider_type: 'feishu', name: 'Home Feishu', is_active: true },
  ],
};

const storedLink = {
  id: 'link-ada',
  org_member_id: 'member-ada',
  user_id: 'user-ada',
  provider_id: 'provider-home',
  subject_kind: 'provider_open_id',
  status: 'active',
  stored_status: 'active',
  version: 'version-1',
  source: 'manual',
  created_by_user_id: 'admin-1',
  valid: true,
  reason: 'active',
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
  const view = render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18n}>
        <WorkspaceOrgSection selectedTenantId={tenantId} />
      </I18nextProvider>
    </QueryClientProvider>,
  );
  return { ...view, client };
}

async function chooseRepair() {
  await screen.findByRole('option', { name: /Ada Person/ });
  fireEvent.change(screen.getByLabelText(words.identityRepairPerson), { target: { value: 'member-ada' } });
  fireEvent.change(screen.getByLabelText(words.identityRepairAccount), { target: { value: 'user-ada' } });
  fireEvent.change(screen.getByLabelText(words.identityRepairProvider), { target: { value: 'provider-home' } });
  fireEvent.change(screen.getByLabelText(words.identityRepairKind), { target: { value: 'provider_open_id' } });
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getSetting.mockResolvedValue({ value: { app_id: 'cli_test' } });
  api.getDepartments.mockResolvedValue([]);
  api.getOrgMembers.mockResolvedValue([]);
  api.updateSetting.mockResolvedValue({});
  api.syncOrg.mockResolvedValue({});
  api.dryRunIdentityLinks.mockResolvedValue({ rows: [], department_proposals: [], counts: {}, applied: false });
  api.getFeishuRuntimeStatus.mockResolvedValue(null);
  api.listIdentityLinks.mockResolvedValue(directory);
  api.createIdentityLink.mockResolvedValue({
    applied: true, code: 'active', reason: 'active', message: 'The link was saved.', retryable: false, link: storedLink,
  });
  api.revokeIdentityLink.mockResolvedValue({
    applied: true, code: 'revoked', reason: 'revoked', message: 'The link was revoked.', retryable: false, link: null,
  });
  api.replaceIdentityLink.mockResolvedValue({
    applied: true, code: 'active', reason: 'active', message: 'The link was saved.', retryable: false, link: storedLink,
  });
});

afterEach(() => {
  cleanup();
});

describe('stored identity repair', () => {
  it('shows the repair form on a wide and a narrow viewport without using a dry-run id', async () => {
    const css = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), 'WorkspaceOrgSection.css'), 'utf8');
    expect(css).toContain('@media (max-width: 720px)');
    expect(css).toContain('flex-direction: column');
    for (const width of [1280, 390]) {
      window.innerWidth = width;
      mount();
      expect(await screen.findByText(words.identityRepair)).toBeTruthy();
      expect(document.querySelectorAll('.ws-org-form-row').length).toBeGreaterThan(0);
      expect(screen.queryByRole('option', { name: 'ou_incoming_only' })).toBeNull();
      cleanup();
    }
  });

  it('shows a loading state until the stored directory returns', async () => {
    let resolveList: (value: typeof directory) => void = () => {};
    api.listIdentityLinks.mockReturnValue(new Promise((resolve) => {
      resolveList = resolve;
    }));
    mount();
    expect(await screen.findByText(words.identityRepairLoading)).toBeTruthy();
    resolveList(directory);
    expect(await screen.findByRole('option', { name: /Ada Person/ })).toBeTruthy();
  });

  it('saves the selected person, account, provider, and kind and shows the persisted id', async () => {
    const storedDirectory = {
      ...directory,
      members: directory.members.map((member) => member.org_member_id === 'member-ada'
        ? { ...member, reason: 'active', valid: true, links: [storedLink] }
        : member),
    };
    let saved = false;
    api.listIdentityLinks.mockImplementation(async () => (saved ? storedDirectory : directory));
    api.createIdentityLink.mockImplementation(async () => {
      saved = true;
      return {
        applied: true, code: 'active', reason: 'active', message: 'The link was saved.', retryable: false, link: storedLink,
      };
    });
    mount();
    await chooseRepair();
    expect(screen.getByText(/This will link Ada Person to Ada through Home Feishu/).textContent)
      .toContain(words.identityKindProviderOpenId);
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(await screen.findByText(/link-ada/)).toBeTruthy();
    expect(api.createIdentityLink).toHaveBeenCalledWith({
      org_member_id: 'member-ada',
      user_id: 'user-ada',
      provider_id: 'provider-home',
      subject_kind: 'provider_open_id',
    }, 'tenant-1');
    expect(api.createIdentityLink.mock.calls[0][0]).not.toHaveProperty('match_subject');
    expect(api.syncOrg).not.toHaveBeenCalled();
    expect(api.dryRunIdentityLinks).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('option', { name: /Ada Person · Active/ })).toBeTruthy());
  });

  it('shows a retry for a typed conflict and keeps the stored account on revoke', async () => {
    api.listIdentityLinks.mockResolvedValue({
      ...directory,
      members: directory.members.map((member) => member.org_member_id === 'member-ada'
        ? { ...member, reason: 'active', valid: true, links: [storedLink] }
        : member),
    });
    api.replaceIdentityLink.mockResolvedValueOnce({
      applied: false,
      code: 'active_link_conflict',
      reason: 'active_link_conflict',
      message: 'An active link already uses this directory person or subject. Reload and retry.',
      retryable: true,
      link: null,
    });
    mount();
    await chooseRepair();
    fireEvent.change(screen.getByLabelText(words.identityRepairAccount), { target: { value: 'user-bea' } });
    expect(screen.getByText(/revoke the stored link for Ada Person to Ada/).textContent)
      .toContain(words.identityKindProviderOpenId);
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(api.replaceIdentityLink).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairReload }));
    expect(await screen.findByText(/Current mapping: Ada ·/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairRetry }));
    await waitFor(() => expect(api.replaceIdentityLink).toHaveBeenCalledTimes(2));
    const replaceBody = api.replaceIdentityLink.mock.calls[0][1];
    expect(replaceBody.expected_user_id).toBe('user-ada');
    expect(replaceBody.user_id).toBe('user-bea');
    expect(replaceBody).not.toHaveProperty('org_member_id');
    expect(replaceBody).not.toHaveProperty('match_subject');
    expect(api.replaceIdentityLink.mock.calls[1][1]).not.toHaveProperty('org_member_id');
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairRevoke }));
    await waitFor(() => expect(api.revokeIdentityLink).toHaveBeenCalledWith('link-ada', {
      expected_version: 'version-1',
      expected_user_id: 'user-ada',
      expected_provider_id: 'provider-home',
      expected_subject_kind: 'provider_open_id',
      expected_status: 'active',
    }, 'tenant-1'));
  });

  it('shows forbidden, empty, and load-error states and clears another company', async () => {
    api.listIdentityLinks.mockRejectedValueOnce(new ApiError(403, 'Admin access required'));
    const first = mount();
    expect(await screen.findByText(words.identityRepairForbidden)).toBeTruthy();
    first.unmount();

    api.listIdentityLinks.mockResolvedValueOnce({ members: [], accounts: [], providers: [] });
    const empty = mount();
    expect(await screen.findByText(words.identityRepairEmpty)).toBeTruthy();
    empty.unmount();

    api.listIdentityLinks.mockRejectedValueOnce(new ApiError(503, words.identityRepairUnavailable));
    const failed = mount();
    expect(await screen.findByRole('button', { name: words.identityRepairRetryLoad })).toBeTruthy();
    failed.unmount();

    api.listIdentityLinks.mockResolvedValue(directory);
    const switched = mount('tenant-1');
    await screen.findByRole('option', { name: /Ada Person/ });
    fireEvent.change(screen.getByLabelText(words.identityRepairPerson), { target: { value: 'member-ada' } });
    switched.rerender(
      <QueryClientProvider client={switched.client}>
        <I18nextProvider i18n={i18n}>
          <WorkspaceOrgSection selectedTenantId="tenant-2" />
        </I18nextProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect((screen.getByLabelText(words.identityRepairPerson) as HTMLSelectElement).value).toBe(''));
  });

  it('requires a selected current link before revoke or replace', async () => {
    const unionLink = {
      ...storedLink,
      id: 'link-union',
      subject_kind: 'provider_union_id',
      version: 'version-union',
    };
    api.listIdentityLinks.mockResolvedValue({
      ...directory,
      members: directory.members.map((member) => member.org_member_id === 'member-ada'
        ? { ...member, reason: 'active', valid: true, links: [storedLink, unionLink] }
        : member),
    });
    mount();
    await chooseRepair();
    expect(screen.queryByRole('button', { name: words.identityRepairRevoke })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(api.replaceIdentityLink).not.toHaveBeenCalled();
    expect(api.createIdentityLink).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText(words.identityRepairCurrentLink), { target: { value: 'link-union' } });
    expect(screen.getByText(/revoke the stored link for Ada Person to Ada/).textContent)
      .toContain(words.identityKindProviderUnionId);
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairRevoke }));
    await waitFor(() => expect(api.revokeIdentityLink).toHaveBeenCalledWith('link-union', {
      expected_version: 'version-union',
      expected_user_id: 'user-ada',
      expected_provider_id: 'provider-home',
      expected_subject_kind: 'provider_union_id',
      expected_status: 'active',
    }, 'tenant-1'));
    expect(api.revokeIdentityLink.mock.calls[0][0]).not.toBe('link-ada');
  });

  it('reloads the current mapping before retrying a stale save or revoke', async () => {
    const advanced = { ...storedLink, version: 'version-2', user_id: 'user-cam' };
    let listed = {
      ...directory,
      accounts: [...directory.accounts, { user_id: 'user-cam', label: 'Cam', is_active: true }],
      members: directory.members.map((member) => member.org_member_id === 'member-ada'
        ? { ...member, reason: 'active', valid: true, links: [storedLink] }
        : member),
    };
    api.listIdentityLinks.mockImplementation(async () => listed);
    api.replaceIdentityLink.mockImplementation(async (_id: string, body: { expected_version: string }) => {
      if (body.expected_version === 'version-1') {
        listed = {
          ...listed,
          members: listed.members.map((member) => member.org_member_id === 'member-ada'
            ? { ...member, links: [advanced] }
            : member),
        };
        throw new ApiError(409, words.identityRepairStale, { code: 'stale_mapping', retryable: true });
      }
      return {
        applied: true, code: 'active', reason: 'active', message: 'The link was saved.', retryable: false, link: advanced,
      };
    });
    mount();
    await chooseRepair();
    fireEvent.change(screen.getByLabelText(words.identityRepairAccount), { target: { value: 'user-bea' } });
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(api.replaceIdentityLink).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairConfirm }));
    expect(api.replaceIdentityLink).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairReload }));
    expect(await screen.findByText(/Current mapping: Cam ·/)).toBeTruthy();
    expect((screen.getByLabelText(words.identityRepairAccount) as HTMLSelectElement).value).toBe('user-bea');
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairRetry }));
    await waitFor(() => expect(api.replaceIdentityLink).toHaveBeenCalledTimes(2));
    expect(api.replaceIdentityLink.mock.calls[1][1]).toMatchObject({
      expected_version: 'version-2',
      expected_user_id: 'user-cam',
      user_id: 'user-bea',
    });
    expect(api.replaceIdentityLink.mock.calls[1][1]).not.toHaveProperty('org_member_id');
    expect(api.createIdentityLink).not.toHaveBeenCalled();

    listed = {
      ...directory,
      members: directory.members.map((member) => member.org_member_id === 'member-ada'
        ? { ...member, reason: 'active', valid: true, links: [storedLink] }
        : member),
    };
    api.replaceIdentityLink.mockReset();
    api.createIdentityLink.mockClear();
    api.revokeIdentityLink.mockImplementation(async (_id: string, body: { expected_version: string }) => {
      if (body.expected_version === 'version-1') {
        listed = {
          ...listed,
          members: listed.members.map((member) => member.org_member_id === 'member-ada'
            ? { ...member, links: [{ ...storedLink, version: 'version-2' }] }
            : member),
        };
        throw new ApiError(409, words.identityRepairStale, { code: 'stale_mapping', retryable: true });
      }
      return {
        applied: true, code: 'revoked', reason: 'revoked', message: 'The link was revoked.', retryable: false, link: null,
      };
    });
    cleanup();
    mount();
    await chooseRepair();
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairRevoke }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairReload }));
    expect(await screen.findByText(/version-2/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: words.identityRepairRetryRevoke }));
    await waitFor(() => expect(api.revokeIdentityLink).toHaveBeenCalledTimes(2));
    expect(api.revokeIdentityLink.mock.calls[1][1].expected_version).toBe('version-2');
    expect(api.replaceIdentityLink).not.toHaveBeenCalled();
    expect(api.createIdentityLink).not.toHaveBeenCalled();
  });
});
