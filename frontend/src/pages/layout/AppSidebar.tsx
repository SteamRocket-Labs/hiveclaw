import type { ReactNode, RefObject } from 'react';
import { NavLink } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconHome,
  IconPlus,
  IconSettings,
  IconUser,
  IconSun,
  IconMoon,
  IconLogout,
  IconWorld,
  IconChevronsLeft,
  IconChevronsRight,
  IconBell,
  IconBuildingMonument,
  IconSearch,
  IconX,
  IconPin,
  IconPinnedOff,
  IconArrowUpRight,
  IconBuilding,
  IconChevronUp,
} from '@tabler/icons-react';

const sidebarIcons = {
  home: <IconHome size={16} stroke={1.5} />,
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

  return (
    <nav className={`sidebar ${isSidebarCollapsed ? 'collapsed' : ''}`}>
      <div className="sidebar-top">
        <div className="sidebar-logo">
          <span className="sidebar-logo-text" style={{ fontWeight: 700, fontSize: '16px', letterSpacing: '-0.03em' }}>HiveClaw</span>
          <button
            className="btn btn-ghost sidebar-collapse-btn"
            onClick={onToggleSidebar}
            style={{
              padding: '4px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              marginLeft: 'auto',
              color: 'var(--text-tertiary)',
            }}
            title={isSidebarCollapsed ? 'Expand Sidebar' : 'Collapse Sidebar'}
          >
            {isSidebarCollapsed ? sidebarIcons.expand : sidebarIcons.collapse}
          </button>
        </div>

        <div className="sidebar-section">
          <NavLink to="/plaza" className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}>
            <span className="sidebar-item-icon" style={{ display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
              <IconBuildingMonument size={14} stroke={1.5} />
            </span>
            <span className="sidebar-item-text">{t('nav.plaza', 'Plaza')}</span>
          </NavLink>
          <NavLink to="/dashboard" className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}>
            <span className="sidebar-item-icon" style={{ display: 'flex' }}>{sidebarIcons.home}</span>
            <span className="sidebar-item-text">{t('nav.dashboard')}</span>
          </NavLink>
        </div>
      </div>

      <div className="sidebar-divider" />

      <div className="sidebar-scrollable">
        {!isSidebarCollapsed && agents.length >= 5 && (
          <div style={{ padding: '4px 12px 4px', position: 'relative' }}>
            <div
              style={{
                position: 'absolute',
                left: '20px',
                top: '50%',
                transform: 'translateY(-50%)',
                pointerEvents: 'none',
                color: 'var(--text-tertiary)',
                display: 'flex',
              }}
            >
              <IconSearch size={14} stroke={2} />
            </div>
            <input
              type="text"
              value={sidebarSearch}
              onChange={(event) => onSetSidebarSearch(event.target.value)}
              placeholder={isChinese ? '搜索...' : 'Search...'}
              style={{
                width: '100%',
                padding: '5px 24px 5px 28px',
                border: '1px solid var(--border-subtle)',
                borderRadius: '6px',
                background: 'var(--bg-secondary)',
                color: 'var(--text-primary)',
                fontSize: '12px',
                outline: 'none',
                boxSizing: 'border-box',
              }}
              onFocus={(event) => {
                event.target.style.borderColor = 'var(--primary)';
              }}
              onBlur={(event) => {
                event.target.style.borderColor = 'var(--border-subtle)';
              }}
            />
            {sidebarSearch && (
              <button
                onClick={() => onSetSidebarSearch('')}
                style={{
                  position: 'absolute',
                  right: '18px',
                  top: '50%',
                  transform: 'translateY(-50%)',
                  background: 'none',
                  border: 'none',
                  color: 'var(--text-tertiary)',
                  cursor: 'pointer',
                  display: 'flex',
                  padding: 0,
                }}
              >
                <IconX size={14} stroke={2} />
              </button>
            )}
          </div>
        )}

        {sortedAgents.map((agent) => {
          const badge = getAgentBadgeStatus(agent);
          const avatarChar = ((Array.from(agent.name || '?')[0] as string) || '?').toUpperCase();
          return (
            <div key={agent.id} style={{ position: 'relative' }} className={`sidebar-agent-item${agent.creator_id === user?.id ? ' owned' : ''}`}>
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
          <div className="sidebar-section">
            <div className="sidebar-section-title">{t('nav.myAgents')}</div>
          </div>
        )}
        {agents.length > 0 && sortedAgents.length === 0 && query && (
          <div style={{ padding: '12px 16px', fontSize: '12px', color: 'var(--text-tertiary)', textAlign: 'center' }}>
            {isChinese ? '无匹配结果' : 'No matches'}
          </div>
        )}
      </div>

      <div className="sidebar-bottom">
        <div className="sidebar-section" style={{ borderBottom: '1px solid var(--border-subtle)', paddingBottom: '8px', marginBottom: 0 }}>
          {user && (
            <NavLink to="/agents/new" className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`} title={t('nav.newAgent')}>
              <span className="sidebar-item-icon" style={{ display: 'flex' }}>{sidebarIcons.plus}</span>
              <span className="sidebar-item-text">{t('nav.newAgent')}</span>
            </NavLink>
          )}
          {user && ['platform_admin', 'org_admin'].includes(user.role) && (
            <NavLink to="/enterprise" className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`} title={t('nav.enterprise')}>
              <span className="sidebar-item-icon" style={{ display: 'flex' }}>
                <IconBuilding size={16} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">{t('nav.enterprise')}</span>
            </NavLink>
          )}
          {user && user.role === 'platform_admin' && (
            <NavLink
              to="/admin/platform-settings"
              className={({ isActive }) => `sidebar-item ${isActive ? 'active' : ''}`}
              title={t('nav.platformSettings', 'Platform Settings')}
            >
              <span className="sidebar-item-icon" style={{ display: 'flex' }}>
                <IconSettings size={16} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">{t('nav.platformSettings', 'Platform Settings')}</span>
            </NavLink>
          )}
        </div>

        <div className="sidebar-footer">
          <div
            className="sidebar-footer-controls"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              marginBottom: '8px',
            }}
          >
            <button
              className="btn btn-ghost"
              onClick={onToggleTheme}
              style={{
                padding: '4px 8px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
              title={theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
            >
              {theme === 'dark' ? sidebarIcons.sun : sidebarIcons.moon}
            </button>
            <button
              className="btn btn-ghost"
              onClick={onOpenNotifications}
              style={{
                padding: '4px 8px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                position: 'relative',
              }}
              title={isChinese ? '通知' : 'Notifications'}
            >
              {sidebarIcons.bell}
              {unreadCount > 0 && (
                <span
                  style={{
                    position: 'absolute',
                    top: '-2px',
                    right: '-4px',
                    minWidth: '16px',
                    height: '16px',
                    borderRadius: '8px',
                    padding: '0 4px',
                    boxSizing: 'border-box',
                    background: 'var(--error)',
                    color: '#fff',
                    fontSize: '10px',
                    fontWeight: 600,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    lineHeight: 1,
                  }}
                >
                  {unreadCount > 99 ? '99+' : unreadCount}
                </span>
              )}
            </button>
          </div>

          {/* Tenant switcher (platform_admin only) */}
          {user?.role === 'platform_admin' && tenants.length > 1 && !isSidebarCollapsed && (
            <select
              value={currentTenant}
              onChange={e => onSwitchTenant(e.target.value)}
              aria-label={t('layout.switchTenant', 'Switch company')}
              className="tenant-switcher"
              style={{
                width: '100%', borderRadius: '6px',
                border: '1px solid var(--border-subtle)',
                background: 'var(--bg-tertiary)', color: 'var(--text-primary)',
                fontSize: '12px', cursor: 'pointer',
                padding: '5px 8px', marginBottom: '8px',
              }}
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
              <div
                style={{
                  width: '28px',
                  height: '28px',
                  borderRadius: 'var(--radius-md)',
                  background: 'var(--bg-tertiary)',
                  border: '1px solid var(--border-subtle)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: 'var(--text-tertiary)',
                  flexShrink: 0,
                }}
              >
                {sidebarIcons.user}
              </div>
              <div className="sidebar-footer-user-info" style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: '13px', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {user?.display_name}
                </div>
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{getRoleLabel(user?.role, t)}</div>
              </div>
              <IconChevronUp
                size={14}
                stroke={1.5}
                style={{
                  color: 'var(--text-tertiary)',
                  flexShrink: 0,
                  transform: showAccountMenu ? 'rotate(0deg)' : 'rotate(180deg)',
                  transition: 'transform 0.2s ease',
                }}
              />
            </div>
          </div>

          {versionDisplay}
        </div>
      </div>
    </nav>
  );
}
