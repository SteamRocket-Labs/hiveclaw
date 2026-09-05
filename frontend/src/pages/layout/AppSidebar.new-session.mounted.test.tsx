// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const harness = vi.hoisted(() => ({
  navigate: vi.fn(),
  createSession: vi.fn(() => new Promise(() => undefined)),
  createLocalSession: vi.fn(),
  listSessions: vi.fn().mockResolvedValue([]),
  getHrAgent: vi.fn(),
  location: {
    pathname: '/agents/agent-1/sessions/session-old',
    search: '',
    hash: '',
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
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

vi.mock('../../api/domains/chat', () => ({
  chatApi: {
    createSession: harness.createSession,
    listSessions: harness.listSessions,
    deleteSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock('../../api/domains/localBridge', () => ({
  localBridgeApi: {
    createAgentChannelSession: harness.createLocalSession,
    listAgentChannelSessions: vi.fn().mockResolvedValue([]),
    deleteAgentChannelSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock('../../api/domains/agents', () => ({
  agentApi: {
    getHrAgent: harness.getHrAgent,
  },
}));

import AppSidebar from './AppSidebar';

describe('AppSidebar new conversation authority', () => {
  beforeEach(() => {
    harness.location.pathname = '/agents/agent-1/sessions/session-old';
    harness.location.search = '';
    harness.location.hash = '';
    harness.navigate.mockReset();
    harness.createSession.mockClear();
    harness.createLocalSession.mockReset();
    harness.listSessions.mockClear();
    harness.getHrAgent.mockReset();
    harness.getHrAgent.mockResolvedValue(null);
  });

  afterEach(() => cleanup());

  it('resolves the company HR agent for a platform administrator inside the selected company', async () => {
    render(
      <AppSidebar
        user={{ id: 'platform-user', role: 'platform_admin' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[]}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    await vi.waitFor(() => {
      expect(harness.getHrAgent).toHaveBeenCalled();
    });
  });

  it('waits for a selected company before resolving the HR agent for a platform administrator', async () => {
    render(
      <AppSidebar
        user={{ id: 'platform-user', role: 'platform_admin' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant=""
        onSwitchTenant={vi.fn()}
        agents={[]}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    await Promise.resolve();
    expect(harness.getHrAgent).not.toHaveBeenCalled();
  });

  it('hands a native new-conversation click to the draft route before durable Session creation', () => {
    render(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'agent-1', name: 'Release Bot', status: 'running', agent_type: 'native' }]}
        hrAgent={null}
        agentSessionsByAgentId={{ 'agent-1': [] }}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'New conversation with Release Bot' }));

    expect(harness.createSession).not.toHaveBeenCalled();
    expect(harness.navigate).toHaveBeenCalledTimes(1);
    expect(harness.navigate).toHaveBeenCalledWith(
      '/agents/agent-1#chat',
      {
        state: {
          newSessionDraft: {
            agent_id: 'agent-1',
            request_id: expect.any(String),
          },
        },
      },
    );
  });

  it('keeps Local Agent channel-session creation on its existing runtime path', async () => {
    harness.createLocalSession.mockResolvedValueOnce({
      id: 'local-channel-session-1',
      chat_session_id: 'local-chat-session-1',
      title: 'Local conversation',
      source: 'web',
      source_channel: 'local_agent',
      session_kind: 'local_agent_channel',
      status: 'active',
      created_at: '2026-08-29T12:00:00Z',
      updated_at: '2026-08-29T12:00:00Z',
      last_message_at: null,
    });

    render(
      <AppSidebar
        user={{ id: 'user-1', role: 'member' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'local-agent-1', name: 'Local Codex', status: 'running', agent_type: 'local_agent' }]}
        hrAgent={null}
        agentSessionsByAgentId={{ 'local-agent-1': [] }}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'New conversation with Local Codex' }));

    expect(harness.createSession).not.toHaveBeenCalled();
    expect(harness.createLocalSession).toHaveBeenCalledWith('local-agent-1', { title: 'New Conversation' });
    await vi.waitFor(() => {
      expect(harness.navigate).toHaveBeenCalledWith(
        '/agents/local-agent-1?session_id=local-chat-session-1#chat',
      );
    });
  });

  it('keeps operator-only rows out of mine-session and new-conversation surfaces', () => {
    harness.location.pathname = '/agents/operator-agent';
    render(
      <AppSidebar
        user={{ id: 'operator-user', role: 'member' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{
          id: 'operator-agent',
          name: 'Audited Agent',
          status: 'running',
          agent_type: 'native',
          access_level: 'operator',
        }]}
        hrAgent={null}
        agentSessionsByAgentId={{
          'operator-agent': [{ id: 'private-session', title: 'Must not expand' } as any],
        }}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    expect(screen.queryByRole('button', { name: 'New conversation with Audited Agent' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Expand sessions' })).toBeNull();
    expect(screen.queryByTestId('sidebar-agent-sessions-operator-agent')).toBeNull();
    expect(screen.queryByText('Must not expand')).toBeNull();
    expect(screen.getAllByRole('link', { name: /Audited Agent/ }).every((link) => (
      link.getAttribute('href') === '/agents/operator-agent?manage=true#chat'
    ))).toBe(true);
    expect(harness.listSessions).not.toHaveBeenCalled();
  });

  it('loads the managed company session tree for a scoped administrator without an operator reason', async () => {
    render(
      <AppSidebar
        user={{ id: 'admin-1', role: 'org_admin' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{
          id: 'agent-managed',
          name: 'Employee Agent',
          status: 'running',
          agent_type: 'native',
          access_level: 'manage',
          is_owner: false,
          action_capabilities: { can_manage_permissions: true },
        }]}
        hrAgent={null}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Expand sessions' }));

    await vi.waitFor(() => {
      expect(harness.listSessions).toHaveBeenCalledWith('agent-managed', 'all');
    });
  });

  it('keeps employees on their own session scope even with a legacy manage grant', async () => {
    render(
      <AppSidebar
        user={{ id: 'member-1', role: 'member' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{
          id: 'agent-granted',
          name: 'Granted Agent',
          status: 'running',
          agent_type: 'native',
          access_level: 'manage',
          is_owner: false,
          action_capabilities: { can_manage_permissions: false },
        }]}
        hrAgent={null}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Expand sessions' }));

    await vi.waitFor(() => {
      expect(harness.listSessions).toHaveBeenCalledWith('agent-granted', 'mine');
    });
  });

  it('defers the managed tree until the roster lands, then loads the scoped inventory', async () => {
    harness.location.pathname = '/agents/agent-managed';
    const managedAgent = {
      id: 'agent-managed',
      name: 'Employee Agent',
      status: 'running',
      agent_type: 'native',
      access_level: 'manage',
      is_owner: false,
      action_capabilities: { can_manage_permissions: true },
    };
    const props = {
      user: { id: 'admin-1', role: 'org_admin' },
      theme: 'light' as const,
      isSidebarCollapsed: false,
      onToggleSidebar: vi.fn(),
      tenants: [{ id: 'tenant-1', name: 'Company A' }],
      currentTenant: 'tenant-1',
      onSwitchTenant: vi.fn(),
      hrAgent: null,
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

    const view = render(<AppSidebar {...props} agents={[]} />);
    await Promise.resolve();
    // Roster still loading: no session call may freeze the tree on 'mine'.
    expect(harness.listSessions).not.toHaveBeenCalled();

    view.rerender(<AppSidebar {...props} agents={[managedAgent]} />);
    await vi.waitFor(() => {
      expect(harness.listSessions).toHaveBeenCalledWith('agent-managed', 'all');
    });
  });

  it('shows the session delete control only on the administrator’s own rows in the managed tree', async () => {
    render(
      <AppSidebar
        user={{ id: 'admin-1', role: 'platform_admin' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{
          id: 'agent-managed',
          name: 'Employee Agent',
          status: 'running',
          agent_type: 'native',
          access_level: 'manage',
          is_owner: false,
          action_capabilities: { can_manage_permissions: true },
        }]}
        hrAgent={null}
        agentSessionsByAgentId={{
          'agent-managed': [
            {
              id: 'session-own',
              agent_id: 'agent-managed',
              user_id: 'admin-1',
              title: 'Admin own session',
              created_at: '2026-09-01T08:00:00Z',
              updated_at: '2026-09-01T08:00:00Z',
            } as any,
            {
              id: 'session-employee',
              agent_id: 'agent-managed',
              user_id: 'employee-9',
              title: 'Employee private session',
              authority_source: 'scoped_business_admin',
              operator_view: false,
              created_at: '2026-09-01T09:00:00Z',
              updated_at: '2026-09-01T09:00:00Z',
            } as any,
          ],
        }}
        isChinese={false}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={false}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Expand sessions' }));

    expect(await screen.findByText('Employee private session')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Delete session Employee private session' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Delete session Admin own session' })).toBeTruthy();
  });
});
