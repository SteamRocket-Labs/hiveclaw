/** Mounted device controls consume actual JWT HTTP and native PostgreSQL state. */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import LocalAgentDeviceControls from '../src/components/LocalAgentDeviceControls';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('revokes the exact device while keeping host execution unknown', async () => {
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
    role: 'org_admin', username: 'Device owner' } as User, process.env.HIVE_M3C_TOKEN || '');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    const view = render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <LocalAgentDeviceControls agentId={process.env.HIVE_G90_AGENT_ID} />
    </QueryClientProvider></I18nextProvider>);
    const details = view.container.querySelector('details')!;
    details.open = true; fireEvent(details, new Event('toggle'));
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke this device access' }));
    await screen.findByText('Device access revoked. Host execution remains unknown; a device stop has not been confirmed.');
    expect(screen.getByText('Cancelled before dispatch: 1. Execution needs review: 0.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  } finally {
    cleanup(); client.clear(); useAuthStore.getState().logout(); globalThis.fetch = original;
  }
});
