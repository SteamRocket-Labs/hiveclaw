import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import AppSidebar, {
  buildSidebarSessionFamilies,
  displaySessionTitle,
  getSessionTag,
  getSessionTags,
  isCompactSidebarViewport,
  sidebarSessionFromLocalAgentChannelSession,
} from './AppSidebar';
import NotificationCenter from './NotificationCenter';

const routeState = vi.hoisted(() => ({
  location: { pathname: '/agents', search: '', hash: '' },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, opts?: string | Record<string, unknown>) => (typeof opts === 'string' ? opts : null) || key.split('.').pop() || key,
    i18n: { language: 'en', changeLanguage: vi.fn() },
  }),
}));

vi.mock('react-router-dom', () => ({
  NavLink: ({ to, className, children, title, end: _end, ...rest }: any) => {
    const href = String(to);
    const targetPath = href.split(/[?#]/)[0] || href;
    const isActive = routeState.location.pathname === targetPath;
    return (
      <a href={href} className={typeof className === 'function' ? className({ isActive }) : className} title={title} {...rest}>
        {children}
      </a>
    );
  },
  useNavigate: () => vi.fn(),
  useLocation: () => routeState.location,
}));

function cssBlock(css: string, selector: string) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escapedSelector}\\s*\\{([\\s\\S]*?)\\}`));
  return match?.[1] ?? '';
}

describe('Layout extracted sections', () => {
  it('reserves the narrow viewport for the workbench by default-collapsing the app sidebar', () => {
    expect(isCompactSidebarViewport(900)).toBe(true);
    expect(isCompactSidebarViewport(901)).toBe(false);
  });

  it('labels agent-to-agent sessions with an A2A badge', () => {
    const t = (key: string, fallback?: string) => fallback || key;
    expect(getSessionTag({
      source_channel: 'agent',
      participant_type: 'agent',
      session_kind: 'delegation_run',
      title: 'Lead ↔ Researcher',
    }, t)).toBe('A2A');
    expect(getSessionTag({
      source_channel: 'web',
      session_kind: 'agent_chat',
      title: 'Lead ↔ Researcher',
    }, t)).toBe('A2A');
    expect(getSessionTag({
      source_channel: 'web',
      session_kind: 'delegation_run',
      title: 'Delegation: Researcher',
    }, t)).toBe('A2A');
  });

  it('groups branch sessions under their root and uses badges instead of title suffixes', () => {
    const t = (key: string, fallback?: string) => fallback || key;
    const root = {
      id: 'session-root',
      agent_id: 'agent-1',
      title: '你能干嘛?',
      created_at: '2026-06-29T08:00:00Z',
      updated_at: '2026-06-29T08:00:00Z',
    } as any;
    const branch = {
      id: 'session-branch',
      agent_id: 'agent-1',
      title: '你能干嘛? (branch)',
      parent_session_id: 'session-root',
      root_session_id: 'session-root',
      session_kind: 'agent_chat',
      transcript_metadata_json: { branch_mode: 'branch' },
      created_at: '2026-06-29T08:01:00Z',
      updated_at: '2026-06-29T08:01:00Z',
    } as any;

    const families = buildSidebarSessionFamilies([branch, root]);

    expect(families).toHaveLength(1);
    expect(families[0].root.id).toBe('session-root');
    expect(families[0].children.map((item) => item.id)).toEqual(['session-branch']);
    expect(displaySessionTitle(branch, t)).toBe('你能干嘛?');
    expect(getSessionTags(branch, t)).toEqual(['A2A', 'Branch']);
    expect(getSessionTag(branch, t)).toBe('A2A');
  });

  it('does not expose repeated edit lineage suffixes as the session name', () => {
    const t = (key: string, fallback?: string) => fallback || key;
    const nestedEdit = {
      id: 'session-grandchild',
      title: 'Quarterly research brief (edit) (edit)',
      parent_session_id: 'session-child',
      root_session_id: 'session-root',
      transcript_metadata_json: { branch_mode: 'edit' },
    } as any;

    expect(displaySessionTitle(nestedEdit, t)).toBe('Quarterly research brief');
  });

  it('binds main content width to the collapsed sidebar state at the app layout scope', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const css = readFileSync(new URL('../../index.css', import.meta.url), 'utf8');

    expect(css).toContain('.app-layout.sidebar-collapsed');
    expect(css).toContain('--sidebar-width: var(--sidebar-width-collapsed);');
    expect(css).toContain('.main-content:has(.agent-detail-session-only)');
    expect(css).toContain('.agent-detail-session-only');
    expect(css).toContain('margin-left: var(--sidebar-width);');
    expect(css).toContain('width: calc(100vw - var(--sidebar-width));');
  });

  it('keeps session navigation active states precise and GitLine hover styles viewport anchored', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const css = readFileSync(new URL('../../index.css', import.meta.url), 'utf8');

    expect(cssBlock(css, '.sidebar-session-row.has-active-branch:not(.active)')).toContain('background: transparent;');
    expect(cssBlock(css, '.sidebar-session-row.has-active-branch:not(.active)::before')).toContain('width: 2px;');
    expect(cssBlock(css, '.session-gitline-hovercard')).toContain('position: fixed;');
    expect(cssBlock(css, '.session-gitline-hovercard')).not.toContain('position: absolute;');
    expect(css).not.toContain('scaleX(1.8) scaleY(1.4)');
    expect(css).not.toContain('scaleX(1.72) scaleY(1.3)');
  });

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
          { id: 'agent-5', name: 'Codex on Mac', created_at: '2026-03-23T00:00:00Z', status: 'idle', agent_type: 'local_agent' },
        ]}
        isChinese={false}
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
    expect(markup).toContain('Company B');
    expect(markup).toContain('sidebar-workspace-select');
    expect(markup).not.toContain('tenant-switcher');
    expect(markup).not.toContain('My Workspace');
    expect(markup).toContain('href="/home"');
    expect(markup).toContain('title="Home"');
    expect(markup).toContain('Digital Employees');
    expect(markup).toContain('Automation');
    expect(markup).toContain('Agent Circle');
    expect(markup).toContain('title="Knowledge"');
    expect(markup).not.toContain('Conversations &amp; Tasks');
    expect(markup).not.toContain('Plan Review');
    expect(markup).not.toContain('Memory &amp; Knowledge');
    expect(markup).not.toContain('Documents &amp; Research');
    expect(markup).not.toContain('A2A / Team');
    expect(markup).not.toContain('Workspace search');
    expect(markup).not.toContain('Control Plane');
    expect(markup).toContain('title="Local connection"');
    expect(markup).toContain('href="/knowledge"');
    expect(markup).toContain('href="/local-agents"');
    expect(markup).not.toContain('href="/team"');
    expect(markup).toContain('href="/agents"');
    expect(markup).toContain('href="/automations"');
    expect(markup).not.toContain('href="/enterprise/tools"');
    expect(markup).toContain('Agent One');
    expect(markup).toContain('Codex on Mac');
    expect(markup).toContain('Local');
    expect(markup).toContain('New digital employee');
    expect(markup).toContain('Settings');
    expect(markup).toContain('Rocky');
    expect(markup).toContain('Super Admin');
    expect(markup).toContain('Company Admin');
    expect(markup).toContain('Platform Settings');
    expect(markup).toContain('Notifications');
    expect(markup).toContain('Theme');
    expect(markup).not.toContain('sidebar-primary-actions');
    expect(markup).not.toContain('sidebar-account-row-static');
    expect(markup).not.toContain('platformAdmin');
    expect(markup).toContain('Version Mock');
  });

  it('labels the language action with its destination when the current locale is Chinese', () => {
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'member', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[]}
        isChinese
        onToggleTheme={vi.fn()}
        onOpenNotifications={vi.fn()}
        unreadCount={0}
        accountMenuRef={React.createRef<HTMLDivElement>()}
        showAccountMenu
        onToggleAccountMenu={vi.fn()}
        onToggleLang={vi.fn()}
        onOpenAccountSettings={vi.fn()}
        onLogout={vi.fn()}
        versionDisplay={null}
      />,
    );

    expect(markup).toContain('English');
    expect(markup).not.toContain('>中文<');
  });

  it('keeps employee creation as a simple action below the employee tree', () => {
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

    expect(markup).not.toContain('Workspace search');
    expect(markup).not.toContain('Search workspace, employees, routes...');
    expect(markup).not.toContain('Quick open');
    expect(markup).toContain('Digital Employees');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('New digital employee');
    expect(markup).toContain('data-testid="sidebar-create-agent-block"');
    expect(markup).not.toContain('aria-label="Toggle Create Agent sessions"');
    expect(markup).not.toContain('sidebar-create-agent-sessions');
    expect(markup).toContain('class="sidebar-item sidebar-create-agent-link');
    expect(markup).toContain('href="/home"');
    expect(markup).toContain('href="/local-agents"');
  });

  it('routes the simple employee-creation action to the real HR Agent when available', () => {
    routeState.location = { pathname: '/agents/hr-agent-1', search: '?session_id=hr-session-1', hash: '#chat' };
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[]}
        hrAgent={{ id: 'hr-agent-1', name: 'HR Agent', status: 'running' }}
        agentSessionsByAgentId={{
          'hr-agent-1': [
            {
              id: 'hr-session-1',
              agent_id: 'hr-agent-1',
              title: 'Onboard a sales assistant',
              created_at: '2026-06-23T08:34:00Z',
              updated_at: '2026-06-23T08:34:00Z',
            },
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

    expect(markup).toContain('data-testid="sidebar-create-agent-block"');
    expect(markup).toContain('New digital employee');
    expect(markup).not.toContain('href="/agents/hr-agent-1#chat"');
    expect(markup).not.toContain('data-testid="sidebar-create-agent-sessions"');
    expect(markup).not.toContain('Onboard a sales assistant');
    expect(markup).not.toContain('aria-label="Toggle Create Agent sessions"');
  });

  it('keeps the user identity hidden until Settings is opened', () => {
    routeState.location = { pathname: '/home', search: '', hash: '' };
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
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

    expect(markup).toContain('Settings');
    expect(markup).not.toContain('Rocky');
    expect(markup).not.toContain('Super Admin');
    expect(markup).not.toContain('sidebar-account-row-static');
  });

  it('renders conversations under the expanded agent dropdown instead of a chat-page side column', () => {
    routeState.location = { pathname: '/agents/agent-1', search: '?session_id=session-1', hash: '#chat' };
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'agent-1', name: 'AI 产品经理', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' }]}
        agentSessionsByAgentId={{
          'agent-1': [
            {
              id: 'session-1',
              agent_id: 'agent-1',
              created_at: '2026-06-23T08:34:00Z',
              updated_at: '2026-06-23T08:34:00Z',
            },
            {
              id: 'session-task',
              agent_id: 'agent-1',
              title: '每周竞品扫描',
              created_at: '2026-06-22T08:34:00Z',
              updated_at: '2026-06-22T08:34:00Z',
              runtime_task_id: 'task-1',
            } as any,
            {
              id: 'session-im',
              agent_id: 'agent-1',
              title: '来自飞书的用户问答',
              created_at: '2026-06-21T08:34:00Z',
              updated_at: '2026-06-21T08:34:00Z',
              source_channel: 'feishu',
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

    expect(markup).toContain('data-testid="sidebar-agent-sessions-agent-1"');
    expect(markup).toContain('aria-label="Open AI 产品经理"');
    expect(markup).toContain('href="/agents/agent-1" class="sidebar-item sidebar-agent-link');
    expect(markup).not.toContain('href="/agents/agent-1#chat" class="sidebar-item sidebar-agent-link');
    expect(markup).toContain('href="/agents/agent-1?session_id=session-1#chat"');
    expect(markup).toContain('class="sidebar-session-item active"');
    expect(markup.match(/class="sidebar-session-item active"/g) || []).toHaveLength(1);
    expect(markup).not.toContain('sidebar-agent-link active');
    expect(markup).toContain('aria-label="New conversation with AI 产品经理"');
    expect(markup).toContain('aria-label="Open AI 产品经理 details"');
    expect(markup).not.toContain('class="sidebar-session-new"');
    expect(markup).toContain('Task');
    expect(markup).toContain('IM');
    expect(markup).toMatch(/class="sidebar-session-row active"[\s\S]*class="sidebar-session-item active"[\s\S]*class="sidebar-session-action"/);
    expect(markup).not.toContain('My Conversations');
    expect(markup).not.toContain('All Users');
  });

  it('renders branch conversations as collapsible children of the root session row', () => {
    routeState.location = { pathname: '/agents/agent-1', search: '?session_id=session-branch', hash: '#chat' };
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'agent-1', name: 'AI 产品经理', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' }]}
        agentSessionsByAgentId={{
          'agent-1': [
            {
              id: 'session-root',
              agent_id: 'agent-1',
              title: '你能干嘛?',
              created_at: '2026-06-29T08:00:00Z',
              updated_at: '2026-06-29T08:00:00Z',
            },
            {
              id: 'session-branch',
              agent_id: 'agent-1',
              title: '你能干嘛? (branch)',
              parent_session_id: 'session-root',
              root_session_id: 'session-root',
              created_at: '2026-06-29T08:01:00Z',
              updated_at: '2026-06-29T08:01:00Z',
              transcript_metadata_json: { branch_mode: 'branch' },
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

    expect(markup).toContain('data-testid="sidebar-session-family-session-root"');
    expect(markup).toContain('data-testid="sidebar-session-children-session-root"');
    expect(markup).toContain('href="/agents/agent-1?session_id=session-root#chat"');
    expect(markup).toContain('href="/agents/agent-1?session_id=session-branch#chat"');
    expect(markup).toContain('Branch');
    expect(markup).toContain('data-testid="sidebar-session-branch-count"');
    expect(markup).not.toContain('你能干嘛? (branch)');
    expect(markup.match(/class="sidebar-session-row active/g) || []).toHaveLength(1);
    expect(markup.match(/class="sidebar-session-item active"/g) || []).toHaveLength(1);
    expect(markup).toContain('class="sidebar-session-row has-children has-active-branch"');
    expect(markup).toContain('class="sidebar-session-row active branch-child"');
  });

  it('marks only the path-selected session active on session-only routes', () => {
    routeState.location = { pathname: '/agents/agent-1/sessions/session-2', search: '', hash: '' };
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'agent-1', name: 'AI 产品经理', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' }]}
        agentSessionsByAgentId={{
          'agent-1': [
            {
              id: 'session-1',
              agent_id: 'agent-1',
              title: 'First session',
              created_at: '2026-06-29T08:00:00Z',
              updated_at: '2026-06-29T08:00:00Z',
            },
            {
              id: 'session-2',
              agent_id: 'agent-1',
              title: 'Second session',
              created_at: '2026-06-29T08:01:00Z',
              updated_at: '2026-06-29T08:01:00Z',
            },
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

    expect(markup.match(/class="sidebar-session-row active"/g) || []).toHaveLength(1);
    expect(markup.match(/class="sidebar-session-item active"/g) || []).toHaveLength(1);
    expect(markup).toMatch(/class="sidebar-session-row active"[\s\S]*Second session/);
    expect(markup).not.toMatch(/class="sidebar-session-row active"[\s\S]*First session[\s\S]*class="sidebar-session-row active"/);
  });

  it('renders local agents as normal agent rows with local session dropdowns', () => {
    routeState.location = { pathname: '/agents/local-agent-1', search: '?session_id=chat-session-1', hash: '#chat' };
    const markup = renderToStaticMarkup(
      <AppSidebar
        user={{ id: 'user-1', role: 'member', display_name: 'Rocky' }}
        theme="light"
        isSidebarCollapsed={false}
        onToggleSidebar={vi.fn()}
        tenants={[{ id: 'tenant-1', name: 'Company A' }]}
        currentTenant="tenant-1"
        onSwitchTenant={vi.fn()}
        agents={[{ id: 'local-agent-1', name: 'Codex on Mac', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'local_agent' }]}
        agentSessionsByAgentId={{
          'local-agent-1': [
            sidebarSessionFromLocalAgentChannelSession('local-agent-1', {
              id: 'channel-session-1',
              chat_session_id: 'chat-session-1',
              title: 'Codex local debug',
              source: 'web',
              source_channel: 'local_agent',
              session_kind: 'local_agent_channel',
              status: 'active',
              created_at: '2026-06-23T08:34:00Z',
              updated_at: '2026-06-23T08:34:00Z',
              last_message_at: '2026-06-23T08:35:00Z',
            }),
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

    expect(markup).toContain('Codex on Mac');
    expect(markup).toContain('Local');
    expect(markup).toContain('tabler-icon-device-desktop');
    expect(markup).toContain('data-testid="sidebar-agent-sessions-local-agent-1"');
    expect(markup).toContain('Codex local debug');
    expect(markup).toContain('href="/agents/local-agent-1?session_id=chat-session-1#chat"');
    expect(markup).toContain('class="sidebar-session-item active"');
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
