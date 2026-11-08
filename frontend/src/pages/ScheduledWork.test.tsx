// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import ScheduledWork from './ScheduledWork';
import { useAuthStore } from '../stores';
import type { User } from '../types';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => translateFromCatalog(en, key) }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('lists only the member scheduled work per bot and stops it through the existing pause path', async () => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'me', tenant_id: 'company' } as User });
  let enabled = true;
  const writes: unknown[] = [];
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    const path = String(url);
    if (init?.method === 'PATCH') { writes.push(JSON.parse(String(init.body))); enabled = false; }
    const data = path.includes('/triggers') ? [
      { id: 'mine', name: 'Morning report', type: 'cron', config: { created_by: 'me' }, is_enabled: enabled },
      { id: 'foreign', name: 'Private colleague task', type: 'cron', config: { created_by: 'other' }, is_enabled: true },
      { id: 'event', name: 'Webhook', type: 'webhook', config: { created_by: 'me' } },
    ] : [{ id: 'bot', name: 'Reports' }];
    return new Response(JSON.stringify(data), { status: 200 });
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ScheduledWork /></QueryClientProvider>);
  await screen.findByText('Morning report');
  expect(screen.queryByText('Private colleague task')).toBeNull();
  expect(screen.queryByText('Webhook')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
  await screen.findByText('Stopped');
  expect(writes).toEqual([{ is_enabled: false }]);
  expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
});
