/** Original-work source recovery uses actual JWT HTTP and PostgreSQL state. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import LocalAgentRequestControls from '../src/components/LocalAgentRequestControls';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('repairs the return of a retained result without executing the request again', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only owned loopback HTTP is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: {}, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: process.env.HIVE_G90_USER_ID, tenant_id: process.env.HIVE_G90_TENANT_ID,
    role: 'member', username: 'Original work owner' } as User, process.env.HIVE_M3E_TOKEN || '');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    const view = render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <LocalAgentRequestControls sessionId={process.env.HIVE_M3E_SESSION_ID || ''} />
    </QueryClientProvider></I18nextProvider>);
    view.container.querySelector('details')!.open = true;
    fireEvent.click(await screen.findByRole('button', { name: 'Retry original-work return' }));
    await screen.findByText('The original-work return is recorded. Execution is not repeated.');
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry original-work return' })).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
  } finally {
    cleanup(); client.clear(); useAuthStore.getState().logout(); globalThis.fetch = original;
  }
});
