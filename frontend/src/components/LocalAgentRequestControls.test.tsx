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
  preview: 'Original local work', created_at: '2026-10-19T01:00:00Z', control: null, can_cancel: true, can_abandon: false, source_delivery: null, can_retry_source_return: false, decision_reason: null, tracking_state: 'open' as const, disposition: null, next_action: null };
function page(overrides: Partial<LocalAgentRequestPage> = {}): LocalAgentRequestPage {
  return { scope: { actor_user_id: 'owner', tenant_id: 'tenant', session_id: 'work' }, requests: [request], next_cursor: null, ...overrides };
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  render(<QueryClientProvider client={client}><LocalAgentRequestControls sessionId="work" /></QueryClientProvider>);
  fireEvent.click(screen.getByText('Local requests and cancellation'));
}
it('links a newly held request to the actual employee approval route', async () => {
  vi.spyOn(localBridgeApi, 'listChannelRequests').mockResolvedValue(page({ requests: [{ ...request,
    status: 'waiting_approval', target_agent_id: 'target-employee', approval_id: 'exact-decision' }] }));
  mount();
  const link = await screen.findByRole('link', { name: 'Review this employee’s approvals' });
  expect(link.getAttribute('href')).toBe('/agents/target-employee#approvals');
  expect(screen.getByText('Waiting for approval')).toBeTruthy();
});
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
it('ends tracking a held unknown request without claiming a device stop', async () => {
  const held = { ...request, status: 'needs_reconciliation', execution_state: 'unknown', can_abandon: true };
  const read = vi.spyOn(localBridgeApi, 'listChannelRequests').mockResolvedValue(page({ requests: [held] }));
  const abandon = vi.spyOn(localBridgeApi, 'abandonChannelRequest').mockImplementation(async (_session, _message, command) => {
    const receipt = { schema: 'hive.local_work_disposition.v1' as const, control_id: 'disposition', action: 'abandon' as const,
      message_id: 'request', session_id: 'work', actor_user_id: 'owner', request_hash: request.request_hash,
      status: 'abandoned' as const, execution_state: 'unknown' as const, next_action: 'continue_original_work', recorded_at: 'now' };
    expect(command).toMatchObject({ expected_user_id: 'owner', expected_tenant_id: 'tenant', request_hash: request.request_hash });
    read.mockResolvedValue(page({ requests: [{ ...held, can_cancel: false, can_abandon: false, tracking_state: 'abandoned', disposition: receipt }] }));
    return receipt;
  });
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'End tracking this request' }));
  await screen.findByText('Tracking ended. External execution remains unknown.');
  expect(abandon).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: 'End tracking this request' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Cancel this request' })).toBeNull();
});
it('retries only the retained result return and explains a rejected action', async () => {
  const held = { ...request, status: 'rejected', execution_state: 'not_dispatched', can_cancel: false,
    decision_reason: 'The current policy did not release this action.', can_retry_source_return: true,
    source_delivery: { status: 'needs_reconciliation' as const } };
  const read = vi.spyOn(localBridgeApi, 'listChannelRequests').mockResolvedValue(page({ requests: [held] }));
  const retry = vi.spyOn(localBridgeApi, 'retryChannelSourceReturn').mockImplementation(async (_work, _id, body) => {
    expect(body).toEqual({ expected_user_id: 'owner', expected_tenant_id: 'tenant', request_hash: request.request_hash });
    const delivery = { status: 'queued' as const, notification_id: 'original-notice' };
    read.mockResolvedValue(page({ requests: [{ ...held, source_delivery: delivery, can_retry_source_return: false }] }));
    return { schema: 'hive.local_source_return.v1', message_id: request.id, session_id: 'work', actor_user_id: 'owner',
      status: 'rejected', source_delivery: delivery, reexecuted: false };
  });
  mount();
  await screen.findByText('Action was not released');
  expect(screen.getByText('The current policy did not release this action.')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Retry original-work return' }));
  await screen.findByText('The original-work return is recorded. Execution is not repeated.');
  expect(retry).toHaveBeenCalledTimes(1);
});
