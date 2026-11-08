// @vitest-environment jsdom

import React from 'react';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const harness = vi.hoisted(() => ({
  navigate: vi.fn(),
  getHrAgent: vi.fn(),
  getAssistant: vi.fn(),
  location: {
    pathname: '/agents/agent-1/sessions/session-old',
    search: '',
    hash: '',
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, values?: Record<string, unknown>) => (fallback ?? _key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(values?.[name] ?? '')),
    i18n: { language: 'en' },
  }),
}));

vi.mock('react-router-dom', () => ({
  NavLink: ({ to, children, className, end: _end, ...props }: any) => (
    <a href={String(to)} className={typeof className === 'function' ? className({ isActive: false }) : className} {...props}>
      {children}
    </a>
  ),
  useLocation: () => harness.location,
  useNavigate: () => harness.navigate,
}));

vi.mock('../../api/domains/agents', () => ({
  agentApi: { getHrAgent: harness.getHrAgent },
}));

vi.mock('../../api/domains/assistant', () => ({
  assistantApi: { get: harness.getAssistant },
}));

import AppSidebar from './AppSidebar';

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

function renderSidebar(element: React.ReactElement) {
  return render(
    <QueryClientProvider client={queryClient}>
      {element}
    </QueryClientProvider>,
  );
}

const baseProps = {
  theme: 'light' as const,
  isSidebarCollapsed: false,
  onToggleSidebar: vi.fn(),
  tenants: [{ id: 'tenant-1', name: 'Company A' }],
  currentTenant: 'tenant-1',
  onSwitchTenant: vi.fn(),
  isChinese: false,
  onToggleTheme: vi.fn(),
  onOpenNotifications: vi.fn(),
  unreadCount: 0,
  accountMenuRef: React.createRef<HTMLDivElement>(),
  showAccountMenu: false,
  onToggleAccountMenu: vi.fn(),
  onToggleLang: vi.fn(),
  onOpenAccountSettings: vi.fn(),
  onLogout: vi.fn(),
  versionDisplay: null,
};

describe('AppSidebar bot-list authority (B3: the bot is the way in)', () => {
  beforeEach(() => {
    harness.location.pathname = '/agents/agent-1/sessions/session-old';
    harness.location.search = '';
    harness.location.hash = '';
    harness.navigate.mockReset();
    harness.getHrAgent.mockReset();
    harness.getHrAgent.mockResolvedValue(null);
    harness.getAssistant.mockReset();
    harness.getAssistant.mockResolvedValue({
      schema: 'hive.assistant_binding.v1',
      agent: { id: 'agent-assistant', name: 'My Assistant' },
      entry: { kind: 'session', path: '/agents/agent-assistant', agent_id: 'agent-assistant', session_id: null },
      candidates: [],
    });
  });

  afterEach(() => cleanup());

  it('does not provision an HR bot to render a creation entry', async () => {
    renderSidebar(
      <AppSidebar {...baseProps} user={{ id: 'platform-user', role: 'platform_admin' }} agents={[]} />,
    );

    await Promise.resolve();
    expect(harness.getHrAgent).not.toHaveBeenCalled();
  });

  it('waits for a selected company before resolving the HR agent for a platform administrator', async () => {
    renderSidebar(
      <AppSidebar {...baseProps} user={{ id: 'platform-user', role: 'platform_admin' }} currentTenant="" agents={[]} />,
    );

    await Promise.resolve();
    expect(harness.getHrAgent).not.toHaveBeenCalled();
  });

  it('orders the bound assistant bot first and drops it nowhere else', async () => {
    renderSidebar(
      <AppSidebar {...baseProps}
        user={{ id: 'user-1', role: 'member' }}
        agents={[
          { id: 'agent-1', name: 'Release Bot', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' },
          { id: 'agent-assistant', name: 'My Assistant', created_at: '2026-03-20T00:00:00Z', status: 'idle', agent_type: 'native' },
        ]} />,
    );

    await waitFor(() => {
      const links = screen.getAllByRole('link').map((link) => link.getAttribute('href'));
      const botLinks = links.filter((href) => href && /^\/agents\//.test(href));
      expect(botLinks[0]).toBe('/agents/agent-assistant#chat');
      expect(botLinks).toContain('/agents/agent-1#chat');
    });
  });

  it('routes employee creation into the ordinary assistant conversation', async () => {
    harness.location.pathname = '/home';
    const { fireEvent } = await import('@testing-library/react');
    renderSidebar(
      <AppSidebar {...baseProps}
        user={{ id: 'user-1', role: 'org_admin' }}
        agents={[]}
        hrAgent={{ id: 'hr-agent-1', name: 'HR Agent', status: 'running' } as any} />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'New digital employee' }));

    expect(harness.navigate).toHaveBeenCalledTimes(1);
    expect(harness.navigate).toHaveBeenCalledWith('/');
  });

  it('keeps operator-only rows as manage links without conversation affordances', () => {
    harness.location.pathname = '/agents/operator-agent';
    renderSidebar(
      <AppSidebar {...baseProps}
        user={{ id: 'operator-user', role: 'member' }}
        agents={[{
          id: 'operator-agent',
          name: 'Audited Agent',
          status: 'running',
          agent_type: 'native',
          access_level: 'operator',
        }]} />,
    );

    expect(screen.getAllByRole('link', { name: /Audited Agent/ }).every(
      (link) => link.getAttribute('href') === '/agents/operator-agent?manage=true#chat',
    )).toBe(true);
    expect(screen.queryByRole('button', { name: /Audited Agent/ })).toBeNull();
    expect(screen.queryByTestId('sidebar-agent-sessions-operator-agent')).toBeNull();
  });
});
