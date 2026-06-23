import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AppSidebar from './AppSidebar';
import NotificationCenter from './NotificationCenter';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: string | Record<string, unknown>) => (typeof opts === 'string' ? opts : null) || key.split('.').pop() || key,
    i18n: { language: 'en', changeLanguage: vi.fn() },
  }),
}));

vi.mock('react-router-dom', () => ({
  NavLink: ({ to, className, children, title }: any) => (
    <a href={to} className={typeof className === 'function' ? className({ isActive: false }) : className} title={title}>
      {children}
    </a>
  ),
}));

describe('Layout extracted sections', () => {
  it('renders AppSidebar as a standalone shell module', () => {
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
        theme="dark"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }, { id: 'tenant-2', name: 'Company B' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[
          { id: 'agent-1', name: 'Agent One', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' },
          { id: 'agent-2', name: 'Agent Two', created_at: '2026-03-26T00:00:00Z', status: 'idle', agent_type: 'native' },
          { id: 'agent-3', name: 'Agent Three', created_at: '2026-03-25T00:00:00Z', status: 'idle', agent_type: 'native' },
          { id: 'agent-4', name: 'Agent Four', created_at: '2026-03-24T00:00:00Z', status: 'idle', agent_type: 'native' },
          { id: 'agent-5', name: 'Agent Five', created_at: '2026-03-23T00:00:00Z', status: 'idle', agent_type: 'native' },
        ]}
        pinnedAgents={new Set(['agent-1'])}
        onTogglePin={vi.fn()}
        isChinese={false}
        sidebarSearch=""
        onSetSidebarSearch={vi.fn()}
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={3}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu={true}
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={<div>Version Mock</div>}
      />,
    );

    expect(markup).toContain('HiveClaw');
    expect(markup).toContain('Workspace');
    expect(markup).toContain('Company A');
    expect(markup).toContain('Digital Employees');
    expect(markup).toContain('Tasks / Automation');
    expect(markup).toContain('Bridge');
    expect(markup).toContain('Agent Circle');
    expect(markup).not.toContain('Conversations &amp; Tasks');
    expect(markup).not.toContain('Plan Review');
    expect(markup).not.toContain('Memory &amp; Knowledge');
    expect(markup).not.toContain('Documents &amp; Research');
    expect(markup).not.toContain('A2A / Team');
    expect(markup).not.toContain('Workspace search');
    expect(markup).not.toContain('Control Plane');
    expect(markup).toContain('href="/local-agents"');
    expect(markup).not.toContain('href="/team"');
    expect(markup).toContain('href="/agents"');
    expect(markup).toContain('href="/automations"');
    expect(markup).not.toContain('href="/enterprise/tools"');
    expect(markup).toContain('Agent One');
    expect(markup).toContain('New Digital Employee');
    expect(markup).toContain('Company Admin');
    expect(markup).toContain('Platform Settings');
    expect(markup).toContain('Version Mock');
  });

  it('keeps create employee inside the digital employee tree instead of a workspace search block', () => {
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'agent-1', name: 'Research Lead', role_description: 'Market research', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' }]}
        pinnedAgents={new Set()}
        onTogglePin={vi.fn()}
        isChinese={false}
        sidebarSearch="local"
        onSetSidebarSearch={vi.fn()}
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

    expect(markup).not.toContain('Workspace search');
    expect(markup).not.toContain('Search workspace, employees, routes...');
    expect(markup).not.toContain('Quick open');
    expect(markup).toContain('Digital Employees');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('New Digital Employee');
    expect(markup).toContain('href="/local-agents"');
  });

  it('renders NotificationCenter as a standalone notification module', () => {
    const markup = renderToStaticMarkup(
      <NotificationCenter
        isOpen={true}
        unreadCount={2}
        notifications={[
          {
            id: 'notif-1',
            title: 'Deploy notice',
            body: 'Release finished successfully.',
            is_read: false,
            created_at: '2026-03-27T10:00:00Z',
          },
        ]}
        notifCategory="all"
        onSetNotifCategory={vi.fn()}
        onMarkAllRead={vi.fn()}
        onClose={vi.fn()}
        onNotificationClick={vi.fn()}
        selectedNotification={{
          id: 'notif-1',
          title: 'Deploy notice',
          body: 'Release finished successfully.',
          sender_name: 'System',
          created_at: '2026-03-27T10:00:00Z',
        }}
        onCloseDetail={vi.fn()}
      />,
    );

    expect(markup).toContain('title');       // t('notifications.title')
    expect(markup).toContain('Deploy notice');
    expect(markup).toContain('Release finished successfully.');
    expect(markup).toContain('markAllRead'); // t('notifications.markAllRead')
  });
});
