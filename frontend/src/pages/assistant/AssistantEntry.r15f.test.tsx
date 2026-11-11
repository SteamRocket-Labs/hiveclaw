// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import AssistantEntry from './AssistantEntry';
import { useAssistantAgentId } from './assistantScope';
import { useAuthStore } from '../../stores';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
const user = { id: 'member', role: 'member', tenant_id: 'company' };
const unbound = { state: 'unbound', scope: { user_id: 'member', tenant_id: 'company' }, agent: null,
  entry: { kind: 'setup', path: '/assistant' }, candidates: [] };
const active = { ...unbound, state: 'active', agent: { id: 'assistant', name: 'My Assistant' },
  entry: { kind: 'employee', path: '/agents/assistant', agent_id: 'assistant' } };
beforeEach(() => {
  localStorage.setItem('current_tenant_id', 'company');
  localStorage.setItem('token', 'local-token');
  useAuthStore.setState({ user: user as never, token: 'local-token' });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
function Sidebar() {
  return <output data-testid="sidebar-assistant">{useAssistantAgentId(user)}</output>;
}

it.each(['sidebar-first', 'entry-first'])('R15f.3 shares one scoped assistant GET (%s)', async order => {
  let release!: (response: Response) => void;
  const read = new Promise<Response>(resolve => { release = resolve; });
  const fetcher = vi.fn((url: string) => url === '/api/assistant/open'
    ? Promise.resolve(new Response(JSON.stringify(active), { status: 200 })) : read);
  vi.stubGlobal('fetch', fetcher);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const entry = <Routes><Route path="/" element={<AssistantEntry mode="entry" />} />
    <Route path="/agents/:id" element={<output data-testid="destination">assistant</output>} /></Routes>;
  render(<QueryClientProvider client={client}><MemoryRouter>
    {order === 'sidebar-first' ? <><Sidebar />{entry}</> : <>{entry}<Sidebar /></>}
  </MemoryRouter></QueryClientProvider>);
  await waitFor(() => expect(fetcher).toHaveBeenCalled());
  expect(fetcher.mock.calls.filter(([url]) => url !== '/api/assistant/open')).toHaveLength(1);
  const readUrl = fetcher.mock.calls[0][0];
  expect(readUrl).toBe('/api/assistant?expected_user_id=member&expected_tenant_id=company');
  await act(async () => { release(new Response(JSON.stringify(unbound), { status: 200 })); await read; });
  await screen.findByTestId('destination');
  await waitFor(() => expect(screen.getByTestId('sidebar-assistant').textContent).toBe('assistant'));
  expect(fetcher.mock.calls.filter(([url]) => url === '/api/assistant/open')).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([url]) => url !== '/api/assistant/open')).toHaveLength(1);
});
