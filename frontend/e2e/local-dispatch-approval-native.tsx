/** Mounted original-work and approval consumers against owned JWT HTTP/PostgreSQL. */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import LocalAgentRequestControls from '../src/components/LocalAgentRequestControls';
import AgentApprovalsSection from '../src/pages/agent-detail/AgentApprovalsSection';
import en from '../src/i18n/en.json';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';

it('uses the existing employee approval UI and refreshes the original request', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only owned loopback HTTP is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: process.env.HIVE_G90_USER_ID, tenant_id: process.env.HIVE_G90_TENANT_ID,
    role: 'member', username: 'Original owner' } as User, process.env.HIVE_M3F_TOKEN || '');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const agentId = process.env.HIVE_G90_AGENT_ID || '';
  const panels = (approval = false) => <I18nextProvider i18n={language}><QueryClientProvider client={client}>
    <LocalAgentRequestControls sessionId={process.env.HIVE_M3F_SESSION_ID || ''} />
    {approval && <AgentApprovalsSection agentId={agentId} />}
  </QueryClientProvider></I18nextProvider>;
  try {
    const view = render(panels());
    view.container.querySelector('details')!.open = true;
    const link = await screen.findByRole('link', { name: 'Review this employee’s approvals' });
    expect(link.getAttribute('href')).toBe(`/agents/${agentId}#approvals`);
    view.rerender(panels(true));
    fireEvent.click(await screen.findByRole('button', { name: en.agent.approvals.approve }));
    await waitFor(() => expect(screen.queryByRole('button', { name: en.agent.approvals.approve })).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: en.common.refresh }));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'Review this employee’s approvals' })).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
  } finally {
    cleanup(); client.clear(); useAuthStore.getState().logout(); globalThis.fetch = original;
  }
});
