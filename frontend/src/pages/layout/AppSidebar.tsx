import type { ReactNode, RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconArrowUpRight,
  IconBell,
  IconBrain,
  IconBuilding,
  IconChartBar,
  IconCheckbox,
  IconChevronUp,
  IconChevronsLeft,
  IconChevronsRight,
  IconDatabase,
  IconDeviceDesktop,
  IconFileText,
  IconHome,
  IconLogout,
  IconMoon,
  IconPin,
  IconPinnedOff,
  IconPlugConnected,
  IconPlus,
  IconRefresh,
  IconRoute,
  IconSearch,
  IconSettings,
  IconShieldCheck,
  IconSitemap,
  IconSun,
  IconUser,
  IconUsers,
  IconWorld,
  IconX,
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
  { to: '/agents', labelKey: 'nav.digitalEmployees', fallback: 'Digital Employees', icon: <IconUsers size={15} stroke={1.6} />, end: true },
  { to: '/messages', labelKey: 'nav.conversationsTasks', fallback: 'Conversations & Tasks', icon: <IconCheckbox size={15} stroke={1.6} /> },
  { to: '/plans', labelKey: 'nav.planReview', fallback: 'Plan Review', icon: <IconShieldCheck size={15} stroke={1.6} /> },
  { to: '/automations', labelKey: 'nav.automations', fallback: 'Automations', icon: <IconRefresh size={15} stroke={1.6} /> },
  { to: '/memory', labelKey: 'nav.memoryKnowledge', fallback: 'Memory & Knowledge', icon: <IconBrain size={15} stroke={1.6} /> },
  { to: '/documents', labelKey: 'nav.documentsResearch', fallback: 'Documents & Research', icon: <IconFileText size={15} stroke={1.6} /> },
  { to: '/team', labelKey: 'nav.a2aTeam', fallback: 'A2A / Team', icon: <IconRoute size={15} stroke={1.6} /> },
  { to: '/local-agents', labelKey: 'nav.localAgentChannel', fallback: 'Local Agent Channel', icon: <IconDeviceDesktop size={15} stroke={1.6} /> },
  { to: '/plaza', labelKey: 'nav.plaza', fallback: 'Agent Circle', icon: <IconSitemap size={15} stroke={1.6} /> },
];

const controlPlaneNavItems: SidebarNavItem[] = [
  { to: '/enterprise/dashboard', labelKey: 'nav.controlOverview', fallback: 'Overview', icon: <IconChartBar size={15} stroke={1.6} /> },
  { to: '/enterprise/hr', labelKey: 'nav.agentGovernance', fallback: 'Agent Governance', icon: <IconUsers size={15} stroke={1.6} /> },
  { to: '/enterprise/llm', labelKey: 'nav.modelsBudget', fallback: 'Models & Budget', icon: <IconDatabase size={15} stroke={1.6} /> },
  { to: '/enterprise/tools', labelKey: 'nav.capabilitiesTools', fallback: 'Capabilities & Tools', icon: <IconPlugConnected size={15} stroke={1.6} /> },
  { to: '/enterprise/subagents', labelKey: 'nav.teamDelegation', fallback: 'Team & Delegation', icon: <IconRoute size={15} stroke={1.6} /> },
  { to: '/enterprise/memory', labelKey: 'nav.memoryGovernance', fallback: 'Memory Governance', icon: <IconBrain size={15} stroke={1.6} /> },
  { to: '/enterprise/info', labelKey: 'nav.channelsIntegrations', fallback: 'Channels & Integrations', icon: <IconBuilding size={15} stroke={1.6} /> },
  { to: '/enterprise/approvals', labelKey: 'nav.approvalCenter', fallback: 'Approval Center', icon: <IconShieldCheck size={15} stroke={1.6} /> },
  { to: '/enterprise/audit', labelKey: 'nav.auditLog', fallback: 'Audit Log', icon: <IconFileText size={15} stroke={1.6} /> },
  { to: '/enterprise/skills', labelKey: 'nav.assetsAutomation', fallback: 'Assets & Automation', icon: <IconRefresh size={15} stroke={1.6} /> },
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
  const query = sidebarSearch.trim().toLowerCase();
  const sortedAgents = [...agents]
    .filter(
      (agent) =>
        !query ||
        (agent.name || '').toLowerCase().includes(query) ||
        (agent.role_description || '').toLowerCase().includes(query),
    )
    .sort((a, b) => {
      const aPinned = pinnedAgents.has(a.id) ? 1 : 0;
      const bPinned = pinnedAgents.has(b.id) ? 1 : 0;
      if (aPinned !== bPinned) return bPinned - aPinned;
      const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
      const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
      return bTime - aTime;
    });

  const canSeeControlPlane = user && ['platform_admin', 'org_admin'].includes(user.role);
  const activeTenantName = tenants.find((tenant) => tenant.id === currentTenant)?.name || t('layout.myCompany', 'My Company');
  const searchableRouteItems = [
    ...workspaceNavItems,
    ...(canSeeControlPlane ? controlPlaneNavItems : []),
  ];
  const quickOpenResults = query
    ? searchableRouteItems.filter((item) => {
      const label = t(item.labelKey, item.fallback).toLowerCase();
      return label.includes(query) || item.to.toLowerCase().includes(query);
    }).slice(0, 6)
    : [];

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
          <div className="sidebar-section-title">{t('nav.myWorkspace', 'My Workspace')}</div>
          {workspaceNavItems.map((item) => (
            <SidebarNavLink key={item.to} item={item} />
          ))}
        </div>

        <div className="sidebar-divider" />

        <div className="sidebar-section sidebar-nav-section">
          <div className="sidebar-section-title">{t('nav.workspaceSearch', 'Workspace search')}</div>
          {!isSidebarCollapsed && (
            <div className="sidebar-search-wrap">
              <IconSearch size={14} stroke={2} className="sidebar-search-icon" />
              <input
                type="text"
                value={sidebarSearch}
                onChange={(event) => onSetSidebarSearch(event.target.value)}
                placeholder={isChinese ? '搜索工作区、员工、页面...' : 'Search workspace, employees, routes...'}
                className="sidebar-search-input"
              />
              {sidebarSearch && (
                <button onClick={() => onSetSidebarSearch('')} className="sidebar-search-clear" title={isChinese ? '清除' : 'Clear'}>
                  <IconX size={14} stroke={2} />
                </button>
              )}
            </div>
          )}
          {!isSidebarCollapsed && quickOpenResults.length > 0 && (
            <div className="sidebar-quick-open">
              <div className="sidebar-quick-open-title">{t('nav.quickOpen', 'Quick open')}</div>
              {quickOpenResults.map((item) => (
                <SidebarNavLink key={`quick-${item.to}`} item={item} />
              ))}
            </div>
          )}
          {!isSidebarCollapsed && (
            <div className="sidebar-section-subtitle">{t('nav.digitalEmployees', 'Digital Employees')}</div>
          )}
          {sortedAgents.map((agent) => {
            const badge = getAgentBadgeStatus(agent);
            const avatarChar = ((Array.from(agent.name || '?')[0] as string) || '?').toUpperCase();
            return (
              <div key={agent.id} className={`sidebar-agent-item${agent.creator_id === user?.id ? ' owned' : ''}`}>
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
                </NavLink>
                {!isSidebarCollapsed && (
                  <button
                    onClick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      onTogglePin(agent.id);
                    }}
                    className={`sidebar-pin-btn ${pinnedAgents.has(agent.id) ? 'pinned' : ''}`}
                    title={pinnedAgents.has(agent.id) ? (isChinese ? '取消置顶' : 'Unpin') : (isChinese ? '置顶' : 'Pin to top')}
                  >
                    {pinnedAgents.has(agent.id) ? (
                      <>
                        <IconPin size={14} stroke={1.5} className="pin-default" />
                        <IconPinnedOff size={14} stroke={1.5} className="pin-hover" />
                      </>
                    ) : (
                      <IconPin size={14} stroke={1.5} className="pin-on" />
                    )}
                  </button>
                )}
              </div>
            );
          })}
          {agents.length === 0 && (
            <div className="sidebar-empty-note">{t('nav.noEmployees', 'No digital employees yet')}</div>
          )}
          {agents.length > 0 && sortedAgents.length === 0 && query && (
            <div className="sidebar-empty-note">{isChinese ? '无匹配结果' : 'No matches'}</div>
          )}
        </div>

        {canSeeControlPlane && (
          <>
            <div className="sidebar-divider" />
            <div className="sidebar-section sidebar-nav-section">
              <div className="sidebar-section-title">{t('nav.controlPlane', 'Control Plane')}</div>
              {controlPlaneNavItems.map((item) => (
                <SidebarNavLink key={item.to} item={item} />
              ))}
            </div>
          </>
        )}
      </div>

      <div className="sidebar-bottom">
        <div className="sidebar-section sidebar-nav-section sidebar-primary-actions">
          {user && (
            <NavLink to="/agents/new" className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`} title={t('nav.newAgent', 'New Digital Employee')}>
              <span className="sidebar-item-icon sidebar-item-icon-centered">{sidebarIcons.plus}</span>
              <span className="sidebar-item-text">{t('nav.newAgent', 'New Digital Employee')}</span>
            </NavLink>
          )}
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
