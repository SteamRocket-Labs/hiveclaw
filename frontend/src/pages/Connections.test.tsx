// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';
import Connections from './Connections';
import { useAuthStore } from '../stores';
import type { User } from '../types';
import { act } from '@testing-library/react';
const translate = (key: string) => translateFromCatalog(en, key);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: translate }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'member-a', tenant_id: 'company' } as User });
});
it('removes the member connection from settings without returning its credential', async () => {
  let removed = false; const calls: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    calls.push(String(url));
    if (init?.method === 'POST') removed = true;
    return new Response(JSON.stringify(removed ? [] : [{ id: 'owned', name: 'Documents', auth_status: 'configured' }]), { status: 200 });
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Connections /></QueryClientProvider>);
  await screen.findByText('Documents');
  fireEvent.click(screen.getByRole('button', { name: 'Remove connection' }));
  await screen.findByText('No connected applications yet. I will ask here when a task needs one.');
  await waitFor(() => expect(calls).toEqual(['/api/mcp-accounts', '/api/mcp-accounts/owned/revoke', '/api/mcp-accounts']));
  expect(screen.queryByText('Documents')).toBeNull();
});
it('does not show a previous member connection while the next member list is loading', async () => {
  const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(new Response(JSON.stringify([
    { id: 'owned-a', name: 'Private A documents', auth_status: 'configured' },
  ]))).mockImplementation(() => new Promise(() => {}));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><Connections /></QueryClientProvider>);
  await screen.findByText('Private A documents');
  act(() => useAuthStore.setState({ user: { id: 'member-b', tenant_id: 'company' } as User }));
  expect(screen.queryByText('Private A documents')).toBeNull();
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
});
