// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { localBridgeApi, type LocalAgentRequestPage } from '../api/domains/localBridge';
import { useAuthStore } from '../stores';
import LocalAgentRequestControls from './LocalAgentRequestControls';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const clients: QueryClient[] = [];
const request = { id: 'request', request_hash: 'a'.repeat(64), status: 'pending', execution_state: 'not_dispatched',
  preview: 'Original local work', created_at: '2026-10-19T01:00:00Z', control: null, can_cancel: true, next_action: null };
function page(overrides: Partial<LocalAgentRequestPage> = {}): LocalAgentRequestPage {
  return { scope: { actor_user_id: 'owner', tenant_id: 'tenant', session_id: 'work' }, requests: [request], next_cursor: null, ...overrides };
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  render(<QueryClientProvider client={client}><LocalAgentRequestControls sessionId="work" /></QueryClientProvider>);
  fireEvent.click(screen.getByText('Local requests and cancellation'));
}
beforeEach(() => {
  useAuthStore.setState({ user: { id: 'owner', tenant_id: 'tenant' } as any });
  localStorage.setItem('current_tenant_id', 'tenant');
});
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); localStorage.clear(); });

it('cancels the exact request and shows the canonical queue outcome', async () => {
  const read = vi.spyOn(localBridgeApi, 'listChannelRequests').mockResolvedValue(page());
  const cancel = vi.spyOn(localBridgeApi, 'cancelChannelRequest').mockImplementation(async (_session, _message, body) => {
    const receipt = { schema: 'hive.local_message_control.v1' as const, control_id: 'control', action: 'cancel' as const,
      message_id: 'request', session_id: 'work', actor_user_id: 'owner', status: 'cancelled' as const,
      execution_state: 'not_dispatched', next_action: 'continue_original_work', recorded_at: 'now' };
    read.mockResolvedValue(page({ requests: [{ ...request, status: 'cancelled', can_cancel: false, control: receipt }] }));
    expect(body).toMatchObject({ expected_user_id: 'owner', expected_tenant_id: 'tenant', request_hash: request.request_hash });
    return receipt;
  });
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Cancel this request' }));
  await screen.findByText('Cancelled before dispatch');
  expect(cancel).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: 'Cancel this request' })).toBeNull();
});
it('retains a control key after an ambiguous response and explicit refresh', async () => {
  vi.spyOn(localBridgeApi, 'listChannelRequests').mockResolvedValue(page());
  const cancel = vi.spyOn(localBridgeApi, 'cancelChannelRequest').mockRejectedValue(new Error('Connection lost'));
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Cancel this request' }));
  await screen.findByRole('alert');
  expect((screen.getByRole('button', { name: 'Cancel this request' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect((screen.getByRole('button', { name: 'Cancel this request' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Cancel this request' }));
  await waitFor(() => expect(cancel).toHaveBeenCalledTimes(2));
  expect(cancel.mock.calls[0][2].idempotency_key).toBe(cancel.mock.calls[1][2].idempotency_key);
});
it('discards the old account and rejects data from another work', async () => {
  const read = vi.spyOn(localBridgeApi, 'listChannelRequests').mockResolvedValue(page());
  const cancel = vi.spyOn(localBridgeApi, 'cancelChannelRequest');
  mount(); await screen.findByText('Original local work');
  read.mockResolvedValue(page({ scope: { actor_user_id: 'owner', tenant_id: 'tenant', session_id: 'other-work' } }));
  act(() => { useAuthStore.setState({ user: { id: 'other-owner', tenant_id: 'tenant' } as any }); });
  await screen.findByRole('alert'); expect(screen.queryByText('Original local work')).toBeNull();
  expect(cancel).not.toHaveBeenCalled();
});
it('loads older requests and identifies late reported results', async () => {
  const read = vi.spyOn(localBridgeApi, 'listChannelRequests').mockImplementation(async (_id, cursor) => cursor
    ? page({ requests: [{ ...request, id: 'older', preview: 'Older original work', status: 'completed',
      execution_state: 'reported_completed', can_cancel: false, next_action: 'review_late_result' }] })
    : page({ next_cursor: 'request' }));
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Load older requests' }));
  await screen.findByText('Older original work'); expect(read).toHaveBeenCalledWith('work', 'request');
  expect(screen.getByText('A late result arrived. Review it in this work; the earlier control remains unconfirmed.')).toBeTruthy();
});
