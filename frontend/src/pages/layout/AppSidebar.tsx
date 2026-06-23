import type { ReactNode, RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconArrowUpRight,
  IconBell,
  IconBuilding,
  IconCheckbox,
  IconChevronUp,
  IconChevronsLeft,
  IconChevronsRight,
  IconDeviceDesktop,
  IconHome,
  IconLogout,
  IconMoon,
  IconPlus,
  IconRefresh,
  IconSettings,
  IconSitemap,
  IconSun,
  IconUser,
  IconWorld,
} from '@tabler/icons-react';

const sidebarIcons = {
  plus: <IconPlus size={16} stroke={1.5} />,
  user: <IconUser size={16} stroke={1.5} />,
  sun: <IconSun size={16} stroke={1.5} />,
  moon: <IconMoon size={16} stroke={1.5} />,
  logout: <IconLogout size={16} stroke={1.5} />,
  globe: <IconWorld size={16} stroke={1.5} />,
  collapse: <IconChevronsLeft size={16} stroke={1.5} />,
  expand: <IconChevronsRight size={16} stroke={1.5} />,
  bell: <IconBell size={16} stroke={1.5} />,
};

type SidebarNavItem = {
  to: string;
  labelKey: string;
  fallback: string;
  icon: ReactNode;
  end?: boolean;
};

const workspaceNavItems: SidebarNavItem[] = [
  { to: '/home', labelKey: 'nav.home', fallback: 'Home', icon: <IconHome size={15} stroke={1.6} />, end: true },
  { to: '/plaza', labelKey: 'nav.plaza', fallback: 'Agent Circle', icon: <IconSitemap size={15} stroke={1.6} /> },
  { to: '/automations', labelKey: 'nav.tasksAutomation', fallback: 'Tasks / Automation', icon: <IconCheckbox size={15} stroke={1.6} /> },
  { to: '/local-agents', labelKey: 'nav.bridge', fallback: 'Bridge', icon: <IconDeviceDesktop size={15} stroke={1.6} /> },
];

const getAgentBadgeStatus = (agent: any): string | null => {
  if (agent.status === 'error') return 'error';
  if (agent.status === 'creating') return 'creating';
  if (agent.agent_type === 'openclaw' && agent.status === 'running' && agent.openclaw_last_seen) {
    const elapsed = Date.now() - new Date(agent.openclaw_last_seen).getTime();
    if (elapsed > 60 * 60 * 1000) return 'disconnected';
  }
  return null;
};

const getRoleLabel = (role: string | undefined, t: any) => {
  if (role === 'platform_admin') return t('roles.platformAdmin');
  if (role === 'org_admin') return t('roles.orgAdmin');
  return t('roles.member');
};

const getAgentSourceBadge = (agent: any, user: any, t: any): string | null => {
  if (agent.agent_type === 'openclaw') return t('nav.localBadge', 'Local');
  if (agent.creator_id && user?.id && agent.creator_id !== user.id) return t('nav.publicBadge', 'Public');
  if (agent.visibility_scope === 'public' || agent.is_public) return t('nav.publicBadge', 'Public');
  return null;
};

function SidebarNavLink({ item }: { item: SidebarNavItem }) {
  const { t } = useTranslation();
  return (
    <NavLink
      to={item.to}
      end={item.end}
      className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
      title={t(item.labelKey, item.fallback)}
    >
      <span className="sidebar-item-icon sidebar-item-icon-centered">{item.icon}</span>
      <span className="sidebar-item-text">{t(item.labelKey, item.fallback)}</span>
    </NavLink>
  );
}

interface AppSidebarProps {
  user: any;
  theme: 'dark' | 'light';
  isSidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  agents: any[];
  pinnedAgents: Set<string>;
  onTogglePin: (agentId: string) => void;
  tenants: { id: string; name: string }[];
  currentTenant: string;
  onSwitchTenant: (tenantId: string) => void;
  isChinese: boolean;
  sidebarSearch: string;
  onSetSidebarSearch: (value: string) => void;
  onToggleTheme: () => void;
  onOpenNotifications: () => void;
  unreadCount: number;
  accountMenuRef: RefObject<HTMLDivElement | null>;
  showAccountMenu: boolean;
  onToggleAccountMenu: () => void;
  onToggleLang: () => void;
  onOpenAccountSettings: () => void;
  onLogout: () => void;
  versionDisplay: ReactNode;
}

export default function AppSidebar({
  user,
  theme,
  isSidebarCollapsed,
  onToggleSidebar,
  agents,
  pinnedAgents,
  onTogglePin,
  tenants,
  currentTenant,
  onSwitchTenant,
  isChinese,
  sidebarSearch,
  onSetSidebarSearch,
  onToggleTheme,
  onOpenNotifications,
  unreadCount,
  accountMenuRef,
  showAccountMenu,
  onToggleAccountMenu,
  onToggleLang,
  onOpenAccountSettings,
  onLogout,
  versionDisplay,
}: AppSidebarProps) {
  const { t, i18n } = useTranslation();
  const sortedAgents = [...agents]
    .sort((a, b) => {
      const aTime = a.last_active_at ? new Date(a.last_active_at).getTime() : (a.created_at ? new Date(a.created_at).getTime() : 0);
      const bTime = b.last_active_at ? new Date(b.last_active_at).getTime() : (b.created_at ? new Date(b.created_at).getTime() : 0);
      return bTime - aTime;
    });

  const canSeeControlPlane = user && ['platform_admin', 'org_admin'].includes(user.role);
  const activeTenantName = tenants.find((tenant) => tenant.id === currentTenant)?.name || t('layout.myCompany', 'My Company');

  return (
    <nav className={`sidebar ${isSidebarCollapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-top">
        <div className="sidebar-logo">
          <span className="sidebar-logo-text">HiveClaw</span>
          <button
            className="btn btn-ghost sidebar-collapse-btn"
            onClick={onToggleSidebar}
            title={isSidebarCollapsed ? 'Expand Sidebar' : 'Collapse Sidebar'}
          >
            {isSidebarCollapsed ? sidebarIcons.expand : sidebarIcons.collapse}
          </button>
        </div>
        {!isSidebarCollapsed && (
          <div className="sidebar-workspace-card">
            <span className="sidebar-workspace-eyebrow">{t('nav.currentWorkspace', 'Workspace')}</span>
            <span className="sidebar-workspace-name">{activeTenantName}</span>
          </div>
        )}
      </div>

      <div className="sidebar-scrollable">
        <div className="sidebar-section sidebar-nav-section">
          <div className="sidebar-section-title">{t('nav.topActions', 'My Workspace')}</div>
          {workspaceNavItems.map((item) => (
            <SidebarNavLink key={item.to} item={item} />
          ))}
        </div>

        <div className="sidebar-divider" />

        <div className="sidebar-section sidebar-nav-section">
          <NavLink to="/agents" className="sidebar-section-title sidebar-section-title-link">
            {t('nav.digitalEmployees', 'Digital Employees')}
          </NavLink>
          {sortedAgents.map((agent) => {
            const badge = getAgentBadgeStatus(agent);
            const sourceBadge = getAgentSourceBadge(agent, user, t);
            const avatarChar = ((Array.from(agent.name || '?')[0] as string) || '?').toUpperCase();
            return (
              <div key={agent.id} className="sidebar-agent-item">
                <NavLink to={`/agents/${agent.id}`} className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`} title={agent.name}>
                  <span className="sidebar-item-icon" style={{ position: 'relative' }}>
                    <span className={`agent-avatar${agent.agent_type === 'openclaw' ? ' openclaw' : ''}`}>{avatarChar}</span>
                    {agent.agent_type === 'openclaw' && (
                      <span className="agent-avatar-link" style={{ display: 'flex' }}>
                        <IconArrowUpRight size={10} stroke={2.5} />
                      </span>
                    )}
                    {badge && <span className={`agent-avatar-badge ${badge}`} />}
                  </span>
                  <span className="sidebar-item-text">{agent.name}</span>
                  {sourceBadge && !isSidebarCollapsed && <span className="sidebar-agent-source-badge">{sourceBadge}</span>}
                </NavLink>
              </div>
            );
          })}
          {agents.length === 0 && (
            <div className="sidebar-empty-note">{t('nav.noEmployees', 'No digital employees yet')}</div>
          )}
          {user && (
            <NavLink to="/agents/new" className={({ isActive }) => `sidebar-item sidebar-create-employee ${isActive ? 'active' : ''}`} title={t('nav.newAgent', 'New Digital Employee')}>
              <span className="sidebar-item-icon sidebar-item-icon-centered">{sidebarIcons.plus}</span>
              <span className="sidebar-item-text">{t('nav.newAgent', 'New Digital Employee')}</span>
            </NavLink>
          )}
        </div>
      </div>

      <div className="sidebar-bottom">
        <div className="sidebar-section sidebar-nav-section sidebar-primary-actions">
          {user && ['platform_admin', 'org_admin'].includes(user.role) && (
            <NavLink to="/enterprise/dashboard" className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`} title={t('nav.enterprise', 'Company Admin')}>
              <span className="sidebar-item-icon sidebar-item-icon-centered">
                <IconBuilding size={16} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">{t('nav.enterprise', 'Company Admin')}</span>
            </NavLink>
          )}
          {user && user.role === 'platform_admin' && (
            <NavLink
              to="/admin/platform-settings"
              className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
              title={t('nav.platformSettings', 'Platform Settings')}
            >
              <span className="sidebar-item-icon sidebar-item-icon-centered">
                <IconSettings size={16} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">{t('nav.platformSettings', 'Platform Settings')}</span>
            </NavLink>
          )}
        </div>

        <div className="sidebar-footer">
          <div className="sidebar-footer-controls">
            <button className="btn btn-ghost" onClick={onToggleTheme} title={theme === 'dark' ? 'Light Mode' : 'Dark Mode'}>
              {theme === 'dark' ? sidebarIcons.sun : sidebarIcons.moon}
            </button>
            <button className="btn btn-ghost" onClick={onOpenNotifications} title={isChinese ? '通知' : 'Notifications'}>
              {sidebarIcons.bell}
              {unreadCount > 0 && <span className="sidebar-unread-badge">{unreadCount > 99 ? '99+' : unreadCount}</span>}
            </button>
          </div>

          {user?.role === 'platform_admin' && tenants.length > 1 && !isSidebarCollapsed && (
            <select
              value={currentTenant}
              onChange={e => onSwitchTenant(e.target.value)}
              aria-label={t('layout.switchTenant', 'Switch company')}
              className="tenant-switcher"
            >
              {tenants.map(tn => (
                <option key={tn.id} value={tn.id}>{tn.name}</option>
              ))}
            </select>
          )}

          <div ref={accountMenuRef} style={{ position: 'relative' }}>
            {showAccountMenu && (
              <div className="account-dropdown">
                <button className="account-dropdown-item" onClick={onToggleLang}>
                  <IconWorld size={15} stroke={1.5} />
                  <span>{i18n.language === 'zh' ? 'English' : '中文'}</span>
                </button>
                <button className="account-dropdown-item" onClick={onOpenAccountSettings}>
                  <IconUser size={15} stroke={1.5} />
                  <span>{isChinese ? '账户设置' : 'Account Settings'}</span>
                </button>
                <div style={{ height: '1px', background: 'var(--border-subtle)', margin: '4px 0' }} />
                <button className="account-dropdown-item account-dropdown-danger" onClick={onLogout}>
                  <IconLogout size={15} stroke={1.5} />
                  <span>{t('layout.logout', 'Logout')}</span>
                </button>
              </div>
            )}
            <div className="sidebar-account-row" onClick={onToggleAccountMenu}>
              <div className="sidebar-account-avatar">{sidebarIcons.user}</div>
              <div className="sidebar-footer-user-info">
                <div className="sidebar-account-name">{user?.display_name}</div>
                <div className="sidebar-account-role">{getRoleLabel(user?.role, t)}</div>
              </div>
              <IconChevronUp
                size={14}
                stroke={1.5}
                className="sidebar-account-chevron"
                style={{ transform: showAccountMenu ? 'rotate(0deg)' : 'rotate(180deg)' }}
              />
            </div>
          </div>

          {versionDisplay}
        </div>
      </div>
    </nav>
  );
}
