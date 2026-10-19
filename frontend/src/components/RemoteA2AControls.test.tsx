// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { remoteA2AApi, type RemoteA2AConnection } from '../api/domains/remoteA2A';
import { useAuthStore } from '../stores';
import RemoteA2AControls from './RemoteA2AControls';
import { ApiError } from '../api/core';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const clients: QueryClient[] = [];
const page = (overrides: Partial<RemoteA2AConnection> = {}): RemoteA2AConnection => ({ agent_id: 'employee', state: 'not_configured',
  version: null, request_id: null, can_configure: true, can_disable: false, card_url: null, rpc_url: null,
  credential_configured: false, allowed_networks: [], max_response_bytes: 16777216, timeout_seconds: 20,
  discovered_at: null, discovery_error: null, agent_card: null, effect_control: 'unverified', cost_metering: 'unknown',
  runtime_activation: 'not_activated', ...overrides });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });clients.push(client);
  render(<QueryClientProvider client={client}><RemoteA2AControls agentId="employee" /></QueryClientProvider>);
  return client;
}
async function fill() {
  fireEvent.change(await screen.findByLabelText('Agent Card URL'), { target: { value: 'https://remote.test/card' } });
  fireEvent.change(screen.getByLabelText('Task interface URL'), { target: { value: 'https://remote.test/rpc' } });
  fireEvent.change(screen.getByLabelText('Machine credential (leave blank to keep)'), { target: { value: 'synthetic-remote-only' } });
}
beforeEach(() => {
  useAuthStore.setState({ user: { id: 'owner', tenant_id: 'company' } as any });
  localStorage.setItem('current_tenant_id', 'company');localStorage.setItem('token', 'personal-login-only');
});
afterEach(() => { cleanup();clients.splice(0).forEach((client) => client.clear());vi.restoreAllMocks();localStorage.clear(); });

it('saves a separate credential outside global caches and preserves truthful activation limits', async () => {
  const get = vi.spyOn(remoteA2AApi, 'get').mockResolvedValue(page());
  const configure = vi.spyOn(remoteA2AApi, 'configure').mockImplementation(async (_agent, body) => {
    expect(body).toMatchObject({ expected_user_id: 'owner', expected_tenant_id: 'company', expected_version: null,
      bearer_token: 'synthetic-remote-only', card_url: 'https://remote.test/card', rpc_url: 'https://remote.test/rpc' });
    const saved = page({ version: 1, request_id: body.request_id, state: 'configured', credential_configured: true });
    get.mockResolvedValue(saved);return saved;
  });
  const client = mount();await fill();fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('Configured');
  expect(configure).toHaveBeenCalledTimes(1);
  expect((screen.getByLabelText('Machine credential (leave blank to keep)') as HTMLInputElement).value).toBe('');
  expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain('synthetic-remote-only');
  expect(client.getMutationCache().getAll()).toHaveLength(0);
  expect(localStorage.getItem('token')).toBe('personal-login-only');
  expect(screen.getByText(/Employee dispatch is not activated/)).toBeTruthy();
});

it('retries the exact nonce and body after a lost save response', async () => {
  const get = vi.spyOn(remoteA2AApi, 'get').mockResolvedValue(page());
  const configure = vi.spyOn(remoteA2AApi, 'configure').mockRejectedValueOnce(new Error('Response lost'))
    .mockImplementation(async (_agent, body) => {
      const saved = page({ version: 1, request_id: body.request_id, state: 'configured' });get.mockResolvedValue(saved);return saved;
    });
  mount();await fill();fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Agent Card URL').matches(':disabled')).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Retry same save' }));
  await screen.findByText('Configured');
  expect(configure).toHaveBeenCalledTimes(2);
  expect(configure.mock.calls[1][1]).toEqual(configure.mock.calls[0][1]);
});

it('reconciles an already persisted save without resending its credential', async () => {
  const get = vi.spyOn(remoteA2AApi, 'get').mockResolvedValue(page());
  const configure = vi.spyOn(remoteA2AApi, 'configure').mockImplementation(async (_agent, body) => {
    get.mockResolvedValue(page({ version: 1, request_id: body.request_id, state: 'configured' }));
    throw new Error('Transaction committed but response lost');
  });
  mount();await fill();fireEvent.click(screen.getByRole('button', { name: 'Save' }));await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect((screen.getByLabelText('Machine credential (leave blank to keep)') as HTMLInputElement).value).toBe('');
  expect(configure).toHaveBeenCalledTimes(1);
});

it('lets the current owner disable a connection while configuration requires an administrator', async () => {
  const configured = page({ can_configure: false, can_disable: true, state: 'discovered', version: 1, request_id: 'original' });
  const get = vi.spyOn(remoteA2AApi, 'get').mockResolvedValue(configured);
  const disable = vi.spyOn(remoteA2AApi, 'disable').mockImplementation(async (_agent, body) => {
    expect(body).toEqual({ expected_user_id: 'owner', expected_tenant_id: 'company', expected_version: 1 });
    const result = { ...configured, version: 2, state: 'disabled' };get.mockResolvedValue(result);return result;
  });
  mount();fireEvent.click(await screen.findByRole('button', { name: 'Disable connection' }));
  await screen.findByText('Disabled');expect(disable).toHaveBeenCalledTimes(1);
  expect(screen.queryByLabelText('Machine credential (leave blank to keep)')).toBeNull();
});

it('clears the old account credential and rejects its late save response', async () => {
  vi.spyOn(remoteA2AApi, 'get').mockResolvedValue(page());
  let finish!: (value: RemoteA2AConnection) => void;
  const configure = vi.spyOn(remoteA2AApi, 'configure').mockImplementation(() => new Promise((done) => { finish = done; }));
  const client = mount();await fill();fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(configure).toHaveBeenCalledTimes(1));
  await act(async () => { useAuthStore.setState({ user: { id: 'other-owner', tenant_id: 'company' } as any }); });
  expect((await screen.findByLabelText('Machine credential (leave blank to keep)') as HTMLInputElement).value).toBe('');
  await act(async () => { finish(page({ state: 'discovered', version: 1, request_id: configure.mock.calls[0][1].request_id })); });
  expect(screen.queryByText('Agent Card read')).toBeNull();
  expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain('synthetic-remote-only');
});

it.each([
  { status: 503, code: 'remote_a2a_credential_unavailable' },
  { status: 409, code: 'remote_a2a_new_endpoint_credential_required' },
])('allows credential repair after a confirmed $code denial', async ({ status, code }) => {
  const get = vi.spyOn(remoteA2AApi, 'get').mockResolvedValue(page({ state: 'configured', version: 1, request_id: 'prior',
    credential_configured: true, card_url: 'https://remote.test/card', rpc_url: 'https://remote.test/rpc' }));
  const configure = vi.spyOn(remoteA2AApi, 'configure')
    .mockRejectedValueOnce(new ApiError(status, 'Credential repair required', { code }))
    .mockImplementation(async (_agent, body) => {
      const saved = page({ version: 2, request_id: body.request_id, state: 'configured', credential_configured: true });
      get.mockResolvedValue(saved);return saved;
    });
  mount();await screen.findByLabelText('Machine credential (leave blank to keep)');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByRole('alert');
  expect(screen.getByLabelText('Machine credential (leave blank to keep)').matches(':disabled')).toBe(false);
  fireEvent.change(screen.getByLabelText('Machine credential (leave blank to keep)'), { target: { value: 'synthetic-replacement' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(configure).toHaveBeenCalledTimes(2));
  expect(configure.mock.calls[1][1].bearer_token).toBe('synthetic-replacement');
  expect(configure.mock.calls[1][1].request_id).not.toBe(configure.mock.calls[0][1].request_id);
});
