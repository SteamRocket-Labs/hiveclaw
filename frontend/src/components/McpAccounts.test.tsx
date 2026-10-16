// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import McpAccounts, { McpAccountControls } from './McpAccounts';

const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); });
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
const company = { id: 'company', name: 'Documents', owner_user_id: null, parent_server_id: null, auth_status: 'configured' };
const personal = { id: 'personal', name: 'My documents', owner_user_id: 'owner', parent_server_id: 'company', auth_status: 'configured',
  account_use_enabled: true, grant_version: 3, can_grant_account: true, can_authorize: true };

it('sends the exact owner, employee and observed grant version, preserving a conflicting decision for readback', async () => {
  const calls: Array<{ url: string; body: unknown }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    return calls.length === 1 ? response({ detail: 'Grant changed; read back first' }, 409) : response({ enabled: true, version: 5 });
  });
  const reviewed = vi.fn(); const select = vi.fn();
  const view = render(<McpAccountControls agentId="employee" serverId="company" accounts={[company, personal]}
    accountId="personal" onSelect={select} onReviewed={reviewed} />);
  fireEvent.click(screen.getByRole('button', { name: 'Remove use for my work' }));
  await screen.findByRole('alert'); expect(reviewed).not.toHaveBeenCalled(); expect(select).not.toHaveBeenCalled();
  expect(calls[0]).toEqual({ url: '/api/mcp-accounts/personal/grants/employee/owner', body: { enabled: false, expected_version: 3 } });
  view.rerender(<McpAccountControls agentId="employee" serverId="company" accounts={[company, { ...personal, account_use_enabled: false, grant_version: 4 }]}
    accountId="personal" onSelect={select} onReviewed={reviewed} />);
  fireEvent.click(screen.getByRole('button', { name: 'Allow this employee to use it for my work' }));
  await waitFor(() => expect(reviewed).toHaveBeenCalledTimes(1));
  expect(calls[1].body).toEqual({ enabled: true, expected_version: 4 });
  expect(calls).toHaveLength(2);
});

it('reuses the same creation identity when saving succeeded but selecting the original work failed', async () => {
  const bodies: Array<{ id: string; name: string }> = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    const body = JSON.parse(String(init?.body)); bodies.push(body); return response({ ...personal, id: body.id, name: body.name });
  });
  const select = vi.fn().mockRejectedValueOnce(new Error('Selection unavailable')).mockResolvedValueOnce(undefined);
  const reviewed = vi.fn();
  render(<McpAccountControls agentId="employee" serverId="company" accounts={[company]} accountId="company" onSelect={select} onReviewed={reviewed} />);
  fireEvent.click(screen.getByRole('button', { name: 'Connect my account' }));
  fireEvent.change(screen.getByLabelText('Account label'), { target: { value: 'Research documents' } });
  fireEvent.click(screen.getByRole('button', { name: 'Continue connecting' }));
  await screen.findByText('Selection unavailable');
  expect((screen.getByLabelText('Account label') as HTMLInputElement).value).toBe('Research documents');
  expect(reviewed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Continue connecting' }));
  await waitFor(() => expect(reviewed).toHaveBeenCalledTimes(1));
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
  expect(select).toHaveBeenLastCalledWith(bodies[0].id);
});

it('refreshes the profile authorization readback after an account-use grant is revoked', async () => {
  let enabled = true; let reads = 0;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    if (init?.method === 'PUT') { enabled = false; return response({ enabled, version: 4 }); }
    if (String(url).endsWith('/oauth')) {
      reads += 1;
      return response({ server_id: 'personal', auth_status: 'configured', attempt: null,
        authorized_agents: enabled ? [{ id: 'employee', name: 'Research employee' }] : [] });
    }
    return response([company, { ...personal, account_use_enabled: enabled, grant_version: enabled ? 3 : 4 }]);
  });
  render(<McpAccounts agentId="employee" serverId="company" />);
  fireEvent.change(await screen.findByRole('combobox'), { target: { value: 'personal' } });
  await screen.findByText('Research employee');
  fireEvent.click(screen.getByRole('button', { name: 'Remove use for my work' }));
  await screen.findByText('No employee has connection-use permission yet.');
  expect(screen.queryByText('Research employee')).toBeNull();
  expect(reads).toBeGreaterThanOrEqual(2);
});
