// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { localBridgeApi } from '../api/domains/localBridge';
import { useAuthStore } from '../stores';
import LocalAgentDeviceControls from './LocalAgentDeviceControls';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const clients: QueryClient[] = [];
const device = { id: 'device', tenant_id: 'tenant', agent_id: 'employee', user_id: 'owner', device_name: 'Original device',
  client_kind: 'codex', status: 'active', scopes: [] };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  const view = render(<QueryClientProvider client={client}><LocalAgentDeviceControls agentId="employee" /></QueryClientProvider>);
  const details = view.container.querySelector('details')!; details.open = true; fireEvent(details, new Event('toggle'));
}
beforeEach(() => {
  useAuthStore.setState({ user: { id: 'owner', tenant_id: 'tenant' } as any });
  localStorage.setItem('current_tenant_id', 'tenant');
});
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); vi.restoreAllMocks(); localStorage.clear(); });

it('revokes the exact device and keeps host execution unconfirmed', async () => {
  const read = vi.spyOn(localBridgeApi, 'listAgentConnections').mockResolvedValue({ connections: [device] });
  const revoke = vi.spyOn(localBridgeApi, 'revokeAgentConnection').mockImplementation(async () => {
    const control = { schema: 'hive.local_connection_control.v1' as const, connection_id: 'device', actor_user_id: 'owner',
      request_counts: { cancelled_before_dispatch: 1, needs_reconciliation: 1 }, host_execution_state: 'unknown' as const,
      stop_confirmation: 'unavailable' as const, next_action: 'inspect_original_runner', recorded_at: 'now' };
    read.mockResolvedValue({ connections: [{ ...device, status: 'revoked', work_control: control }] });
    return { status: 'revoked', connection_id: 'device', work_control: control };
  });
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Revoke this device access' }));
  await screen.findByText('Device access revoked. Host execution remains unknown; a device stop has not been confirmed.');
  expect(revoke).toHaveBeenCalledWith('employee', 'device', { expected_user_id: 'owner', expected_tenant_id: 'tenant' });
  expect(screen.queryByRole('button', { name: 'Revoke this device access' })).toBeNull();
});
it('discards old account controls and refuses a foreign device list', async () => {
  const read = vi.spyOn(localBridgeApi, 'listAgentConnections').mockResolvedValue({ connections: [device] });
  const revoke = vi.spyOn(localBridgeApi, 'revokeAgentConnection');
  mount(); await screen.findByRole('button', { name: 'Revoke this device access' });
  read.mockResolvedValue({ connections: [{ ...device, tenant_id: 'another-company' }] });
  act(() => { useAuthStore.setState({ user: { id: 'other-user', tenant_id: 'tenant' } as any }); });
  expect(screen.queryByText(/Original device/)).toBeNull();
  const details = document.querySelector('details')!; details.open = true; fireEvent(details, new Event('toggle'));
  await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
  expect(screen.queryByRole('button', { name: 'Revoke this device access' })).toBeNull();
  expect(revoke).not.toHaveBeenCalled();
});
