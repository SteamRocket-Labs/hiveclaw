import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import AppSidebar, { isCompactSidebarViewport } from './AppSidebar';
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

vi.mock('../../api/domains/assistant', () => ({
  assistantApi: { get: vi.fn().mockResolvedValue({ agent: null, candidates: [] }) },
}));

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, enabled: false } },
});

function renderSidebar(element: React.ReactElement) {
  return renderToStaticMarkup(<QueryClientProvider client={queryClient}>{element}</QueryClientProvider>);
}

function cssBlock(css: string, selector: string) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escapedSelector}\\s*\\{([\\s\\S]*?)\\}`));
  return match?.[1] ?? '';
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

describe('Layout extracted sections', () => {
  it('reserves the narrow viewport for the workbench by default-collapsing the app sidebar', () => {
    expect(isCompactSidebarViewport(900)).toBe(true);
    expect(isCompactSidebarViewport(901)).toBe(false);
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

  it('renders the sidebar as the bot list with My Assistant leading it (B3)', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        agents={[
          { id: 'agent-1', name: 'Agent One', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' },
          { id: 'agent-2', name: 'Agent Two', created_at: '2026-03-26T00:00:00Z', status: 'idle', agent_type: 'native' },
          { id: 'agent-5', name: 'Codex on Mac', created_at: '2026-03-23T00:00:00Z', status: 'idle', agent_type: 'local_agent' },
        ]}
      />,
    );

    expect(markup).toContain('HiveClaw');
    expect(markup).toContain('Workspace');
    expect(markup).toContain('Company A');
    // One entry per bot, no per-bot action buttons, no session rows. B5:
    // opening a bot opens its conversation (#chat); local-runtime bots keep
    // the management landing because their channel has no chat sessions.
    expect(markup).toContain('href="/agents/agent-1#chat"');
    expect(markup).toContain('href="/agents/agent-2#chat"');
    expect(markup).toContain('href="/agents/agent-5"');
    expect(markup).toContain('Codex on Mac');
    expect(markup).toContain('Local');
    expect(markup).toContain('New digital employee');
    expect(markup).not.toContain('aria-label="New conversation with');
    expect(markup).not.toContain('aria-label="Expand sessions"');
    expect(markup).not.toContain('sidebar-session-row');
    expect(markup).not.toContain('sidebar-agent-disclosure');
    // The removed top-level navigation entries are gone; the routes stay.
    expect(markup).not.toContain('href="/home"');
    expect(markup).not.toContain('href="/plaza"');
    expect(markup).not.toContain('href="/automations"');
    expect(markup).not.toContain('href="/projects"');
    expect(markup).not.toContain('sidebar-top-actions');
  });

  it('keeps employee creation as a simple action below the bot list', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        agents={[{ id: 'agent-1', name: 'Research Lead', role_description: 'Market research', created_at: '2026-03-27T00:00:00Z', status: 'running', agent_type: 'native' }]}
      />,
    );

    expect(markup).not.toContain('Workspace search');
    expect(markup).toContain('Research Lead');
    expect(markup).toContain('New digital employee');
    expect(markup).toContain('data-testid="sidebar-create-agent-block"');
    expect(markup).toContain('class="sidebar-item sidebar-create-agent-link');
  });

  it('moves knowledge and the local bridge behind the Settings menu', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        showAccountMenu
        agents={[]}
      />,
    );

    expect(markup).toContain('Settings');
    expect(markup).toContain('href="/knowledge"');
    expect(markup).toContain('href="/local-agents"');
    // The company admin entry lands on Members & Login, not the dashboard.
    expect(markup).toContain('href="/enterprise/users"');
    expect(markup).not.toContain('href="/enterprise/dashboard"');
  });

  it('keeps the user identity hidden until Settings is opened', () => {
    routeState.location = { pathname: '/agents/agent-1', search: '', hash: '' };
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        agents={[]}
      />,
    );

    expect(markup).toContain('Settings');
    expect(markup).not.toContain('Example Owner');
    expect(markup).not.toContain('Super Admin');
  });

  it('never offers a disabled company in the workspace selector or as the current workspace', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        tenants={[
          { id: 'tenant-1', name: 'Company A' },
          { id: 'tenant-2', name: 'Disabled Holdings', is_active: false },
        ]}
        currentTenant="tenant-2"
        agents={[]}
      />,
    );

    // The previously selected company is disabled: it is not a valid selection,
    // so the selector opens on the placeholder and never lists it.
    expect(markup).toContain('sidebar-workspace-select');
    expect(markup).toContain('Company A');
    expect(markup).not.toContain('Disabled Holdings');
    expect(markup).toContain('Select a company…');
  });

  it('says plainly when no active company remains instead of showing a disabled workspace', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        tenants={[{ id: 'tenant-2', name: 'Disabled Holdings', is_active: false }]}
        currentTenant="tenant-2"
        showAccountMenu
        agents={[]}
      />,
    );

    expect(markup).toContain('No active company available');
    expect(markup).not.toContain('Disabled Holdings');
    expect(markup).not.toContain('sidebar-workspace-select');
    // The platform-control path (Settings → Platform Settings) stays reachable.
    expect(markup).toContain('Platform Settings');
  });

  it('says no active company after a successful empty company inventory, not a selection placeholder', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        tenants={[]}
        companiesLoaded
        currentTenant=""
        showAccountMenu
        agents={[]}
      />,
    );

    // The inventory answered successfully with zero companies: a truthful
    // empty state, not a pending-selection prompt and no dead selector.
    expect(markup).toContain('No active company available');
    expect(markup).not.toContain('No company selected');
    expect(markup).not.toContain('sidebar-workspace-select');
    expect(markup).toContain('Platform Settings');
  });

  it('does not claim no active company before the company inventory answer arrives', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'platform_admin', display_name: 'Example Owner' }}
        tenants={[]}
        currentTenant=""
        agents={[]}
      />,
    );

    // No answer yet (or a failed request): the selection placeholder stays;
    // the empty-inventory sentence must not appear on speculation.
    expect(markup).toContain('No company selected');
    expect(markup).not.toContain('No active company available');
  });

  it('labels the language action with its destination when the current locale is Chinese', () => {
    const markup = renderSidebar(<AppSidebar
        {...baseProps}
        user={{ id: 'user-1', role: 'member', display_name: 'Example Owner' }}
        isChinese
        showAccountMenu
        agents={[]}
      />,
    );

    expect(markup).toContain('English');
    expect(markup).not.toContain('>中文<');
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

  it('keeps the collapsed Settings menu visible as a fixed flyout beside the rail', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const css = readFileSync(new URL('../../index.css', import.meta.url), 'utf8');

    // No collapsed display:none rule may swallow the Settings dropdown.
    const collapsedHideRules = css.match(/\.sidebar\.collapsed[^{]*\{[^}]*display:\s*none[^}]*\}/g) || [];
    expect(collapsedHideRules.length).toBeGreaterThan(0);
    for (const rule of collapsedHideRules) {
      expect(rule).not.toContain('account-dropdown');
    }
    // The collapsed flyout pins the menu beside the 68px rail so the sidebar's
    // overflow clip cannot hide it.
    const flyout = cssBlock(css, '.sidebar.collapsed .sidebar-settings-dropdown');
    expect(flyout).toContain('position: fixed;');
    expect(flyout).toContain('left: calc(var(--sidebar-width-collapsed)');
    expect(flyout).toContain('width: 232px;');
  });
});
