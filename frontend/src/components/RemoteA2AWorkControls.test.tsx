// @vitest-environment jsdom
import { StrictMode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { remoteA2AApi, type RemoteA2ATask, type RemoteA2ATasks } from '../api/domains/remoteA2A';
import { useAuthStore } from '../stores';
import RemoteA2AWorkControls from './RemoteA2AWorkControls';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const clients: QueryClient[] = [];
const page = (): RemoteA2ATasks => ({ agent_id: 'employee', source_session_id: 'original-work',
  connection: { ready: true, version: 1 }, items: [] });
const task = (overrides: Partial<RemoteA2ATask> = {}): RemoteA2ATask => ({ id: 'transport-task', agent_id: 'employee',
  source_session_id: 'original-work', request_id: 'request', latest_operation_id: 'request', status: 'suspended',
  configuration_version: 1,
  remote_task_id: 'remote-task', remote_context_id: 'remote-context', remote_state: 'TASK_STATE_INPUT_REQUIRED',
  error_code: null, cost_metering: 'unknown', effect_control: 'unverified', local_abandoned: false,
  recovery_actions: ['read', 'refresh', 'send_message', 'cancel', 'abandon'], ...overrides });

async function mount(strict = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });clients.push(client);
  const component = <QueryClientProvider client={client}><RemoteA2AWorkControls agentId="employee" sessionId="original-work" /></QueryClientProvider>;
  const { container } = render(strict ? <StrictMode>{component}</StrictMode> : component);
  const details = container.querySelector('details')!;
  await act(async () => { details.open = true;details.dispatchEvent(new Event('toggle')); });
  return client;
}
async function fill() {
  fireEvent.change(await screen.findByLabelText('Remote request or reply'), { target: { value: 'Full original request\nKeep all evidence.' } });
  fireEvent.click(screen.getByLabelText('I authorize this exact remote request with unverified internal effect controls.'));
  fireEvent.click(screen.getByLabelText('I accept unknown external charges for this request; no hard spend limit is claimed.'));
}
beforeEach(() => {
  useAuthStore.setState({ user: { id: 'owner', tenant_id: 'company' } as any });
  localStorage.setItem('current_tenant_id', 'company');
  vi.spyOn(remoteA2AApi, 'tasks').mockResolvedValue(page());
  vi.spyOn(remoteA2AApi, 'task').mockResolvedValue(task());
});
afterEach(() => { cleanup();clients.splice(0).forEach((c) => c.clear());vi.restoreAllMocks();localStorage.clear(); });

it('loads the actual work controls under StrictMode', async () => {
  await mount(true);
  expect(await screen.findByLabelText('Remote request or reply')).toBeTruthy();
});

it('keeps a lost request nonce/body immutable and retries without another dispatch identity', async () => {
  const send = vi.spyOn(remoteA2AApi, 'startTask').mockRejectedValueOnce(new Error('Reply lost')).mockResolvedValue(task());
  await mount();await fill();
  fireEvent.click(screen.getByText('Send new remote request'));
  await screen.findByText('Retry exact request');
  const first = send.mock.calls[0][1];
  expect(first.source_session_id).toBe('original-work');
  expect(first.expected_user_id).toBe('owner');
  expect(first.params).toMatchObject({ message: { role: 'ROLE_USER', parts: [{ text: 'Full original request\nKeep all evidence.' }] } });
  expect((screen.getByLabelText('Remote request or reply') as HTMLTextAreaElement).matches(':disabled')).toBe(true);
  fireEvent.click(screen.getByText('Retry exact request'));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[1][1]).toEqual(first);
});

it('reconciles a saved request from the work without resending', async () => {
  const send = vi.spyOn(remoteA2AApi, 'startTask').mockRejectedValue(new Error('Reply lost'));
  await mount();await fill();fireEvent.click(screen.getByText('Send new remote request'));
  await screen.findByText('Retry exact request');
  const nonce = send.mock.calls[0][1].request_id;
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), items: [task({ request_id: nonce })] });
  fireEvent.click(screen.getByText('Read saved state'));
  await screen.findByLabelText('Saved remote receipt');
  expect(send).toHaveBeenCalledTimes(1);
  expect(screen.queryByText('Retry exact request')).toBeNull();
});

it('replies to the existing task/context and never changes original work', async () => {
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), items: [task()] });
  const operate = vi.spyOn(remoteA2AApi, 'operateTask').mockResolvedValue(task({ status: 'pending' }));
  await mount();fireEvent.click(await screen.findByText('transport-task'));
  await screen.findByLabelText('Saved remote receipt');
  fireEvent.change(screen.getByLabelText('Remote request or reply'), { target: { value: 'Use Q3.' } });
  fireEvent.click(screen.getByText('Reply to this remote task'));
  await waitFor(() => expect(operate).toHaveBeenCalledOnce());
  expect(operate.mock.calls[0].slice(0, 2)).toEqual(['employee', 'transport-task']);
  expect(operate.mock.calls[0][2]).toMatchObject({ action: 'send_message', expected_user_id: 'owner',
    params: { message: { taskId: 'remote-task', contextId: 'remote-context', parts: [{ text: 'Use Q3.' }] } } });
});

it('discards a late receipt after account scope changes', async () => {
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), items: [task()] });
  let resolve!: (value: RemoteA2ATask) => void;
  vi.mocked(remoteA2AApi.task).mockReturnValue(new Promise((r) => { resolve = r; }));
  await mount();fireEvent.click(await screen.findByText('transport-task'));
  await waitFor(() => expect(remoteA2AApi.task).toHaveBeenCalledOnce());
  act(() => useAuthStore.setState({ user: { id: 'other-owner', tenant_id: 'company' } as any }));
  await act(async () => resolve(task({ observation: { sensitive: 'old-owner-result' } })));
  expect(screen.queryByText(/old-owner-result/)).toBeNull();
});

it('recovers files from saved replies without sending a remote operation', async () => {
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), connection: { ready: false, version: null }, items: [task()] });
  const recover = vi.spyOn(remoteA2AApi, 'recoverArtifacts').mockResolvedValue({ agent_id: 'employee', task_id: 'transport-task',
    source_session_id: 'original-work', parts: [{ type: 'artifact' }], file_returns: [{ status: 'available' }] });
  const send = vi.spyOn(remoteA2AApi, 'operateTask');
  await mount();fireEvent.click(await screen.findByText('transport-task'));
  await screen.findByLabelText('Saved remote receipt');
  fireEvent.click(screen.getByText('Recover files from saved replies'));
  await waitFor(() => expect(recover).toHaveBeenCalledOnce());
  expect(recover.mock.calls[0]).toEqual(['employee', 'transport-task', { expected_user_id: 'owner', expected_tenant_id: 'company' }]);
  expect(send).not.toHaveBeenCalled();
  expect(await screen.findByText(/Files returned to this work/)).toBeTruthy();
});

it('rejects file receipts returned for another original work', async () => {
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), items: [task()] });
  vi.spyOn(remoteA2AApi, 'recoverArtifacts').mockResolvedValue({ agent_id: 'employee', task_id: 'transport-task',
    source_session_id: 'another-work', parts: [], file_returns: [] });
  await mount();fireEvent.click(await screen.findByText('transport-task'));
  await screen.findByLabelText('Saved remote receipt');
  fireEvent.click(screen.getByText('Recover files from saved replies'));
  await screen.findByRole('alert');
  expect(screen.queryByText(/Files returned to this work/)).toBeNull();
});

it('retries a locally unstarted operation in the same work with no replacement message and preserves a lost retry nonce', async () => {
  const denied = task({ status: 'needs_reconciliation', remote_task_id: null, remote_context_id: null,
    remote_state: null, error_code: 'remote_a2a_authority_unavailable', recovery_actions: ['read', 'retry', 'abandon'] });
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), connection: { ready: false, version: null }, items: [denied] });
  vi.mocked(remoteA2AApi.task).mockResolvedValue(denied);
  const operate = vi.spyOn(remoteA2AApi, 'operateTask').mockRejectedValueOnce(new Error('Retry reply lost'))
    .mockResolvedValue(task({ status: 'pending', recovery_actions: ['read'] }));
  await mount();fireEvent.click(await screen.findByText('transport-task'));
  await screen.findByLabelText('Saved remote receipt');
  fireEvent.change(screen.getByLabelText('Remote request or reply'), { target: { value: 'Must not replace the saved request.' } });
  fireEvent.click(screen.getByText('Retry saved operation after access repair'));
  await screen.findByText('Retry exact request');
  const first = operate.mock.calls[0][2];
  expect(first).toMatchObject({ action: 'retry', params: {}, expected_user_id: 'owner', expected_tenant_id: 'company', expected_version: 1 });
  expect(operate.mock.calls[0].slice(0, 2)).toEqual(['employee', 'transport-task']);
  fireEvent.click(screen.getByText('Retry exact request'));
  await waitFor(() => expect(operate).toHaveBeenCalledTimes(2));
  expect(operate.mock.calls[1][2]).toEqual(first);
});

it('does not offer a saved-operation retry for an unknown external outcome', async () => {
  const unknown = task({ status: 'needs_reconciliation', error_code: 'remote_a2a_response_lost',
    recovery_actions: ['read', 'refresh', 'cancel', 'abandon'] });
  vi.mocked(remoteA2AApi.tasks).mockResolvedValue({ ...page(), items: [unknown] });
  vi.mocked(remoteA2AApi.task).mockResolvedValue(unknown);
  await mount();fireEvent.click(await screen.findByText('transport-task'));
  await screen.findByLabelText('Saved remote receipt');
  expect(screen.queryByText('Retry saved operation after access repair')).toBeNull();
});
