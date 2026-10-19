/** Mounted local request controls use actual JWT HTTP and PostgreSQL state. */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import LocalAgentRequestControls from '../src/components/LocalAgentRequestControls';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('withdraws one request from its original work through the mounted control', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const sessionId = process.env.HIVE_M3B_SESSION_ID || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(sessionId).toBeTruthy();
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: {}, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: process.env.HIVE_G90_USER_ID, tenant_id: process.env.HIVE_G90_TENANT_ID,
    role: 'org_admin', username: 'Local work owner' } as User, process.env.HIVE_M3B_TOKEN || '');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    const view = render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <LocalAgentRequestControls sessionId={sessionId} />
    </QueryClientProvider></I18nextProvider>);
    view.container.querySelector('details')!.open = true;
    await screen.findByText('Prepare the selected employee report.');
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel this request' }));
    await screen.findByText('Cancelled before dispatch');
    expect(screen.queryByRole('button', { name: 'Cancel this request' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  } finally {
    cleanup(); client.clear(); useAuthStore.getState().logout(); globalThis.fetch = original;
  }
});
