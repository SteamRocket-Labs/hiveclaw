// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import TenantCompanyIdentity from './TenantCompanyIdentity';

const mocks = vi.hoisted(() => ({ user: { id: 'member', role: 'member', tenant_id: 'company' }, read: vi.fn(), issue: vi.fn(), disconnect: vi.fn() }));
vi.mock('../stores', () => ({ useAuthStore: (select: (s: { user: typeof mocks.user }) => unknown) => select({ user: mocks.user }) }));
vi.mock('../api/domains/channels', () => ({ channelApi: { companyIdentity: mocks.read, companyIdentityChallenge: mocks.issue, companyIdentityDisconnect: mocks.disconnect } }));
const scope = { expected_tenant_id: 'company', expected_user_id: 'member' };
const identity = (provider = 'dingtalk', state = 'verification_required') => ({ tenant_id: 'company', user_id: 'member', provider,
  state, principal_id: state === 'bound' ? 'principal' : null, subject_id: 'staff-one', disconnectable: state === 'bound' ? [{ principal_id: 'principal', subject_id: 'staff-one' }] : [] });
let instance: ReturnType<typeof createInstance>;
beforeEach(async () => {
  vi.resetAllMocks(); localStorage.clear(); mocks.user = { id: 'member', role: 'member', tenant_id: 'company' };
  mocks.read.mockImplementation(async (provider) => identity(provider));
  mocks.issue.mockResolvedValue({ tenant_id: 'company', user_id: 'member', subject_id: 'staff-one', challenge_id: 'nonce', command: '/hive-connect PRIVATE_COMMAND', expires_at: '2026-10-19T10:00:00Z' });
  instance = createInstance(); await instance.init({ lng: 'en', resources: { en: { translation: en } } });
});
afterEach(cleanup);
function mount() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = () => <I18nextProvider i18n={instance}><MemoryRouter><QueryClientProvider client={cache}><TenantCompanyIdentity /></QueryClientProvider></MemoryRouter></I18nextProvider>;
  const page = render(view());
  const details = page.container.querySelector('details')!;
  details.open = true; fireEvent(details, new Event('toggle'));
  return { ...page, update: () => page.rerender(view()) };
}

it('issues only for the current member and waits for callback verification before showing bound', async () => {
  mount();
  fireEvent.click(await screen.findByRole('button', { name: en.companyMessaging.verify }));
  await screen.findByDisplayValue('/hive-connect PRIVATE_COMMAND');
  expect(mocks.issue).toHaveBeenCalledWith('dingtalk', scope);
  expect(screen.queryByText(en.companyMessaging.bound)).toBeNull();
  mocks.read.mockResolvedValue(identity('dingtalk', 'bound'));
  fireEvent.click(screen.getByRole('button', { name: en.common.refresh }));
  await screen.findByText(en.companyMessaging.bound);
  expect(screen.queryByDisplayValue('/hive-connect PRIVATE_COMMAND')).toBeNull();
  expect(screen.getByRole('link', { name: en.assistant.title }).getAttribute('href')).toBe('/assistant');
  mocks.disconnect.mockResolvedValue({ principal_id: 'principal', authority_bound: false });
  fireEvent.click(screen.getByRole('button', { name: en.tenantFeishu.disconnect }));
  await waitFor(() => expect(mocks.disconnect).toHaveBeenCalledWith('dingtalk', 'principal', scope));
});

it('discards a late private command when the selected provider changes', async () => {
  let finish!: (value: unknown) => void;
  mocks.issue.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  mount();
  fireEvent.click(await screen.findByRole('button', { name: en.companyMessaging.verify }));
  fireEvent.change(screen.getByLabelText(en.companyDirectory.provider), { target: { value: 'wecom' } });
  await waitFor(() => expect(mocks.read).toHaveBeenCalledWith('wecom', scope, expect.any(AbortSignal)));
  await act(async () => { finish({ tenant_id: 'company', user_id: 'member', command: 'OLD_PRIVATE_TOKEN', expires_at: '2026-10-19T10:00:00Z' }); });
  expect(screen.queryByDisplayValue('OLD_PRIVATE_TOKEN')).toBeNull();
});

it('rejects a foreign account response and requires a fresh identity read after an unconfirmed challenge', async () => {
  mocks.issue.mockResolvedValue({ tenant_id: 'company', user_id: 'other-member', command: 'FOREIGN_PRIVATE_TOKEN' });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: en.companyMessaging.verify }));
  await screen.findByRole('alert');
  expect(screen.queryByDisplayValue('FOREIGN_PRIVATE_TOKEN')).toBeNull();
  expect(screen.queryByRole('button', { name: en.companyMessaging.verify })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: en.common.refresh }));
  await screen.findByRole('button', { name: en.companyMessaging.verify });
});
