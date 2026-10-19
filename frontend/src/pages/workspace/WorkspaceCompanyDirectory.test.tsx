// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';

const mocks = vi.hoisted(() => ({ user: { id: 'admin', tenant_id: 'company-a', role: 'org_admin' }, read: vi.fn(), save: vi.fn(), sync: vi.fn(), webhook: vi.fn() }));
vi.mock('../../stores', () => ({ useAuthStore: (select: (state: { user: typeof mocks.user }) => unknown) => select({ user: mocks.user }) }));
vi.mock('../../api/domains/channels', () => ({ channelApi: { tenantChannels: mocks.read, saveTenantDirectory: mocks.save, tenantCompanyWebhook: mocks.webhook } }));
vi.mock('../../api/domains/enterprise', () => ({ enterpriseApi: { syncOrg: mocks.sync } }));
import WorkspaceCompanyDirectory from './WorkspaceCompanyDirectory';

const config = (provider = 'dingtalk', tenant = 'company-a', revision = 'one') => ({ id: `${tenant}:${provider}`, tenant_id: tenant,
  channel_type: provider, app_id: `${provider}-app`, is_active: true, has_app_secret: true, revision, extra_config: {} });
const scope = { expected_tenant_id: 'company-a', expected_user_id: 'admin' };
let instance: ReturnType<typeof createInstance>;
beforeEach(async () => {
  vi.clearAllMocks(); mocks.user = { id: 'admin', tenant_id: 'company-a', role: 'org_admin' };
  mocks.read.mockImplementation(async (value) => [config('dingtalk', value.expected_tenant_id), config('wecom', value.expected_tenant_id)]);
  mocks.sync.mockImplementation(async (tenant, provider) => ({ tenant_id: tenant, provider, status: 'complete', departments: 2, members: 3 }));
  mocks.webhook.mockImplementation(async (_provider, value) => ({ tenant_id: value.expected_tenant_id, webhook_url: 'https://company.fixture.test/api/channel/wecom/tenant/company-a/webhook' }));
  instance = createInstance(); await instance.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
});
afterEach(cleanup);
function mount(tenant = 'company-a') {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (next: string) => <I18nextProvider i18n={instance}><QueryClientProvider client={cache}>
    <WorkspaceCompanyDirectory selectedTenantId={next} />
  </QueryClientProvider></I18nextProvider>;
  const result = render(view(tenant));
  return { ...result, changeTenant: (next: string) => result.rerender(view(next)) };
}

it('syncs the selected saved connection in the captured requester scope', async () => {
  mount();
  await waitFor(() => expect((screen.getByRole('button', { name: en.enterprise.org.syncNow }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.org.syncNow }));
  await screen.findByText('Observed 2 departments and 3 people in the application-visible directory.');
  expect(mocks.sync).toHaveBeenCalledWith('company-a', 'dingtalk', 'admin');
  fireEvent.change(screen.getByLabelText(en.companyDirectory.provider), { target: { value: 'wecom' } });
  await waitFor(() => expect((screen.getByLabelText('Corp ID') as HTMLInputElement).value).toBe('wecom-app'));
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.org.syncNow }));
  await waitFor(() => expect(mocks.sync).toHaveBeenCalledWith('company-a', 'wecom', 'admin'));
  expect(mocks.save).not.toHaveBeenCalled();
});

it('preserves a failed connection draft and requires explicit save before syncing it', async () => {
  mocks.save.mockRejectedValueOnce(new Error('Unconfirmed save'));
  mocks.save.mockImplementationOnce(async (_provider, value) => ({ ...config(), app_id: value.app_id, revision: 'two' }));
  mount();
  await waitFor(() => expect((screen.getByLabelText('App Key') as HTMLInputElement).value).toBe('dingtalk-app'));
  fireEvent.change(screen.getByLabelText('App Key'), { target: { value: 'replacement-app' } });
  fireEvent.change(screen.getByLabelText(en.companyDirectory.secret), { target: { value: 'synthetic-new-secret' } });
  expect((screen.getByRole('button', { name: en.enterprise.org.syncNow }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: en.companyDirectory.save }));
  await screen.findByText(en.companyDirectory.saveFailed);
  expect((screen.getByLabelText(en.companyDirectory.secret) as HTMLInputElement).value).toBe('synthetic-new-secret');
  fireEvent.click(screen.getByRole('button', { name: en.companyDirectory.refresh }));
  await waitFor(() => expect((screen.getByRole('button', { name: en.companyDirectory.save }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: en.companyDirectory.save }));
  await screen.findByText(en.companyDirectory.saved);
  expect(mocks.save).toHaveBeenLastCalledWith('dingtalk', { ...scope, app_id: 'replacement-app', app_secret: 'synthetic-new-secret', is_active: true, expected_revision: 'one' });
  expect((screen.getByLabelText(en.companyDirectory.secret) as HTMLInputElement).value).toBe('');
  expect(mocks.sync).not.toHaveBeenCalled();
});

it('discards a late sync result after switching the selected company', async () => {
  let finish!: (value: object) => void;
  mocks.sync.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const page = mount();
  await waitFor(() => expect((screen.getByRole('button', { name: en.enterprise.org.syncNow }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.org.syncNow }));
  page.changeTenant('company-b');
  await waitFor(() => expect(mocks.read).toHaveBeenCalledWith({ expected_tenant_id: 'company-b', expected_user_id: 'admin' }, expect.any(AbortSignal)));
  finish({ tenant_id: 'company-a', provider: 'dingtalk', status: 'complete', departments: 99, members: 99 });
  await waitFor(() => expect((screen.getByRole('button', { name: en.enterprise.org.syncNow }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByText(/Observed 99/)).toBeNull();
});

it('reports assignment conflicts and links to the existing department repair surface', async () => {
  mocks.sync.mockResolvedValueOnce({ tenant_id: 'company-a', provider: 'dingtalk', status: 'complete', departments: 2, members: 3,
    assignment_counts: { conflict: 1 } });
  mount();
  await waitFor(() => expect((screen.getByRole('button', { name: en.enterprise.org.syncNow }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: en.enterprise.org.syncNow }));
  await screen.findByText((text) => text.includes(en.companyDirectory.assignmentReview));
  expect(screen.getByRole('link', { name: en.companyDirectory.departments }).getAttribute('href')).toBe('#org-department-cutover');
});


it('enables company messaging with exact company credentials and no directory or identity side effect', async () => {
  mocks.save.mockImplementationOnce(async (_provider, value) => {
    const saved = { ...config(), revision: 'two', extra_config: value.extra_config };
    mocks.read.mockResolvedValue([saved]);
    return saved;
  });
  mount();
  await screen.findByDisplayValue('dingtalk-app');
  fireEvent.click(screen.getByLabelText(en.companyMessaging.enable));
  fireEvent.change(screen.getByLabelText('Corp ID'), { target: { value: 'synthetic-company-corp' } });
  expect((screen.getByRole('button', { name: en.enterprise.org.syncNow }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: en.companyDirectory.save }));
  await screen.findByText(en.companyDirectory.saved);
  expect(mocks.save).toHaveBeenLastCalledWith('dingtalk', { ...scope, app_id: 'dingtalk-app', is_active: true,
    expected_revision: 'one', extra_config: { messaging_enabled: true, corp_id: 'synthetic-company-corp' } });
  expect(mocks.sync).not.toHaveBeenCalled();
});

it('configures the actual Smart Bot transport and creator-dependent identity mode without inferring a member binding', async () => {
  mocks.save.mockImplementationOnce(async (_provider, value) => {
    const saved = { ...config('wecom'), revision: 'two', has_verification_token: true, has_encrypt_key: true, extra_config: value.extra_config };
    mocks.read.mockResolvedValue([saved]);
    return saved;
  });
  mount();
  await screen.findByDisplayValue('dingtalk-app');
  fireEvent.change(screen.getByLabelText(en.companyDirectory.provider), { target: { value: 'wecom' } });
  await screen.findByDisplayValue('wecom-app');
  fireEvent.click(screen.getByLabelText(en.companyMessaging.enable));
  fireEvent.change(screen.getByLabelText(en.companyMessaging.wecomTransport), { target: { value: 'smart_bot' } });
  fireEvent.change(screen.getByLabelText('Bot ID'), { target: { value: 'company-bot' } });
  expect((screen.getByLabelText(en.companyMessaging.botSubjectMode) as HTMLSelectElement).value).toBe('encrypted');
  expect(screen.getByText(en.companyMessaging.smartBotSetup)).toBeTruthy();
  fireEvent.change(screen.getByLabelText(en.tenantFeishu.verificationToken), { target: { value: 'synthetic-bot-token' } });
  fireEvent.change(screen.getByLabelText(en.companyMessaging.encodingKey), { target: { value: 'synthetic-bot-key' } });
  fireEvent.click(screen.getByRole('button', { name: en.companyDirectory.save }));
  await screen.findByText(en.companyDirectory.saved);
  expect(mocks.save).toHaveBeenLastCalledWith('wecom', { ...scope, app_id: 'wecom-app', is_active: true, expected_revision: 'one',
    verification_token: 'synthetic-bot-token', encrypt_key: 'synthetic-bot-key', extra_config: { messaging_enabled: true,
      wecom_agent_id: '', wecom_transport: 'smart_bot', wecom_bot_id: 'company-bot', wecom_bot_userid_mode: 'encrypted' } });
  expect((screen.getByLabelText(en.tenantFeishu.verificationToken) as HTMLInputElement).value).toBe('');
  expect((screen.getByLabelText(en.companyMessaging.encodingKey) as HTMLInputElement).value).toBe('');
  expect((await screen.findByDisplayValue('https://company.fixture.test/api/channel/wecom/tenant/company-a/webhook'))).toBeTruthy();
  expect(mocks.sync).not.toHaveBeenCalled();
});
