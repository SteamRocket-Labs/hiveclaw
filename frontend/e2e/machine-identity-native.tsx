/** Actual employee A2A management consumes owned JWT HTTP and scoped credentials. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import AgentA2ASection from '../src/pages/agent-detail/AgentA2ASection';
import en from '../src/i18n/en.json';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('issues from the real A2A tab, uses a distinct machine credential, and revokes it', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only owned loopback HTTP is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  const userToken = process.env.HIVE_M3H_TOKEN || '';
  const agentId = process.env.HIVE_G90_AGENT_ID || '';
  useAuthStore.getState().setAuth({ id: process.env.HIVE_G90_USER_ID, tenant_id: process.env.HIVE_G90_TENANT_ID,
    role: 'org_admin', username: 'Credential issuer' } as User, userToken);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
      <AgentA2ASection agentId={agentId} canManage />
    </QueryClientProvider></I18nextProvider>);
    fireEvent.change(await screen.findByLabelText('Integration name'), { target: { value: 'Native scoped partner' } });
    fireEvent.click(screen.getByRole('button', { name: 'Issue credential' }));
    const input = await screen.findByLabelText('New machine secret') as HTMLInputElement;
    const machineToken = input.value;
    expect(machineToken.startsWith('hmi_')).toBe(true);
    expect(machineToken).not.toBe(userToken);
    expect(localStorage.getItem('token')).toBe(userToken);
    expect(JSON.stringify(client.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(machineToken);
    const description = await fetch(`/api/agents/${agentId}/machine-description`, { headers: { Authorization: `Bearer ${machineToken}` } });
    expect(description.status).toBe(200);
    expect((await description.json()).agent.id).toBe(agentId);
    fireEvent.click(screen.getByRole('button', { name: 'Revoke access' }));
    await screen.findByText('Native scoped partner · Revoked');
    await waitFor(() => expect(screen.queryByLabelText('New machine secret')).toBeNull());
    const refused = await fetch(`/api/agents/${agentId}/machine-description`, { headers: { Authorization: `Bearer ${machineToken}` } });
    expect(refused.status).toBe(401);
    expect(localStorage.getItem('token')).toBe(userToken);
  } finally {
    cleanup();client.clear();useAuthStore.getState().logout();globalThis.fetch = original;
  }
});
