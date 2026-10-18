/** The employee control consumes real HTTP state backed by PostgreSQL RLS. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { afterEach, expect, it } from 'vitest';
import CommandEnvironment from '../src/components/CommandEnvironment';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import en from '../src/i18n/en.json';

afterEach(cleanup);
it('resumes and stops the same employee resource through the mounted control', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const userId = process.env.HIVE_G90_USER_ID || '';
  const tenantId = process.env.HIVE_G90_TENANT_ID || '';
  const agentId = process.env.HIVE_G90_AGENT_ID || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(userId && tenantId && agentId).toBeTruthy();
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: userId, tenant_id: tenantId, role: 'org_admin', username: 'Resource owner' } as User, 'synthetic-resource-owner');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    const view = render(<I18nextProvider i18n={language}><QueryClientProvider client={client}><CommandEnvironment agentId={agentId} /></QueryClientProvider></I18nextProvider>);
    const details = view.container.querySelector('details')!;
    details.open = true; fireEvent(details, new Event('toggle'));
    await screen.findByText('Stopped');
    fireEvent.click(screen.getByRole('button', { name: 'Resume command environment' }));
    await screen.findByText('Ready for supervised commands');
    await waitFor(() => expect((screen.getByRole('button', { name: 'Stop supervised commands' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Stop supervised commands' }));
    await screen.findByText('Stopped');
    expect(screen.queryByRole('alert')).toBeNull();
  } finally {
    globalThis.fetch = original; client.clear(); useAuthStore.getState().logout();
  }
});
