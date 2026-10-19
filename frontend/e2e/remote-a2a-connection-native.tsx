/** The mounted management tab uses real owner HTTP and governed remote discovery. */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import AgentA2ASection from '../src/pages/agent-detail/AgentA2ASection';
import en from '../src/i18n/en.json';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('configures, discovers and disables a remote endpoint from the actual employee tab', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const remote = process.env.HIVE_M3N_REMOTE_ORIGIN || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(remote.startsWith('http://127.0.0.1:')).toBe(true);
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only owned loopback HTTP is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  const token = process.env.HIVE_M3N_TOKEN || '';
  const agentId = process.env.HIVE_G90_AGENT_ID || '';
  useAuthStore.getState().setAuth({ id: process.env.HIVE_G90_USER_ID, tenant_id: process.env.HIVE_G90_TENANT_ID,
    role: 'org_admin', username: 'Remote integration administrator' } as User, token);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <AgentA2ASection agentId={agentId} canManage />
    </QueryClientProvider></I18nextProvider>);
    const heading = await screen.findByRole('heading', { name: 'Remote A2A connection' });
    const controls = within(heading.closest('section')!);
    fireEvent.change(await controls.findByLabelText('Agent Card URL'), { target: { value: remote + '/card' } });
    fireEvent.change(controls.getByLabelText('Task interface URL'), { target: { value: remote + '/rpc' } });
    fireEvent.change(controls.getByLabelText('Machine credential (leave blank to keep)'), { target: { value: 'synthetic-remote-credential' } });
    fireEvent.change(controls.getByLabelText('Approved private network ranges (comma separated)'), { target: { value: '127.0.0.1/32' } });
    fireEvent.click(controls.getByRole('button', { name: 'Save' }));
    await controls.findByText('Configured');
    expect((controls.getByLabelText('Machine credential (leave blank to keep)') as HTMLInputElement).value).toBe('');
    fireEvent.click(controls.getByRole('button', { name: 'Read Agent Card' }));
    await controls.findByText('Agent Card read');
    await controls.findByText(/^Reference Agent/);
    expect(controls.getByText(/Automatic employee dispatch is not activated/)).toBeTruthy();
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain('synthetic-remote-credential');
    expect(localStorage.getItem('token')).toBe(token);
    fireEvent.click(controls.getByRole('button', { name: 'Disable connection' }));
    await controls.findByText('Disabled');
    await waitFor(() => expect(controls.queryByLabelText('Remove stored credential')).toBeNull());
  } finally {
    cleanup();client.clear();useAuthStore.getState().logout();globalThis.fetch = original;
  }
});
