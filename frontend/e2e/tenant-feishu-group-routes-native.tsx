/** The real enterprise settings form saves routes through HTTP and PostgreSQL RLS. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it } from 'vitest';
import TenantFeishuSettings from '../src/components/TenantFeishuSettings';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import en from '../src/i18n/en.json';

afterEach(cleanup);
it('preserves and reselects the explicit employee through the company form', async () => {
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
  useAuthStore.getState().setAuth({ id: userId, tenant_id: tenantId, role: 'org_admin', username: 'Native group administrator' } as User, 'synthetic-company-group-admin');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    const view = render(<I18nextProvider i18n={language}><QueryClientProvider client={client}><MemoryRouter><TenantFeishuSettings selectedTenantId={tenantId} /></MemoryRouter></QueryClientProvider></I18nextProvider>);
    await screen.findByDisplayValue('cli_company_group');
    const details = view.container.querySelector('details')!;
    details.open = true; fireEvent(details, new Event('toggle'));
    await screen.findByDisplayValue('oc_company_shared');
    const employee = (screen.getByLabelText('Employee 1') as HTMLSelectElement).value;
    expect(employee).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add a group route' }));
    fireEvent.change(screen.getByLabelText('Group ID 1'), { target: { value: 'oc_company_shared' } });
    fireEvent.change(screen.getByLabelText('Employee 1'), { target: { value: employee } });
    fireEvent.click(screen.getByRole('button', { name: 'Save group routes' }));
    await screen.findByText('Company group routes saved.');
    await waitFor(() => expect(screen.queryByText(/Could not confirm these routes/)).toBeNull());
    expect((screen.getByLabelText('Employee 1') as HTMLSelectElement).value).toBe(employee);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain('synthetic-company-group-secret');
  } finally {
    globalThis.fetch = original; client.clear(); useAuthStore.getState().logout();
  }
});
