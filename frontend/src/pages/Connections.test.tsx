// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import Connections from './Connections';
import { useAuthStore } from '../stores';
import type { User } from '../types';

const translate = (key: string, options?: Record<string, unknown>) => translateFromCatalog(en, key, options);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate, i18n: { language: 'en' } }) }));
const row = { kind: 'mcp', id: 'owned', name: 'Documents', scope: 'personal', configured: true,
  fields: [{ key: 'access_token', label: '' }], bots: [{ id: 'bot', name: 'Finance assistant' }],
  removal: { fallback: 'company' }, version: null };
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Connections /></QueryClientProvider>);
  return client;
}
beforeEach(() => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'member-a', tenant_id: 'company', display_name: 'Member A' } as User });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('lists existing credentials and asks before removal, including the real company fallback', async () => {
  const calls: Array<[string, string]> = [];
  let removed = false;
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    calls.push([String(url), init?.method || 'GET']);
    if (init?.method === 'DELETE') { removed = true; return new Response(JSON.stringify({ removed: true, removal: row.removal })); }
    return new Response(JSON.stringify(removed ? [] : [row]));
  });
  mount();
  await screen.findByText('Documents');
  expect(screen.getByRole('heading', { name: 'Vault' })).toBeTruthy();
  expect(screen.getByText('Finance assistant')).toBeTruthy();
  expect(screen.getByText('My account')).toBeTruthy();
  expect(screen.queryByText(/unknown|last used|access_token/i)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  const dialog = screen.getByRole('dialog');
  expect(within(dialog).getByText('After removal, this digital employee will use the company settings.')).toBeTruthy();
  expect(calls).toEqual([['/api/vault', 'GET']]);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Remove' }));
  await screen.findByText('After removal, this digital employee will use the company settings.');
  await waitFor(() => expect(screen.queryByText('Documents')).toBeNull());
  expect(calls).toEqual([['/api/vault', 'GET'], ['/api/vault/mcp/owned', 'DELETE'], ['/api/vault', 'GET']]);
});

it('sends replacements directly, clears the password after save and cancel, and keeps secrets out of query data', async () => {
  const requests: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    if (init?.method === 'PUT') {
      requests.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify({ removed: false, removal: row.removal }));
    }
    return new Response(JSON.stringify([row]));
  });
  const client = mount();
  await screen.findByText('Documents');
  fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
  let input = screen.getByLabelText('Credential') as HTMLInputElement;
  expect(input.type).toBe('password');
  expect(input.value).toBe('');
  fireEvent.change(input, { target: { value: 'private-new-token' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(requests).toEqual([{ values: { access_token: 'private-new-token' }, version: null }]);
  expect(JSON.stringify(client.getQueryData(['vault', 'member-a', 'company']))).not.toContain('private-new-token');
  fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
  input = screen.getByLabelText('Credential') as HTMLInputElement;
  expect(input.value).toBe('');
  fireEvent.change(input, { target: { value: 'cancelled-token' } });
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
  expect((screen.getByLabelText('Credential') as HTMLInputElement).value).toBe('');
  expect(requests).toHaveLength(1);
});

it('retains a failed edit for retry without rendering raw server errors', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ detail: 'RAW_PRIVATE_TOKEN' }), { status: 503 })
    : new Response(JSON.stringify([row])));
  mount(); await screen.findByText('Documents');
  fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
  fireEvent.change(screen.getByLabelText('Credential'), { target: { value: 'retry-token' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByRole('alert');
  expect((screen.getByLabelText('Credential') as HTMLInputElement).value).toBe('retry-token');
  expect(screen.queryByText('RAW_PRIVATE_TOKEN')).toBeNull();
});

it('hides the prior member list and open credential dialog while the next member loads', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify([row])))
    .mockImplementation(() => new Promise(() => {}));
  mount(); await screen.findByText('Documents');
  fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
  fireEvent.change(screen.getByLabelText('Credential'), { target: { value: 'previous-member-token' } });
  act(() => useAuthStore.setState({ user: { id: 'member-b', tenant_id: 'company' } as User }));
  expect(screen.queryByText('Documents')).toBeNull();
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
});

it('shows personal, managed-bot and company scopes without exposing field names or missing-fact placeholders', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify([row,
    { ...row, kind: 'bot', id: 'assignment', name: 'Billing', scope: 'bot', field: 'bearer_token', fields: [{ key: 'bearer_token', label: 'Access token' }] },
    { ...row, kind: 'company', id: 'tool', name: 'Search', scope: 'company', configured: false, field: 'api_key', fields: [{ key: 'api_key', label: 'API Key' }] },
  ])));
  mount(); await screen.findByText('Billing');
  expect(screen.getByText('Only this digital employee')).toBeTruthy();
  expect(screen.getByText('Company shared')).toBeTruthy();
  expect(screen.getByText('Not configured')).toBeTruthy();
  expect(screen.queryByText(/bearer_token|api_key|unknown|last used/i)).toBeNull();
});

it('renders copied company and OAuth rows without replacement or removal actions', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify([
    { ...row, kind: 'company', id: 'search', name: 'Company search', scope: 'company', read_only: true },
    { ...row, kind: 'company_mcp', id: 'oauth', name: 'Company documents', scope: 'company', read_only: true },
    { ...row, kind: 'company', id: 'billing', name: 'Company billing', scope: 'company', read_only: false },
  ])));
  mount(); await screen.findByText('Company search');
  for (const name of ['Company search', 'Company documents']) {
    const section = screen.getByRole('heading', { name }).closest('article')!;
    expect(within(section).queryByRole('button')).toBeNull();
    expect(within(section).getByText('Company shared')).toBeTruthy();
  }
  const editable = screen.getByRole('heading', { name: 'Company billing' }).closest('article')!;
  expect(within(editable).getByRole('button', { name: 'Replace' })).toBeTruthy();
  expect(within(editable).getByRole('button', { name: 'Remove' })).toBeTruthy();
});

it('uses the English list separator and plural consequence for several digital employees', async () => {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify([{ ...row,
    bots: [row.bots[0], { id: 'research', name: 'Research assistant' }],
  }])));
  mount(); await screen.findByText('Finance assistant, Research assistant');
  fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
  expect(within(screen.getByRole('dialog')).getByText('After removal, these digital employees will use the company settings.')).toBeTruthy();
});

it('explains the existing account review after a custom API secret changes', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => init?.method === 'PUT'
    ? new Response(JSON.stringify({ removed: false, removal: { fallback: 'company', review_required: true } }))
    : new Response(JSON.stringify([row])));
  mount(); await screen.findByText('Documents');
  fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
  fireEvent.change(screen.getByLabelText('Credential'), { target: { value: 'replacement-token' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('Credential replaced. Work that selected this account needs its account choice confirmed again.');
});
