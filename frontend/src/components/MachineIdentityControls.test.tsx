// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { machineIdentitiesApi, type MachineIdentity, type MachineIssueReceipt } from '../api/domains/machineIdentities';
import { useAuthStore } from '../stores';
import MachineIdentityControls from './MachineIdentityControls';
import { ApiError } from '../api/core';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_key: string, fallback: string) => fallback }) }));
const clients: QueryClient[] = [];
const identity: MachineIdentity = { id: 'principal', credential_id: 'credential', agent_id: 'employee', issue_request_id: 'request',
  name: 'Partner service', status: 'active', scopes: ['agent.describe'], expires_at: '2026-11-01T10:00:00Z',
  last_used_at: null, revoked_at: null, created_at: '2026-10-19T10:00:00Z' };
const page = (identities: MachineIdentity[] = [], can_issue = true) => ({ agent_id: 'employee', can_issue,
  supported_scopes: ['agent.describe', 'agent.execute', 'task.read', 'task.cancel'], identities });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });clients.push(client);
  render(<QueryClientProvider client={client}><MachineIdentityControls agentId="employee" /></QueryClientProvider>);
  return client;
}
beforeEach(() => { useAuthStore.setState({ user: { id: 'owner', tenant_id: 'company' } as any });
  localStorage.setItem('current_tenant_id', 'company');localStorage.setItem('token', 'user-login-token'); });
afterEach(() => { cleanup();clients.splice(0).forEach((client) => client.clear());vi.restoreAllMocks();localStorage.clear(); });

it('keeps a new machine secret outside the metadata query cache and user login', async () => {
  const list = vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  vi.spyOn(machineIdentitiesApi, 'issue').mockImplementation(async (_agent, body) => {
    expect(body).toMatchObject({ expected_user_id: 'owner', expected_tenant_id: 'company', scopes: ['agent.describe'] });
    const created = { ...identity, issue_request_id: body.issue_request_id };
    list.mockResolvedValue(page([created]));
    return { ...created, token: 'hmi_synthetic_new_secret', idempotent_replay: false, secret_status: 'available_once' };
  });
  const client = mount();
  fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Partner service' } });
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
  const secret = await screen.findByLabelText('New machine secret') as HTMLInputElement;
  expect(secret.value).toBe('hmi_synthetic_new_secret');
  expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(secret.value);
  expect(localStorage.getItem('token')).toBe('user-login-token');
  fireEvent.click(screen.getByRole('button', { name: 'Dismiss secret' }));
  expect(screen.queryByLabelText('New machine secret')).toBeNull();
});

it('reconciles a lost issuance response before another credential can be issued', async () => {
  const list = vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  const issue = vi.spyOn(machineIdentitiesApi, 'issue').mockImplementation(async (_agent, body) => {
    list.mockResolvedValue(page([{ ...identity, issue_request_id: body.issue_request_id }]));
    throw new Error('The transaction may have committed before the response was lost');
  });
  mount();fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Partner service' } });
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
  await screen.findByRole('alert');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByText('This credential was already issued. Its secret cannot be recovered; revoke it before issuing another.');
  expect((screen.getByRole('button', { name: 'Issue credential' }) as HTMLButtonElement).disabled).toBe(true);
  expect(issue).toHaveBeenCalledTimes(1);
});

it('issues only explicitly selected task permissions and shows the task endpoint', async () => {
  const list = vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  const issue = vi.spyOn(machineIdentitiesApi, 'issue').mockImplementation(async (_agent, body) => {
    const created = { ...identity, issue_request_id: body.issue_request_id, scopes: body.scopes };
    list.mockResolvedValue(page([created]));
    return { ...created, token: 'hmi_explicit_task_scopes', idempotent_replay: false, secret_status: 'available_once' };
  });
  mount();
  fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Task integration' } });
  fireEvent.click(screen.getByLabelText('Run tasks'));
  fireEvent.click(screen.getByLabelText('Read task status and files'));
  fireEvent.click(screen.getByLabelText('Cancel queued or running tasks'));
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
  await screen.findByLabelText('New machine secret');
  expect(issue.mock.calls[0][1].scopes).toEqual(['agent.describe', 'agent.execute', 'task.read', 'task.cancel']);
  expect(screen.getByText('/api/agents/employee/a2a/rpc')).toBeTruthy();
  expect(screen.getByText('agent.describe, agent.execute, task.read, task.cancel')).toBeTruthy();
});

it('requires at least one permission instead of silently issuing broader access', async () => {
  vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  const issue = vi.spyOn(machineIdentitiesApi, 'issue');
  mount();
  const name = await screen.findByLabelText('Integration name');
  fireEvent.change(name, { target: { value: 'No permissions' } });
  fireEvent.click(screen.getByLabelText('Read employee description'));
  expect((screen.getByRole('button', { name: 'Issue credential' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.submit(name.closest('form')!);
  await screen.findByText('Choose at least one permission.');
  expect(issue).not.toHaveBeenCalled();
});

it('hides an in-flight secret after the authenticated account changes', async () => {
  vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  let resolve!: (value: MachineIssueReceipt) => void;
  const issue = vi.spyOn(machineIdentitiesApi, 'issue').mockImplementation(() => new Promise((done) => { resolve = done; }));
  mount();fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Partner service' } });
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
  await waitFor(() => expect(issue).toHaveBeenCalledTimes(1));
  act(() => { useAuthStore.setState({ user: { id: 'other-owner', tenant_id: 'company' } as any }); });
  await act(async () => { resolve({ ...identity, issue_request_id: issue.mock.calls[0][1].issue_request_id,
    token: 'hmi_stale_account_secret', idempotent_replay: false, secret_status: 'available_once' }); });
  expect(screen.queryByLabelText('New machine secret')).toBeNull();
});

it('lets the current owner revoke while preserving administrator-only issuance', async () => {
  const list = vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page([identity], false));
  const revoke = vi.spyOn(machineIdentitiesApi, 'revoke').mockImplementation(async (_agent, id, body) => {
    expect(id).toBe('principal');expect(body).toEqual({ expected_user_id: 'owner', expected_tenant_id: 'company' });
    list.mockResolvedValue(page([{ ...identity, status: 'revoked' }], false));
    return { id, agent_id: 'employee', status: 'revoked', revoked_at: '2026-10-19T10:01:00Z' };
  });
  mount();fireEvent.click(await screen.findByRole('button', { name: 'Revoke access' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Revoke access' })).toBeNull());
  expect(screen.queryByRole('button', { name: 'Issue credential' })).toBeNull();
  expect(revoke).toHaveBeenCalledTimes(1);
});

it('retries the same frozen issuance after a refresh finds no committed credential', async () => {
  const list = vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  const issue = vi.spyOn(machineIdentitiesApi, 'issue').mockRejectedValueOnce(new Error('Response lost'))
    .mockImplementationOnce(async (_agent, body) => {
      const created = { ...identity, issue_request_id: body.issue_request_id };
      list.mockResolvedValue(page([created]));
      return { ...created, token: 'hmi_recovered_secret', idempotent_replay: false, secret_status: 'available_once' };
    });
  mount();fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Original integration' } });
  fireEvent.click(screen.getByLabelText('Read task status and files'));
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
  await screen.findByRole('alert');fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  const retry = await screen.findByRole('button', { name: 'Retry issuance' });
  await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false));
  expect((screen.getByLabelText('Integration name') as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByLabelText('Read task status and files').matches(':disabled')).toBe(true);
  fireEvent.click(retry);await screen.findByLabelText('New machine secret');
  expect(issue.mock.calls[1][1]).toEqual(issue.mock.calls[0][1]);
  expect(issue.mock.calls[1][1].scopes).toEqual(['agent.describe', 'task.read']);
});

it('clears the secret when the selected company changes in the same account', async () => {
  vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  vi.spyOn(machineIdentitiesApi, 'issue').mockImplementation(async (_agent, body) => ({ ...identity,
    issue_request_id: body.issue_request_id, token: 'hmi_original_company_secret', idempotent_replay: false,
    secret_status: 'available_once' }));
  mount();fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Original integration' } });
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));await screen.findByLabelText('New machine secret');
  act(() => { localStorage.setItem('current_tenant_id', 'other-company');
    window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'other-company' })); });
  await waitFor(() => expect(screen.queryByLabelText('New machine secret')).toBeNull());
});

it('allows expiry correction after the server confirms no credential was issued', async () => {
  vi.spyOn(machineIdentitiesApi, 'list').mockResolvedValue(page());
  vi.spyOn(machineIdentitiesApi, 'issue').mockRejectedValue(new ApiError(422, 'Expiry elapsed', { code: 'machine_expiry_required' }));
  mount();fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Original integration' } });
  fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
  await screen.findByText('Enter an integration name and a future expiry time.');
  expect((screen.getByLabelText('Expires at') as HTMLInputElement).disabled).toBe(false);
  expect((screen.getByLabelText('Integration name') as HTMLInputElement).value).toBe('Original integration');
  expect(screen.queryByLabelText('New machine secret')).toBeNull();
});
