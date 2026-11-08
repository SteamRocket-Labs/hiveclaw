// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import McpAccounts from './McpAccounts';
const translate = (key: string, fallback?: string) => translateFromCatalog(en, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
vi.mock('./McpOAuthSettings', () => ({ default: ({ serverId }: { serverId: string }) => <p>Reconnect {serverId}</p> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
const company = { id: 'company', name: 'Company documents', owner_user_id: null, parent_server_id: null, auth_status: 'none' };
const personal = { id: 'personal', name: 'My documents', owner_user_id: 'owner', parent_server_id: 'company', auth_status: 'configured', can_authorize: true };

it('shows reconnect and remove for each legacy account without add or use-grant controls', async () => {
  // S2 / item 6: owner-only connections have one account; legacy duplicates stay removable.
  const calls: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    calls.push(`${init?.method || 'GET'} ${String(url)}`);
    return response(init?.method === 'POST' ? personal : [company, personal, { ...personal, id: 'legacy', name: 'Legacy documents' }]);
  });
  render(<McpAccounts agentId="employee" serverId="company" />);
  await screen.findByText('Reconnect personal');
  expect(screen.getByText('Reconnect legacy')).toBeTruthy();
  expect(screen.getAllByRole('button', { name: 'Remove connection' })).toHaveLength(2);
  expect(screen.queryByRole('combobox')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Connect my account' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Remove use for my work' })).toBeNull();
  fireEvent.click(screen.getAllByRole('button', { name: 'Remove connection' })[1]);
  await waitFor(() => expect(calls).toContain('POST /api/mcp-accounts/legacy/revoke'));
  expect(calls.some((call) => call.includes('/grants/'))).toBe(false);
});

it('connects the missing account once without an add-account form', async () => {
  let connected = false;
  const bodies: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    if (init?.method === 'POST') { connected = true; bodies.push(JSON.parse(String(init.body))); return response(personal); }
    return response(connected ? [company, personal] : [company]);
  });
  render(<McpAccounts agentId="employee" serverId="company" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Connect my account' }));
  await screen.findByText('Reconnect personal');
  expect(bodies).toHaveLength(1);
  expect(screen.queryByLabelText('Account label')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Connect my account' })).toBeNull();
});
