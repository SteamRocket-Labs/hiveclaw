// @vitest-environment jsdom
import React from 'react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AssistantEntry from './AssistantEntry';
import AppSidebar from '../layout/AppSidebar';
import AgentSettingsSection from '../agent-detail/AgentSettingsSection';
import AgentDetailHeader from '../agent-detail/AgentDetailHeader';
import AgentChatSection from '../agent-detail/AgentChatSection';
import { useAuthStore } from '../../stores';
import { agentApi } from '../../api/domains/agents';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';

const locale = vi.hoisted(() => ({ language: 'zh' }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string | Record<string, unknown>, values?: Record<string, unknown>) => {
    const dictionary = locale.language === 'zh' ? zh : en;
    const translated = key.split('.').reduce((value: any, part) => value?.[part], dictionary);
    const text = typeof translated === 'string' ? translated : typeof fallback === 'string' ? fallback : key;
    const options = typeof fallback === 'object' ? fallback : values;
    return text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(options?.[name] ?? ''));
  }, i18n: { language: locale.language },
}) }));
vi.mock('../../components/CommandEnvironment', () => ({ default: () => null }));
vi.mock('../../components/ChannelConfig', () => ({ default: () => null }));
vi.mock('../agent-detail/AgentActionPolicyCard', () => ({ default: () => null }));

const user = { id: 'r2-member', tenant_id: 'company', role: 'member' };
const unbound = {
  state: 'unbound', scope: { user_id: user.id, tenant_id: 'company' }, agent: null,
  candidates: [], entry: { kind: 'setup', path: '/assistant' },
};
const own = { ...unbound, state: 'active', created: true,
  agent: { id: 'own', name: 'My Assistant' }, entry: { kind: 'employee', path: '/agents/own', agent_id: 'own' } };
const bots = [{ id: 'recent', name: 'Recent', last_active_at: '2026-11-05' }];
const bindingKey = ['assistant', user.id, 'company', 'member'];
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status, headers: { 'content-type': 'application/json' },
});
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function Location() {
  const location = useLocation();
  return <output data-testid="destination">{location.pathname}{location.search}{location.hash}</output>;
}
const sidebarProps = {
  user, theme: 'light' as const, isSidebarCollapsed: false, onToggleSidebar: vi.fn(),
  tenants: [{ id: 'company', name: 'Company' }], currentTenant: 'company', onSwitchTenant: vi.fn(),
  isChinese: true, onToggleTheme: vi.fn(), onOpenNotifications: vi.fn(), unreadCount: 0,
  accountMenuRef: React.createRef<HTMLDivElement>(), showAccountMenu: false, onToggleAccountMenu: vi.fn(),
  onToggleLang: vi.fn(), onOpenAccountSettings: vi.fn(), onLogout: vi.fn(), versionDisplay: null, hrAgent: null,
};
function mount(children = <AssistantEntry mode="entry" />, client = new QueryClient({ defaultOptions: {
  queries: { retry: false, refetchOnWindowFocus: false },
} })) {
  const view = render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/']}>
    <Routes><Route path="/" element={children} /><Route path="/agents/:id" element={<Location />} />
      <Route path="/assistant" element={<Location />} /></Routes>
  </MemoryRouter></QueryClientProvider>);
  return { ...view, client };
}
function transport(read: unknown = unbound, open: unknown = own, available: unknown[] = bots) {
  return vi.stubGlobal('fetch', vi.fn(async (url: string) => response(
    url === '/api/assistant/open' ? open : url.startsWith('/api/assistant') ? read
      : url.startsWith('/api/agents/') ? available : [],
  )));
}
const writes = () => vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/assistant/open');
beforeEach(() => {
  locale.language = 'zh';
  localStorage.setItem('token', 'local-token');
  localStorage.setItem('current_tenant_id', 'company');
  useAuthStore.setState({ user: user as never, token: 'local-token' });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('NM round 2 landing regressions', () => {
  it.each(['503', 'network'])('keeps a failed assistant read on the page with retry: %s', async (failure) => {
    transport(own);
    const fetcher = vi.mocked(fetch);
    const successful = fetcher.getMockImplementation()!;
    fetcher.mockImplementation(async (...args) => {
      if (String(args[0]).startsWith('/api/assistant')) {
        if (failure === 'network') throw new TypeError('Failed to fetch');
        return response({}, 503);
      }
      return successful(...args);
    });
    mount();
    expect(await screen.findByText('暂时无法连接助理，请重试。')).toBeTruthy();
    expect(screen.queryByTestId('destination')).toBeNull();
    expect(writes()).toHaveLength(0);
    fetcher.mockImplementation(successful);
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
  });
  it('does not navigate with a cached binding after its read fails', async () => {
    transport();
    const original = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((...args) => String(args[0]).startsWith('/api/assistant')
      ? Promise.resolve(response({}, 503)) : original(...args));
    const client = new QueryClient();
    client.setQueryData(['assistant-entry', user.id, 'company', 'member', false], own);
    mount(undefined, client);
    expect(await screen.findByText('暂时无法连接助理，请重试。')).toBeTruthy();
    expect(screen.queryByTestId('destination')).toBeNull();
  });

  it.each(['503', 'network'])('opens again when retrying a failed assistant open: %s', async (failure) => {
    transport();
    const fetcher = vi.mocked(fetch);
    const successful = fetcher.getMockImplementation()!;
    let failOpening = true;
    fetcher.mockImplementation(async (...args) => {
      if (args[0] === '/api/assistant/open' && failOpening) {
        if (failure === 'network') throw new TypeError('Failed to fetch');
        return response({}, 503);
      }
      return successful(...args);
    });
    mount();
    expect(await screen.findByText('暂时无法连接助理，请重试。')).toBeTruthy();
    expect(screen.queryByTestId('destination')).toBeNull();
    expect(writes()).toHaveLength(1);
    failOpening = false;
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
    expect(writes()).toHaveLength(2);
  });

  it('opens the incomplete assistant when its agent is in the sidebar list', async () => {
    transport(unbound, { ...own, state: 'provision_incomplete', entry: unbound.entry }, [...bots, own.agent]);
    mount();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
  });
  it('excludes an incomplete assistant only when it is absent from the sidebar list', async () => {
    transport(unbound, { ...own, state: 'provision_incomplete', entry: unbound.entry });
    mount();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/recent#chat'));
  });
  it('waits for the refreshed sidebar list before opening a newly created incomplete assistant', async () => {
    const refreshed = deferred<Response>();
    const client = new QueryClient();
    client.setQueryData(['agents', 'company'], bots);
    transport(unbound, { ...own, state: 'provision_incomplete', entry: unbound.entry });
    const original = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((...args) => String(args[0]).startsWith('/api/agents/')
      ? refreshed.promise : original(...args));
    function Sidebar() {
      const { data: agents = [] } = useQuery({
        queryKey: ['agents', 'company'], queryFn: () => agentApi.list('company'), staleTime: 30_000,
      });
      return <AppSidebar {...sidebarProps} agents={agents} />;
    }
    render(<QueryClientProvider client={client}><MemoryRouter>
      <Sidebar />
      <Routes><Route path="/" element={<AssistantEntry mode="entry" />} />
        <Route path="/agents/:id" element={<Location />} /></Routes>
    </MemoryRouter></QueryClientProvider>);
    await waitFor(() => expect(client.isFetching({ queryKey: ['agents', 'company'] })).toBe(1));
    expect(screen.queryByTestId('destination')).toBeNull();
    await act(async () => { refreshed.resolve(response([...bots, own.agent])); await refreshed.promise; });
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
    expect(document.querySelector('.sidebar-agent-link')?.getAttribute('href')).toBe('/agents/own#chat');
  });
  it('opens once per page load and scope across landing remounts', async () => {
    transport(unbound, { ...unbound, state: 'model_unconfigured' });
    const first = mount();
    await screen.findByTestId('destination');
    first.unmount();
    const second = mount(undefined, first.client);
    await screen.findByTestId('destination');
    expect(writes()).toHaveLength(1);
    second.unmount();
    localStorage.setItem('current_tenant_id', 'company-2');
    const original = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (...args) => {
      if (String(args[0]).startsWith('/api/assistant')) return response({ ...unbound,
        state: args[0] === '/api/assistant/open' ? 'model_unconfigured' : 'unbound',
        scope: { ...unbound.scope, tenant_id: 'company-2' },
      });
      return original(...args);
    });
    mount(undefined, first.client);
    await screen.findByTestId('destination');
    expect(writes()).toHaveLength(2);
  });
  it('aborts an opening when leaving and never caches its late result or opens it again', async () => {
    const pending = deferred<Response>();
    transport();
    const original = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation((...args) => args[0] === '/api/assistant/open' ? pending.promise : original(...args));
    const view = mount();
    await waitFor(() => expect(writes()).toHaveLength(1));
    const signal = writes()[0][1]?.signal;
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => { pending.resolve(response(own)); await pending.promise; });
    expect(view.client.getQueryData(bindingKey)).toBeUndefined();
    mount(undefined, view.client);
    await screen.findByTestId('destination');
    expect(writes()).toHaveLength(1);
  });
  it('refreshes the sidebar binding after open even if an older GET arrives later', async () => {
    const older = deferred<Response>();
    transport();
    const original = vi.mocked(fetch).getMockImplementation()!;
    let reads = 0;
    vi.mocked(fetch).mockImplementation((...args) => {
      if (args[0] === '/api/assistant') return ++reads === 1 ? older.promise : Promise.resolve(response(own));
      return original(...args);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // The sidebar survives the landing redirect, as in Layout.
    render(<QueryClientProvider client={client}><MemoryRouter>
      <AppSidebar {...sidebarProps} agents={[...bots, own.agent]} />
      <Routes><Route path="/" element={<AssistantEntry mode="entry" />} />
        <Route path="/agents/:id" element={<Location />} /></Routes>
    </MemoryRouter></QueryClientProvider>);
    await screen.findByTestId('destination');
    await act(async () => { older.resolve(response(unbound)); await older.promise; });
    await waitFor(() => expect((client.getQueryData(bindingKey) as typeof own)?.agent?.id).toBe('own'));
    expect(document.querySelector('.sidebar-agent-link')?.getAttribute('href')).toBe('/agents/own#chat');
  });
  it('never auto-provisions for a platform admin inside a selected company', async () => {
    useAuthStore.setState({ user: { ...user, role: 'platform_admin' } as never });
    transport();
    mount();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/recent#chat'));
    expect(writes()).toHaveLength(0);
  });
  it('keeps company selection and can select the platform admin home company explicitly', async () => {
    localStorage.removeItem('current_tenant_id');
    useAuthStore.setState({ user: { ...user, role: 'platform_admin' } as never });
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (!new Headers(init?.headers).get('X-Tenant-Id')) return response({ detail: { state: 'company_required' } }, 409);
      return response(url.startsWith('/api/assistant') ? unbound : bots);
    }));
    mount();
    expect(await screen.findByText('请先选择公司')).toBeTruthy();
    expect(screen.queryByText('请联系公司管理员设置助理。')).toBeNull();
    act(() => {
      localStorage.setItem('current_tenant_id', 'company');
      window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'company' }));
    });
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/recent#chat'));
    expect(writes()).toHaveLength(0);
  });
  it('still provisions automatically for a company admin', async () => {
    useAuthStore.setState({ user: { ...user, role: 'org_admin' } as never });
    transport(); mount();
    await screen.findByTestId('destination');
    expect(writes()).toHaveLength(1);
  });
  it.each([
    [{ access_level: 'operator' }, '/agents/recent?manage=true#chat'],
    [{ agent_type: 'local_agent' }, '/agents/recent'],
  ])('uses the sidebar link for fallback %j', async (attributes, destination) => {
    transport(unbound, { ...unbound, state: 'model_forbidden' }, [{ ...bots[0], ...attributes }]);
    mount();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe(destination));
  });
});

describe('own assistant identity and settings', () => {
  function settings(id: string) {
    return <AgentSettingsSection {...{
      agentId: id, agent: { id, name: 'My Assistant' }, llmModels: [], canManage: true,
      settingsForm: {}, onSettingsFormChange: vi.fn(), settingsError: '', wmDraft: '',
    } as any} />;
  }
  it('hides change-employee settings for a different employee', async () => {
    transport(own);
    const client = new QueryClient();
    client.setQueryData(bindingKey, own);
    mount(settings('recent'), client);
    expect(screen.queryByRole('link', { name: '改选其他员工' })).toBeNull();
  });
  it('changes employee from the own assistant settings with a router link', async () => {
    transport(own); mount(settings('own'));
    fireEvent.click(await screen.findByRole('link', { name: '改选其他员工' }));
    expect(await screen.findByTestId('destination')).toHaveProperty('textContent', '/assistant?settings=true');
  });
  it.each(['zh', 'en'])('localizes the bound sidebar label and header without renaming other assistants: %s', async (language) => {
    locale.language = language;
    transport(own);
    mount(<><AppSidebar {...sidebarProps} agents={[own.agent, { id: 'other', name: 'Other Assistant' }]} />
      <AgentDetailHeader agent={own.agent} agentId="own" canManage={false} isOperatorOnly={false}
        isSystemHr={false} onError={vi.fn()} statusKey="idle" /></>);
    const title = language === 'zh' ? '我的助理' : 'My Assistant';
    expect(await screen.findByRole('link', { name: `${language === 'zh' ? '打开' : 'Open'} ${title}` })).toBeTruthy();
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(title));
    expect(screen.getByRole('link', { name: `${language === 'zh' ? '打开' : 'Open'} Other Assistant` })).toBeTruthy();
    expect(own.agent.name).toBe('My Assistant');
  });
  it.each(['zh', 'en'])('localizes the own assistant empty conversation: %s', async (language) => {
    locale.language = language;
    transport(own);
    mount(<AgentChatSection {...{
      agentId: 'own', agent: own.agent, currentUser: user, isAdmin: false, sessionOnly: true,
      sessions: [], allSessions: [], activeSession: null, historyMsgs: [], chatMessages: [], attachedFiles: [],
      historyContainerRef: React.createRef(), chatContainerRef: React.createRef(), chatEndRef: React.createRef(),
      fileInputRef: React.createRef(), chatInputRef: React.createRef(), chatInput: '',
    } as any} />);
    const text = language === 'zh' ? '开始与 我的助理 对话' : 'Start a conversation with My Assistant';
    expect(await screen.findByText(text)).toBeTruthy();
  });
});
