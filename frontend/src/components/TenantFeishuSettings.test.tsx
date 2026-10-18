// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/core';
import TenantFeishuSettings, { TenantFeishuIdentity } from './TenantFeishuSettings';

const mocks = vi.hoisted(() => ({ list: vi.fn(), webhook: vi.fn(), save: vi.fn(), identity: vi.fn(), action: vi.fn(), groups: vi.fn(), employees: vi.fn(),
  auth: { user: { id: 'owner', tenant_id: 'company', role: 'org_admin' } } }));
vi.mock('../stores', () => ({ useAuthStore: (selector: (state: typeof mocks.auth) => unknown) => selector(mocks.auth) }));
vi.mock('../api/domains/channels', () => ({ channelApi: {
  tenantChannels: mocks.list, tenantFeishuWebhook: mocks.webhook, saveTenantFeishu: mocks.save,
  tenantFeishuGroupRoutes: mocks.groups,
  tenantFeishuIdentity: mocks.identity, tenantFeishuIdentityAction: mocks.action,
} }));
vi.mock('../api/domains/agents', () => ({ agentApi: { list: mocks.employees } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, value?: string | { defaultValue?: string; field?: string }) =>
  typeof value === 'string' ? value : value?.defaultValue?.replace('{{field}}', value.field || '') || key }) }));

const config = { id: 'installation', tenant_id: 'company', channel_type: 'feishu', app_id: 'cli_company', revision: 'version-1',
  is_active: true, has_app_secret: true, has_encrypt_key: false, has_verification_token: true, extra_config: {} };
const identity = { user_id: 'owner', tenant_id: 'company', state: 'bound', principal_id: 'principal',
  entries: [{ subject_id: 'ou_member', principal_id: 'principal', state: 'bound' }] };
function wrap(child: React.ReactNode, client: QueryClient) {
  return <QueryClientProvider client={client}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider>;
}
function client() { return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }); }
beforeEach(() => {
  mocks.auth.user = { id: 'owner', tenant_id: 'company', role: 'org_admin' };
  localStorage.setItem('current_tenant_id', 'company');
  mocks.list.mockResolvedValue([config]); mocks.webhook.mockResolvedValue({ tenant_id: 'company', webhook_url: 'https://example.test/api/callback' });
  mocks.identity.mockResolvedValue(identity); mocks.action.mockResolvedValue({ authority_bound: false });
});
afterEach(() => { cleanup(); localStorage.clear(); vi.resetAllMocks(); });

describe('company Feishu settings', () => {
  it('preserves blank saved secrets and sends the captured company/user/version', async () => {
    const qc = client(); render(wrap(<TenantFeishuSettings selectedTenantId="company" />, qc));
    await waitFor(() => expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('cli_company'));
    const saved = { ...config, revision: 'version-2', is_active: false };
    mocks.save.mockImplementation(async () => { mocks.list.mockResolvedValue([saved]); return saved; });
    fireEvent.click(screen.getByLabelText('Enable the company bot'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Company bot settings saved.');
    expect(mocks.save).toHaveBeenCalledWith({ app_id: 'cli_company', is_active: false, platform_region: 'feishu_cn',
      expected_revision: 'version-1', expected_tenant_id: 'company', expected_user_id: 'owner' });
    expect(qc.getMutationCache().getAll()).toHaveLength(0);
  });
  it('keeps a conflicting draft and refreshes only its saved comparison/version', async () => {
    render(wrap(<TenantFeishuSettings selectedTenantId="company" />, client()));
    await waitFor(() => expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('cli_company'));
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'cli_draft' } });
    fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'DRAFT_ONLY_SECRET' } });
    mocks.save.mockRejectedValue(new ApiError(409, 'revision conflict'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('alert');
    mocks.list.mockResolvedValue([{ ...config, app_id: 'cli_other_admin', revision: 'version-2' }]);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText(/Saved application: cli_other_admin/);
    expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('cli_draft');
    expect((screen.getByLabelText('App secret') as HTMLInputElement).value).toBe('DRAFT_ONLY_SECRET');
    mocks.save.mockRejectedValue(new ApiError(422, 'callback authentication required'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText(/Check the application credentials/);
    expect(mocks.save.mock.calls.at(-1)?.[0].expected_revision).toBe('version-2');
  });
  it('drops old application group drafts when the company bot application changes', async () => {
    mocks.groups.mockResolvedValue({ tenant_id: 'company', installation_ref: 'old-app', revision: 'g1', routes: [{ chat_id: 'OLD_APP_ROUTE', agent_id: 'employee' }] });
    mocks.employees.mockResolvedValue([{ id: 'employee', tenant_id: 'company', name: 'Research employee' }]);
    const view = render(wrap(<TenantFeishuSettings selectedTenantId="company" />, client()));
    await waitFor(() => expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('cli_company'));
    const details = view.container.querySelector('details')!;
    details.open = true; fireEvent(details, new Event('toggle'));
    await screen.findByDisplayValue('OLD_APP_ROUTE');
    fireEvent.change(screen.getByLabelText('App ID'), { target: { value: 'cli_replacement' } });
    fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'replacement-secret' } });
    const next = { ...config, app_id: 'cli_replacement', revision: 'version-2' };
    mocks.save.mockImplementation(async () => { mocks.list.mockResolvedValue([next]); return next; });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Company bot settings saved.');
    expect(screen.queryByDisplayValue('OLD_APP_ROUTE')).toBeNull();
    expect(view.container.querySelector('details')!.open).toBe(false);
  });
  it('does not reset a previously configured custom platform on unrelated saves', async () => {
    mocks.list.mockResolvedValue([{ ...config, extra_config: { platform_region: 'custom', open_api_domain: 'https://approved.example' } }]);
    render(wrap(<TenantFeishuSettings selectedTenantId="company" />, client()));
    await waitFor(() => expect((screen.getByLabelText('Platform') as HTMLInputElement).value).toBe('keep'));
    mocks.save.mockRejectedValue(new ApiError(422, 'fixture response'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByRole('alert');
    expect(mocks.save.mock.calls[0][0]).not.toHaveProperty('platform_region');
    expect(mocks.save.mock.calls[0][0]).not.toHaveProperty('extra_config');
  });
  it('clears credentials on company/account change and ignores a late old read', async () => {
    const qc = client(); const view = render(wrap(<TenantFeishuSettings selectedTenantId="company" />, qc));
    await waitFor(() => expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('cli_company'));
    fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'OLD_COMPANY_SECRET' } });
    let finish!: (value: unknown) => void;
    mocks.list.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    mocks.auth.user = { id: 'other-owner', tenant_id: 'other-company', role: 'org_admin' };
    localStorage.setItem('current_tenant_id', 'other-company');
    mocks.list.mockResolvedValue([]); mocks.webhook.mockResolvedValue({ tenant_id: 'other-company', webhook_url: 'https://example.test/other' });
    view.rerender(wrap(<TenantFeishuSettings selectedTenantId="other-company" />, qc));
    await waitFor(() => expect((screen.getByLabelText('App ID').closest('fieldset') as HTMLFieldSetElement).disabled).toBe(false));
    await act(async () => { finish([config]); });
    expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('App secret') as HTMLInputElement).value).toBe('');
    expect(screen.queryByText(/Saved application: cli_company/)).toBeNull();
  });
});

describe('member company bot identity', () => {
  it('allows self-disconnect of a saved account link while the bot is paused', async () => {
    mocks.identity.mockResolvedValue({ ...identity, state: 'company_bot_paused', entries: [],
      disconnectable: [{ subject_id: 'ou_member', principal_id: 'principal' }] });
    render(wrap(<TenantFeishuIdentity />, client()));
    await screen.findByText('The company bot is paused.');
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect this identity' }));
    await waitFor(() => expect(mocks.action).toHaveBeenCalledWith('disconnect', 'principal', {
      expected_tenant_id: 'company', expected_user_id: 'owner' }));
  });
  it('rechecks actual status after disconnect and reconnect, with scoped principal actions', async () => {
    render(wrap(<TenantFeishuIdentity />, client()));
    await screen.findByText('Your identity is verified for this company bot.');
    mocks.action.mockImplementation(async (action: string) => {
      mocks.identity.mockResolvedValue(action === 'disconnect' ? { ...identity, state: 'reconnect_required',
        entries: [{ ...identity.entries[0], state: 'reconnect_required' }] } : identity);
      return { authority_bound: action !== 'disconnect' };
    });
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect this identity' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Reconnect this identity' }));
    await screen.findByRole('button', { name: 'Disconnect this identity' });
    expect(mocks.action.mock.calls.map((args) => args.slice(0, 2))).toEqual([['disconnect', 'principal'], ['reconnect', 'principal']]);
    expect(mocks.action.mock.calls[0][2]).toEqual({ expected_tenant_id: 'company', expected_user_id: 'owner' });
  });
  it('hides stale authority after an unknown action result and recovers by explicit refresh', async () => {
    render(wrap(<TenantFeishuIdentity />, client()));
    await screen.findByRole('button', { name: 'Disconnect this identity' });
    mocks.action.mockRejectedValue(new Error('response lost'));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect this identity' }));
    await screen.findByRole('alert');
    expect(screen.queryByText('Your identity is verified for this company bot.')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Disconnect this identity' })).toBeNull();
    mocks.identity.mockResolvedValue({ ...identity, state: 'identity_required', entries: [], principal_id: null });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Verify your Feishu account above, then refresh this status.');
  });
});
