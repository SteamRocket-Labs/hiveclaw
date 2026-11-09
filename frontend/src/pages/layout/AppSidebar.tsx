import { useEffect, useRef, type ReactNode, type RefObject } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconBell,
  IconBuilding,
  IconChevronUp,
  IconChevronsLeft,
  IconChevronsRight,
  IconDatabase,
  IconDeviceDesktop,
  IconLogout,
  IconMoon,
  IconPlus,
  IconSettings,
  IconSun,
  IconUser,
  IconWorld,
} from '@tabler/icons-react';
import { type HrAgentInfo } from '../../api/domains/agents';
import { isActiveCompany } from '../../api/domains/admin';
import { isManagedEmployeeAgent } from '../../roles';
import { assistantNameForDisplay, sidebarAgentLink, sortSidebarAgents, useAssistantAgentId } from '../assistant/assistantScope';

const sidebarIcons = {
  user: <IconUser size={16} stroke={1.5} />,
  collapse: <IconChevronsLeft size={16} stroke={1.5} />,
  expand: <IconChevronsRight size={16} stroke={1.5} />,
};

const isLocalAgentRuntimeType = (agent: any): boolean => agent?.agent_type === 'local_agent';

const getAgentBadgeStatus = (agent: any): string | null => {
  if (agent.status === 'error') return 'error';
  if (agent.status === 'creating') return 'creating';
  return null;
};

const getAgentSourceBadge = (agent: any, t: any, user?: any): string | null => {
  if (isLocalAgentRuntimeType(agent)) return t('nav.localBadge', 'Local');
  if (agent.is_owner === false) {
    // PDEC-013: inside an administrator's company inventory a non-owned Agent
    // is a managed employee Agent, not automatically a public one.
    if (isManagedEmployeeAgent(user, agent)) return t('nav.managedBadge', 'Managed');
    return t('nav.publicBadge', 'Public');
  }
  if (agent.visibility_scope === 'public' || agent.is_public) return t('nav.publicBadge', 'Public');
  return null;
};

const getAccountRoleLabel = (user: any, t: any): string => {
  if (user?.role === 'platform_admin') return t('nav.superAdmin', 'Super Admin');
  if (user?.role === 'org_admin') return t('nav.orgAdmin', 'Company Admin');
  return t('nav.member', 'Member');
};

interface AppSidebarProps {
  user: any;
  theme: 'dark' | 'light';
  isSidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  agents: any[];
  tenants: { id: string; name: string; is_active?: boolean }[];
  companiesLoaded?: boolean;
  currentTenant: string;
  onSwitchTenant: (tenantId: string) => void;
  isChinese: boolean;
  onToggleTheme: () => void;
  onOpenNotifications: () => void;
  unreadCount: number;
  accountMenuRef: RefObject<HTMLDivElement | null>;
  showAccountMenu: boolean;
  onToggleAccountMenu: () => void;
  onCloseAccountMenu?: () => void;
  onToggleLang: () => void;
  onOpenAccountSettings: () => void;
  onLogout: () => void;
  versionDisplay: ReactNode;
  hrAgent?: HrAgentInfo | null;
}

export const COMPACT_SIDEBAR_MAX_WIDTH = 900;

export function isCompactSidebarViewport(width: number): boolean {
  return Number.isFinite(width) && width <= COMPACT_SIDEBAR_MAX_WIDTH;
}

const ACTIVE_AGENT_RE = /^\/agents\/([^/?#]+)/;

function getActiveAgentId(pathname: string): string | null {
  const match = pathname.match(ACTIVE_AGENT_RE);
  const value = match?.[1];
  if (!value || value === 'new') return null;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export default function AppSidebar({
  user,
  theme,
  isSidebarCollapsed,
  onToggleSidebar,
  agents,
  tenants,
  companiesLoaded,
  currentTenant,
  onSwitchTenant,
  isChinese,
  onToggleTheme,
  onOpenNotifications,
  unreadCount,
  accountMenuRef,
  showAccountMenu,
  onToggleAccountMenu,
  onCloseAccountMenu,
  onToggleLang,
  onOpenAccountSettings,
  onLogout,
  versionDisplay,
}: AppSidebarProps) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const activeAgentId = getActiveAgentId(location.pathname);

  // The bot is the way in (B3): My Assistant's own binding decides which bot
  // leads the list. The read shares the assistant cache the entry route uses,
  // so it adds no request once that page has loaded.
  const assistantAgentId = useAssistantAgentId(user, currentTenant || (user?.role === 'platform_admin' ? '' : user?.tenant_id) || '');

  // Creation uses an ordinary conversation; the legacy HR system bot stays
  // outside the member's bot list.
  const sortedAgents = sortSidebarAgents(
    agents.filter((agent) => agent.agent_class !== 'internal_system' || agent.name !== '__system_hr__'), assistantAgentId,
  );

  const canSeeControlPlane = user && ['platform_admin', 'org_admin'].includes(user.role);
  // Only active companies may ever be selected or shown as the current
  // workspace; a disabled selected company is simply not a valid selection.
  const activeTenants = tenants.filter((tenant) => isActiveCompany(tenant));
  const hasSelectedCompany = Boolean(currentTenant) && activeTenants.some((tenant) => tenant.id === currentTenant);
  const activeTenantName = activeTenants.find((tenant) => tenant.id === currentTenant)?.name
    // A platform admin with no company selected keeps that honest state: it
    // wins over the admin's own tenant_name (B2 r2). tenant_name is the label
    // only for the member's own company (members never load the inventory).
    || (user?.role === 'platform_admin' && !currentTenant
      ? t('layout.noCompanySelected', 'No company selected')
      : (user as { tenant_name?: string } | null)?.tenant_name
        || t('layout.myCompany', 'My Company'));

  const settingsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const onCloseAccountMenuRef = useRef(onCloseAccountMenu);
  useEffect(() => {
    onCloseAccountMenuRef.current = onCloseAccountMenu;
  });
  const accountMenuLocationRef = useRef<string | null>(null);
  const accountMenuLocationKey = `${location.pathname}${location.search}${location.hash}`;

  // Opening the Settings menu moves focus to its first action so keyboard users
  // Tab straight through it; Escape closes and returns focus to the trigger.
  useEffect(() => {
    if (!showAccountMenu || !onCloseAccountMenuRef.current) return;
    const firstItem = accountMenuRef.current?.querySelector<HTMLElement>('.account-dropdown-item');
    firstItem?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      onCloseAccountMenuRef.current?.();
      settingsTriggerRef.current?.focus();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [showAccountMenu, accountMenuRef]);

  // A route change (including browser history navigation, which carries no
  // outside mousedown) closes the menu instead of leaving it over a new page.
  useEffect(() => {
    if (accountMenuLocationRef.current === null) {
      accountMenuLocationRef.current = accountMenuLocationKey;
      return;
    }
    if (accountMenuLocationRef.current === accountMenuLocationKey) return;
    accountMenuLocationRef.current = accountMenuLocationKey;
    if (showAccountMenu) onCloseAccountMenuRef.current?.();
  }, [accountMenuLocationKey, showAccountMenu]);

  return (
    <nav className={`sidebar ${isSidebarCollapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-top">
        <div className="sidebar-logo">
          <span className="sidebar-logo-text">HiveClaw</span>
          <button
            className="btn btn-ghost sidebar-collapse-btn"
            onClick={onToggleSidebar}
            title={isSidebarCollapsed ? t('layout.expandSidebar', 'Expand Sidebar') : t('layout.collapseSidebar', 'Collapse Sidebar')}
          >
            {isSidebarCollapsed ? sidebarIcons.expand : sidebarIcons.collapse}
          </button>
        </div>
        {!isSidebarCollapsed && (
          <div className="sidebar-workspace-card">
            <span className="sidebar-workspace-eyebrow">{t('nav.currentWorkspace', 'Workspace')}</span>
            {user?.role === 'platform_admin' && activeTenants.length === 0 && (tenants.length > 0 || companiesLoaded) ? (
              // The inventory answered and nothing is active — zero companies
              // returned, or every known company disabled: say so plainly. The
              // Settings menu's Platform Settings entry stays reachable for
              // reactivation.
              <span className="sidebar-workspace-name">{t('layout.noActiveCompany', 'No active company available')}</span>
            ) : user?.role === 'platform_admin' && (activeTenants.length > 1 || !hasSelectedCompany) && activeTenants.length > 0 ? (
              <select
                value={hasSelectedCompany ? currentTenant : ''}
                onChange={e => onSwitchTenant(e.target.value)}
                aria-label={t('layout.switchTenant', 'Switch company')}
                className="sidebar-workspace-select"
              >
                {!hasSelectedCompany && (
                  <option value="" disabled>{t('layout.selectCompanyPlaceholder', 'Select a company…')}</option>
                )}
                {activeTenants.map(tn => (
                  <option key={tn.id} value={tn.id}>{tn.name}</option>
                ))}
              </select>
            ) : (
              <span className="sidebar-workspace-name">{activeTenantName}</span>
            )}
          </div>
        )}
      </div>

      <div className="sidebar-scrollable">
        <div className="sidebar-section sidebar-nav-section">
          {sortedAgents.map((agent) => {
            const badge = getAgentBadgeStatus(agent);
            const sourceBadge = getAgentSourceBadge(agent, t, user);
            const name = String(agent.id) === assistantAgentId ? t('assistant.title') : assistantNameForDisplay(agent.name, t('assistant.title', 'My Assistant'));
            const avatarChar = ((Array.from(name || '?')[0] as string) || '?').toUpperCase();
            const isAgentRowActive = String(activeAgentId || '') === String(agent.id);
            // B5: opening a bot opens its conversation (#chat is the workbench),
            // or a fresh one when it has none. Operator-only bots keep their
            // managed entry; local-runtime bots keep the management landing
            // (their channel does not use chat sessions).
            const botEntry = sidebarAgentLink(agent);
            return (
              <NavLink
                key={agent.id}
                to={botEntry}
                className={() => `sidebar-item sidebar-agent-link ${isAgentRowActive ? 'active' : ''}`}
                title={name}
                aria-label={t('layout.openNamed', 'Open {{name}}', { name })}
              >
                <span className="sidebar-item-icon" style={{ position: 'relative' }}>
                  <span className={`agent-avatar${isLocalAgentRuntimeType(agent) ? ' local-runtime' : ''}`}>{avatarChar}</span>
                  {isLocalAgentRuntimeType(agent) && (
                    <span className="agent-avatar-link" style={{ display: 'flex' }}>
                      <IconDeviceDesktop size={10} stroke={2.5} />
                    </span>
                  )}
                  {badge && <span className={`agent-avatar-badge ${badge}`} />}
                </span>
                <span className="sidebar-item-text">{name}</span>
                {sourceBadge && !isSidebarCollapsed && <span className="sidebar-agent-source-badge">{sourceBadge}</span>}
              </NavLink>
            );
          })}
          {agents.length === 0 && (
            <div className="sidebar-empty-note">{t('nav.noEmployees', 'No digital employees yet')}</div>
          )}
          {user && (
            <div className="sidebar-create-agent-block" data-testid="sidebar-create-agent-block">
              <button
                type="button"
                className="sidebar-item sidebar-create-agent-link"
                title={t('nav.newDigitalEmployee', 'New digital employee')}
                onClick={() => navigate('/')}
              >
                <span className="sidebar-item-icon sidebar-item-icon-centered">
                  <IconPlus size={15} stroke={1.8} />
                </span>
                <span className="sidebar-item-text">{t('nav.newDigitalEmployee', 'New digital employee')}</span>
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="sidebar-bottom">
        <div className="sidebar-footer">
          <div ref={accountMenuRef} className="sidebar-settings-menu-wrap">
            {showAccountMenu && (
              <div className="account-dropdown sidebar-settings-dropdown">
                <div className="account-dropdown-identity">
                  <div className="sidebar-account-avatar">{sidebarIcons.user}</div>
                  <div className="account-dropdown-identity-copy">
                    <div className="account-dropdown-name">{user?.display_name || user?.email || t('nav.account', 'Account')}</div>
                    <div className="account-dropdown-role">{getAccountRoleLabel(user, t)}</div>
                  </div>
                </div>
                <div className="account-dropdown-separator" />
                <button className="account-dropdown-item" onClick={onOpenAccountSettings}>
                  <IconUser size={15} stroke={1.5} />
                  <span>{isChinese ? '账户设置' : 'Account Settings'}</span>
                </button>
                <NavLink to="/knowledge" className="account-dropdown-item">
                  <IconDatabase size={15} stroke={1.5} />
                  <span>{t('nav.knowledge', 'Knowledge')}</span>
                </NavLink>
                <NavLink to="/local-agents" className="account-dropdown-item">
                  <IconDeviceDesktop size={15} stroke={1.5} />
                  <span>{t('nav.bridge', 'Local connection')}</span>
                </NavLink>
                <NavLink to="/connections" className="account-dropdown-item">
                  <IconWorld size={15} stroke={1.5} />
                  <span>{t('vault.title')}</span>
                </NavLink>
                <NavLink to="/scheduled-work" className="account-dropdown-item">
                  <IconBell size={15} stroke={1.5} />
                  <span>{t('memberSettings.schedules')}</span>
                </NavLink>
                {canSeeControlPlane && (
                  <NavLink to="/enterprise/users" className="account-dropdown-item">
                    <IconBuilding size={15} stroke={1.5} />
                    <span>{t('nav.enterprise', 'Company Admin')}</span>
                  </NavLink>
                )}
                {user?.role === 'platform_admin' && (
                  <NavLink to="/admin/platform-settings" className="account-dropdown-item">
                    <IconSettings size={15} stroke={1.5} />
                    <span>{t('nav.platformSettings', 'Platform Settings')}</span>
                  </NavLink>
                )}
                <div className="account-dropdown-separator" />
                <button className="account-dropdown-item" onClick={onToggleTheme}>
                  {theme === 'dark' ? <IconSun size={15} stroke={1.5} /> : <IconMoon size={15} stroke={1.5} />}
                  <span>{t('nav.theme', 'Theme')}</span>
                </button>
                <button className="account-dropdown-item" onClick={onOpenNotifications}>
                  <IconBell size={15} stroke={1.5} />
                  <span>{t('nav.notifications', 'Notifications')}</span>
                  {unreadCount > 0 && <span className="account-dropdown-count">{unreadCount > 99 ? '99+' : unreadCount}</span>}
                </button>
                <div className="account-dropdown-separator" />
                <button className="account-dropdown-item" onClick={onToggleLang}>
                  <IconWorld size={15} stroke={1.5} />
                  <span>{isChinese ? 'English' : '中文'}</span>
                </button>
                <div className="account-dropdown-separator" />
                <button className="account-dropdown-item account-dropdown-danger" onClick={onLogout}>
                  <IconLogout size={15} stroke={1.5} />
                  <span>{t('layout.logout', 'Logout')}</span>
                </button>
                {versionDisplay && (
                  <>
                    <div className="account-dropdown-separator" />
                    <div className="account-dropdown-version">{versionDisplay}</div>
                  </>
                )}
              </div>
            )}
            <button
              className="sidebar-settings-row"
              onClick={onToggleAccountMenu}
              type="button"
              ref={settingsTriggerRef}
              aria-expanded={showAccountMenu}
              title={t('nav.settings', 'Settings')}
            >
              <IconSettings size={17} stroke={1.7} />
              <span>{t('nav.settings', 'Settings')}</span>
              <IconChevronUp
                size={14}
                stroke={1.5}
                className="sidebar-account-chevron"
                style={{ transform: showAccountMenu ? 'rotate(0deg)' : 'rotate(180deg)' }}
              />
            </button>
          </div>
        </div>
      </div>
    </nav>
  );
}
