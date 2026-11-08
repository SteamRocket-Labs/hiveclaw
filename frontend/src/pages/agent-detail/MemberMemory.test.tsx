// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import MemberMemory from './MemberMemory';
import { useAuthStore } from '../../stores';
import type { User } from '../../types';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => translateFromCatalog(en, key) }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('shows a read-only own memory list, deletes an entry, and clears it on member switch', async () => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'me', tenant_id: 'company' } as User });
  let deleted = false;
  const methods: string[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
    methods.push(init?.method || 'GET');
    if (init?.method === 'DELETE') deleted = true;
    return new Response(JSON.stringify(deleted ? [] : [{ id: 'mine', content: 'Use Chinese for summaries.' }]), { status: 200 });
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemberMemory agentId="bot" /></QueryClientProvider>);
  await screen.findByText('Use Chinese for summaries.');
  expect(screen.queryByRole('heading', { name: 'What it remembers' })).toBeNull();
  expect(screen.queryByRole('textbox')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  await screen.findByText('Nothing you asked it to remember yet.');
  expect(methods).toEqual(['GET', 'DELETE', 'GET']);
  act(() => useAuthStore.setState({ user: { id: 'other', tenant_id: 'company' } as User }));
  expect(screen.queryByText('Use Chinese for summaries.')).toBeNull();
});
