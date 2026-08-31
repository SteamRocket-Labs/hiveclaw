// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const harness = vi.hoisted(() => ({
  navigate: vi.fn(),
  createSession: vi.fn(() => new Promise(() => undefined)),
  createLocalSession: vi.fn(),
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
    listSessions: vi.fn().mockResolvedValue([]),
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
    harness.navigate.mockReset();
    harness.createSession.mockClear();
    harness.createLocalSession.mockReset();
    harness.getHrAgent.mockReset();
  });

  afterEach(() => cleanup());

  it('does not request the company HR agent for a platform administrator', async () => {
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
});
