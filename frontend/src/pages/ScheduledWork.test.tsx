// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import ScheduledWork from './ScheduledWork';
import { useAuthStore } from '../stores';
import type { User } from '../types';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, fallback?: string | Record<string, unknown>, options?: Record<string, unknown>) => translateFromCatalog(en, key, fallback, options) }) }));
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
      { id: 'event', name: 'Webhook', type: 'webhook', config: { created_by: 'me' }, is_enabled: false },
    ] : [{ id: 'bot', name: 'Reports' }];
    return new Response(JSON.stringify(data), { status: 200 });
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ScheduledWork /></QueryClientProvider>);
  await screen.findByText('Morning report');
  expect(screen.queryByText('Private colleague task')).toBeNull();
  expect(screen.getByText('Webhook')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
  await waitFor(() => expect(screen.getAllByText('Stopped')).toHaveLength(2));
  expect(writes).toEqual([{ is_enabled: false }]);
  expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
});

it('keeps a stop control for every trigger type owned by the member', async () => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'me', tenant_id: 'company' } as User });
  const types = ['cron', 'once', 'interval', 'poll', 'webhook', 'on_message'];
  const stopped = new Set<string>();
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, init) => {
    const path = String(url);
    if (init?.method === 'PATCH') stopped.add(path.split('/').at(-1)!);
    return new Response(JSON.stringify(path.includes('/triggers') ? types.map(type => ({
      id: type, name: `Owned ${type}`, type, is_enabled: !stopped.has(type),
      config: { created_by: 'me', expr: '0 9 * * *', minutes: 5, url: 'https://local.test/feed', at: '2026-11-10T09:00:00Z' },
    })) : [{ id: 'bot', name: 'Reports' }]), { status: 200 });
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ScheduledWork /></QueryClientProvider>);
  await screen.findByText('Owned poll');
  expect(screen.getByText('Reports · When a request arrives')).toBeTruthy();
  expect(screen.getAllByRole('button', { name: 'Stop' })).toHaveLength(6);
  for (const type of types) {
    const row = screen.getByText(`Owned ${type}`).closest('section')!;
    fireEvent.click(row.querySelector('button')!);
    await waitFor(() => expect(row.textContent).toContain('Stopped'));
  }
  expect([...stopped]).toEqual(types);
});

it('shows loaded work and the failed bot name when another bot request fails', async () => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'me', tenant_id: 'company' } as User });
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => {
    const path = String(url);
    if (path.includes('/broken/')) return new Response(JSON.stringify({ detail: 'Unavailable' }), { status: 503 });
    return new Response(JSON.stringify(path.includes('/triggers') ? [
      { id: 'mine', name: 'Available work', type: 'poll', config: { created_by: 'me', url: 'https://local.test' }, is_enabled: true },
      { id: 'foreign', name: 'Not mine', type: 'poll', config: { created_by: 'other' }, is_enabled: true },
    ] : [{ id: 'working', name: 'Available bot' }, { id: 'broken', name: 'Unavailable bot' }]), { status: 200 });
  });
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ScheduledWork /></QueryClientProvider>);
  await screen.findByText('Available work');
  expect(await screen.findByText('Could not read work for Unavailable bot. Try again later.')).toBeTruthy();
  expect(screen.queryByText('Not mine')).toBeNull();
});

it('does not duplicate the embedded bot section heading', async () => {
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: { id: 'me', tenant_id: 'company' } as User });
  vi.spyOn(globalThis, 'fetch').mockImplementation(async url => new Response(JSON.stringify(
    String(url).includes('/triggers') ? [] : { id: 'bot', name: 'Reports' }), { status: 200 }));
  render(<QueryClientProvider client={new QueryClient()}><ScheduledWork agentId="bot" /></QueryClientProvider>);
  await screen.findByText('No scheduled work yet. Ask in a conversation when you need it.');
  expect(screen.queryByRole('heading', { name: 'Scheduled work' })).toBeNull();
});
