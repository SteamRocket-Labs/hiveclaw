// @vitest-environment jsdom
import { createRef } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import AssistantEntry from './AssistantEntry';
import AppSidebar from '../layout/AppSidebar';
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

it('R15f.3 reuses a resolved sidebar GET and still opens automatically once', async () => {
  const fetcher = vi.fn(async (url: string) => new Response(JSON.stringify(
    url === '/api/assistant/open' ? active : unbound,
  ), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = (entry: boolean) => <QueryClientProvider client={client}><MemoryRouter>
    <Sidebar />{entry && <Routes><Route path="/" element={<AssistantEntry mode="entry" />} />
      <Route path="/agents/:id" element={<output data-testid="destination">assistant</output>} /></Routes>}
  </MemoryRouter></QueryClientProvider>;
  const view = render(tree(false));
  await waitFor(() => expect(client.getQueryState(['assistant', 'member', 'company', 'member'])?.status).toBe('success'));
  expect(fetcher).toHaveBeenCalledTimes(1);
  view.rerender(tree(true));
  await screen.findByTestId('destination');
  await waitFor(() => expect(screen.getByTestId('sidebar-assistant').textContent).toBe('assistant'));
  expect(fetcher.mock.calls.filter(([url]) => url === '/api/assistant/open')).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([url]) => url !== '/api/assistant/open')).toHaveLength(1);
});

it('R15f.3 keeps sidebar links usable through a foreign stored tenant 409 and recovery', async () => {
  localStorage.setItem('current_tenant_id', 'foreign');
  const fetcher = vi.fn(async (url: string) => url.includes('expected_tenant_id=foreign')
    ? new Response(JSON.stringify({ detail: { state: 'scope_changed', message: 'scope changed' } }), { status: 409 })
    : new Response(JSON.stringify(active), { status: 200 }));
  vi.stubGlobal('fetch', fetcher);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props = {
    user, theme: 'light' as const, isSidebarCollapsed: false, onToggleSidebar: vi.fn(),
    agents: [{ id: 'recent', name: 'Recent', last_active_at: '2026-11-05' },
      { id: 'assistant', name: 'My Assistant', last_active_at: '2026-11-01' }],
    tenants: [], onSwitchTenant: vi.fn(), isChinese: true, onToggleTheme: vi.fn(),
    onOpenNotifications: vi.fn(), unreadCount: 0, accountMenuRef: createRef<HTMLDivElement>(),
    showAccountMenu: false, onToggleAccountMenu: vi.fn(), onToggleLang: vi.fn(),
    onOpenAccountSettings: vi.fn(), onLogout: vi.fn(), versionDisplay: null,
  };
  const tree = (tenant: string) => <QueryClientProvider client={client}><MemoryRouter>
    <AppSidebar {...props} currentTenant={tenant} />
  </MemoryRouter></QueryClientProvider>;
  const view = render(tree('foreign'));
  await waitFor(() => expect(client.getQueryState(['assistant', 'member', 'foreign', 'member'])?.status).toBe('error'));
  expect(client.getQueryState(['assistant', 'member', 'foreign', 'member'])?.error)
    .toMatchObject({ status: 409, data: { state: 'scope_changed' } });
  const links = () => [...view.container.querySelectorAll('.sidebar-agent-link')].map(link => link.getAttribute('href'));
  expect(links()).toEqual(['/agents/recent#chat', '/agents/assistant#chat']);
  expect(screen.queryByRole('alert')).toBeNull();
  expect(view.container.textContent).not.toMatch(/scope_changed|scope changed|connectionFailed|409/);
  // Layout corrects the member's stored tenant and currentTenant together.
  localStorage.setItem('current_tenant_id', 'company');
  view.rerender(tree('company'));
  await waitFor(() => expect(links()).toEqual(['/agents/assistant#chat', '/agents/recent#chat']));
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(client.getQueryState(['assistant', 'member', 'company', 'member'])?.status).toBe('success');
  expect(screen.queryByRole('alert')).toBeNull();
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
