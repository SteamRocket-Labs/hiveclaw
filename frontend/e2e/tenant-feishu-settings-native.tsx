/** DOM integration with the actual HTTP/RLS service; no browser or provider transport. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { afterEach, expect, it } from 'vitest';
import TenantFeishuSettings, { TenantFeishuIdentity } from '../src/components/TenantFeishuSettings';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import en from '../src/i18n/en.json';

afterEach(cleanup);

it('saves company credentials and restores the same member through real HTTP and PostgreSQL', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const userId = process.env.HIVE_G90_USER_ID || '';
  const tenantId = process.env.HIVE_G90_TENANT_ID || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(userId && tenantId).toBeTruthy();
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: userId, tenant_id: tenantId, role: 'org_admin', username: 'Native member' } as User, 'synthetic-dom-member');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (child: React.ReactNode) => <I18nextProvider i18n={language}><QueryClientProvider client={client}><MemoryRouter>{child}</MemoryRouter></QueryClientProvider></I18nextProvider>;
  try {
    const admin = render(wrap(<TenantFeishuSettings selectedTenantId={tenantId} />));
    await waitFor(() => expect((screen.getByLabelText('App ID') as HTMLInputElement).value).toBe('cli_settings'));
    fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'synthetic-dom-company-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Company bot settings saved.');
    expect((screen.getByLabelText('App secret') as HTMLInputElement).value).toBe('');
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain('synthetic-dom-company-secret');
    expect((screen.getByLabelText('Event callback URL') as HTMLInputElement).value).toContain(`/api/channel/feishu/tenant/${tenantId}/webhook`);
    admin.unmount();
    render(wrap(<TenantFeishuIdentity />));
    await screen.findByText('Your identity is verified for this company bot.');
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect this identity' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Reconnect this identity' }));
    await screen.findByRole('button', { name: 'Disconnect this identity' });
    expect(client.getMutationCache().getAll()).toHaveLength(0);
  } finally {
    globalThis.fetch = original;
    client.clear();
    useAuthStore.getState().logout();
  }
});
