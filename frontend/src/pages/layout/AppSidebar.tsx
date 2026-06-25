import { useEffect, useMemo, useState, type ReactNode, type RefObject } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconArrowUpRight,
  IconBell,
  IconBuilding,
  IconCheckbox,
  IconChevronUp,
  IconChevronsLeft,
  IconChevronsRight,
  IconChevronDown,
  IconChevronRight,
  IconDeviceDesktop,
  IconFolder,
  IconLogout,
  IconMessageCircle,
  IconMoon,
  IconPlus,
  IconSettings,
  IconSitemap,
  IconSun,
  IconTrash,
  IconUser,
  IconWorld,
} from '@tabler/icons-react';
import { chatApi, type ChatSession } from '../../api/domains/chat';
import { agentApi, type HrAgentInfo } from '../../api/domains/agents';
import { localBridgeApi, type LocalAgentChannelSession } from '../../api/domains/localBridge';

const sidebarIcons = {
  plus: <IconPlus size={16} stroke={1.5} />,
  user: <IconUser size={16} stroke={1.5} />,
  collapse: <IconChevronsLeft size={16} stroke={1.5} />,
  expand: <IconChevronsRight size={16} stroke={1.5} />,
};

type SidebarNavItem = {
  to: string;
  labelKey: string;
  fallback: string;
  icon: ReactNode;
  end?: boolean;
};

const workspaceNavItems: SidebarNavItem[] = [
  { to: '/plaza', labelKey: 'nav.plaza', fallback: 'Agent Circle', icon: <IconSitemap size={15} stroke={1.6} /> },
  { to: '/automations', labelKey: 'nav.tasksAutomation', fallback: 'Tasks / Automation', icon: <IconCheckbox size={15} stroke={1.6} /> },
  { to: '/local-agents', labelKey: 'nav.bridge', fallback: 'Bridge', icon: <IconDeviceDesktop size={15} stroke={1.6} /> },
];

const isLocalAgentRuntimeType = (agent: any): boolean => agent?.agent_type === 'local_agent' || agent?.agent_type === 'openclaw';
const isLocalAgentType = (agent: any): boolean => agent?.agent_type === 'local_agent';

const getAgentBadgeStatus = (agent: any): string | null => {
  if (agent.status === 'error') return 'error';
  if (agent.status === 'creating') return 'creating';
  if (agent.agent_type === 'openclaw' && agent.status === 'running' && agent.openclaw_last_seen) {
    const elapsed = Date.now() - new Date(agent.openclaw_last_seen).getTime();
    if (elapsed > 60 * 60 * 1000) return 'disconnected';
  }
  return null;
};

const getAgentSourceBadge = (agent: any, user: any, t: any): string | null => {
  if (isLocalAgentRuntimeType(agent)) return t('nav.localBadge', 'Local');
  if (agent.creator_id && user?.id && agent.creator_id !== user.id) return t('nav.publicBadge', 'Public');
  if (agent.visibility_scope === 'public' || agent.is_public) return t('nav.publicBadge', 'Public');
  return null;
};

const getAccountRoleLabel = (user: any, t: any): string => {
  if (user?.role === 'platform_admin') return t('nav.superAdmin', 'Super Admin');
  if (user?.role === 'org_admin') return t('nav.orgAdmin', 'Company Admin');
  return t('nav.member', 'Member');
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
  agentSessionsByAgentId?: Record<string, ChatSession[]>;
  hrAgent?: HrAgentInfo | null;
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

function getActiveSessionId(search: string): string | null {
  const params = new URLSearchParams(search);
  return params.get('session_id') || params.get('session') || null;
}

function getSessionTag(session: ChatSession | any, t: any): string | null {
  const source = String(session.source_channel || session.thread_source || '').toLowerCase();
  if (source === 'trigger' || source === 'schedule' || source === 'task' || session.runtime_task_id) {
    return t('nav.sessionTaskBadge', 'Task');
  }
  if (source === 'feishu' || source === 'slack' || source === 'dingtalk' || source === 'wecom' || source === 'wechat_personal' || source === 'telegram' || source === 'email') {
    return t('nav.sessionImBadge', 'IM');
  }
  if (source === 'local_bridge' || source === 'local_agent') return t('nav.localBadge', 'Local');
  return null;
}

export function sidebarSessionFromLocalAgentChannelSession(
  agentId: string,
  session: LocalAgentChannelSession,
): ChatSession & Record<string, unknown> {
  const createdAt = session.created_at || session.last_message_at || session.updated_at || '';
  const updatedAt = session.updated_at || session.last_message_at || session.created_at || createdAt;
  const routeSessionId = session.chat_session_id || session.id;
  return {
    id: routeSessionId,
    agent_id: agentId,
    title: session.title || 'Local Agent Chat',
    created_at: createdAt,
    updated_at: updatedAt,
    last_message_at: session.last_message_at || null,
    chat_session_id: session.chat_session_id,
    channel_session_id: session.id,
    local_channel_session_id: session.id,
    source_channel: session.source_channel || 'local_agent',
    session_kind: session.session_kind || 'local_agent_channel',
    thread_source: 'local_agent',
  } as ChatSession & Record<string, unknown>;
}

function formatSessionTime(session: ChatSession | any, locale: string): string {
  const raw = session.last_message_at || session.updated_at || session.created_at;
  if (!raw) return '';
  try {
    return new Date(raw).toLocaleDateString(locale, { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
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
  agentSessionsByAgentId,
  hrAgent: providedHrAgent,
}: AppSidebarProps) {
  const { t, i18n } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const activeAgentId = getActiveAgentId(location.pathname);
  const activeSessionId = getActiveSessionId(location.search);
  const isCreateAgentRoute = location.pathname === '/agents/new';
  const [resolvedHrAgent, setResolvedHrAgent] = useState<HrAgentInfo | null>(providedHrAgent ?? null);
  const createAgentId = resolvedHrAgent?.id ? String(resolvedHrAgent.id) : null;
  const isCreateAgentActive = isCreateAgentRoute || (!!createAgentId && activeAgentId === createAgentId);
  const [expandedAgentIds, setExpandedAgentIds] = useState<Set<string>>(() => new Set(activeAgentId ? [activeAgentId] : []));
  const [isCreateAgentExpanded, setIsCreateAgentExpanded] = useState(isCreateAgentActive);
  const [sessionsByAgentId, setSessionsByAgentId] = useState<Record<string, ChatSession[]>>(agentSessionsByAgentId || {});
  const [sessionLoadingByAgentId, setSessionLoadingByAgentId] = useState<Record<string, boolean>>({});
  const locale = i18n.language?.startsWith('zh') ? 'zh-CN' : 'en-US';
  const sortedAgents = [...agents]
    .filter((agent) => !createAgentId || String(agent.id) !== createAgentId)
    .sort((a, b) => {
      const aTime = a.last_active_at ? new Date(a.last_active_at).getTime() : (a.created_at ? new Date(a.created_at).getTime() : 0);
      const bTime = b.last_active_at ? new Date(b.last_active_at).getTime() : (b.created_at ? new Date(b.created_at).getTime() : 0);
      return bTime - aTime;
    });
  const getSidebarAgentById = (agentId: string) =>
    sortedAgents.find((agent) => String(agent.id) === String(agentId))
    || (createAgentId && String(createAgentId) === String(agentId) ? resolvedHrAgent : null);

  const canSeeControlPlane = user && ['platform_admin', 'org_admin'].includes(user.role);
  const activeTenantName = tenants.find((tenant) => tenant.id === currentTenant)?.name || t('layout.myCompany', 'My Company');
  const effectiveSessionsByAgentId = useMemo(
    () => ({ ...sessionsByAgentId, ...(agentSessionsByAgentId || {}) }),
    [agentSessionsByAgentId, sessionsByAgentId],
  );

  useEffect(() => {
    if (providedHrAgent !== undefined) {
      setResolvedHrAgent(providedHrAgent);
      return;
    }
    if (!user) {
      setResolvedHrAgent(null);
      return;
    }
    let cancelled = false;
    agentApi.getHrAgent()
      .then((agent) => {
        if (!cancelled) setResolvedHrAgent(agent);
      })
      .catch(() => {
        if (!cancelled) setResolvedHrAgent(null);
      });
    return () => {
      cancelled = true;
    };
  }, [providedHrAgent, user, currentTenant]);

  useEffect(() => {
    if (!activeAgentId) return;
    setExpandedAgentIds((prev) => {
      if (prev.has(activeAgentId)) return prev;
      const next = new Set(prev);
      next.add(activeAgentId);
      return next;
    });
  }, [activeAgentId]);

  useEffect(() => {
    if (isCreateAgentActive) setIsCreateAgentExpanded(true);
  }, [isCreateAgentActive]);

  const loadAgentSessions = async (agentId: string) => {
    if (agentSessionsByAgentId?.[agentId] || sessionsByAgentId[agentId] || sessionLoadingByAgentId[agentId]) return;
    setSessionLoadingByAgentId((prev) => ({ ...prev, [agentId]: true }));
    try {
      const agent = getSidebarAgentById(agentId);
      if (isLocalAgentType(agent)) {
        const rows = await localBridgeApi.listAgentChannelSessions(agentId);
        setSessionsByAgentId((prev) => ({
          ...prev,
          [agentId]: rows.map((session) => sidebarSessionFromLocalAgentChannelSession(agentId, session)),
        }));
        return;
      }
      const rows = await chatApi.listSessions(agentId, 'mine');
      setSessionsByAgentId((prev) => ({
        ...prev,
        [agentId]: rows.filter((session: any) => session.source_channel !== 'heartbeat'),
      }));
    } catch {
      setSessionsByAgentId((prev) => ({ ...prev, [agentId]: [] }));
    } finally {
      setSessionLoadingByAgentId((prev) => ({ ...prev, [agentId]: false }));
    }
  };

  const replaceAgentSessions = (agentId: string, updater: (sessions: ChatSession[]) => ChatSession[]) => {
    setSessionsByAgentId((prev) => ({
      ...prev,
      [agentId]: updater(prev[agentId] || []),
    }));
  };

  const toggleAgentSessions = (agentId: string) => {
    setExpandedAgentIds((prev) => {
      const next = new Set(prev);
      if (next.has(agentId)) next.delete(agentId);
      else next.add(agentId);
      return next;
    });
    void loadAgentSessions(agentId);
  };

  const handleCreateSession = async (agentId: string) => {
    try {
      const agent = getSidebarAgentById(agentId);
      const session = isLocalAgentType(agent)
        ? sidebarSessionFromLocalAgentChannelSession(
          agentId,
          await localBridgeApi.createAgentChannelSession(agentId, {
            title: t('agent.chat.newSession', 'New Conversation'),
          }),
        )
        : await chatApi.createSession(agentId);
      replaceAgentSessions(agentId, (rows) => [session, ...rows.filter((row) => String(row.id) !== String(session.id))]);
      navigate(`/agents/${agentId}?session_id=${encodeURIComponent(String(session.id))}#chat`);
    } catch (error) {
      console.error('Failed to create chat session:', error);
    }
  };

  const handleDeleteSession = async (agentId: string, session: ChatSession | any) => {
    const sessionId = String(session.id);
    const label = session.title || t('agent.chat.session', 'Session');
    const ok = window.confirm(t('chat.deleteConfirmWithTitle', 'Delete "{{title}}" and all its messages? This cannot be undone.', { title: label }));
    if (!ok) return;
    try {
      const agent = getSidebarAgentById(agentId);
      if (isLocalAgentType(agent)) {
        await localBridgeApi.deleteAgentChannelSession(agentId, String((session as any).channel_session_id || session.id));
      } else {
        await chatApi.deleteSession(agentId, sessionId);
      }
      const currentRows = effectiveSessionsByAgentId[agentId] || [];
      const nextRows = currentRows.filter((row: any) => (
        String(row.id) !== sessionId && String(row.chat_session_id || '') !== sessionId
      ));
      replaceAgentSessions(agentId, () => nextRows);
      if (String(activeAgentId || '') === String(agentId) && String(activeSessionId || '') === sessionId) {
        const next = nextRows[0];
        if (next) navigate(`/agents/${agentId}?session_id=${encodeURIComponent(String(next.id))}#chat`);
        else navigate(`/agents/${agentId}#chat`);
      }
    } catch (error) {
      console.error('Failed to delete chat session:', error);
    }
  };

  useEffect(() => {
    if (!activeAgentId) return;
    void loadAgentSessions(activeAgentId);
    // The loader intentionally watches only route agent changes; manual
    // expansion calls the same loader directly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeAgentId]);

  useEffect(() => {
    if (!createAgentId || !isCreateAgentActive) return;
    void loadAgentSessions(createAgentId);
    // Same loader as active agent expansion; route changes are the relevant
    // trigger here, not every render-time state update.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createAgentId, isCreateAgentActive]);

  const createAgentHref = createAgentId ? `/agents/${createAgentId}#chat` : '/agents/new';
  const createAgentSessions = createAgentId ? (effectiveSessionsByAgentId[createAgentId] || []) : [];
  const createAgentSessionsLoading = createAgentId ? sessionLoadingByAgentId[createAgentId] : false;

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
            {user?.role === 'platform_admin' && tenants.length > 1 ? (
              <select
                value={currentTenant}
                onChange={e => onSwitchTenant(e.target.value)}
                aria-label={t('layout.switchTenant', 'Switch company')}
                className="sidebar-workspace-select"
              >
                {tenants.map(tn => (
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
        <div className="sidebar-section sidebar-nav-section sidebar-top-actions">
          {workspaceNavItems.map((item) => (
            <SidebarNavLink key={item.to} item={item} />
          ))}
        </div>

        <div className="sidebar-section sidebar-nav-section">
          <NavLink to="/agents" className="sidebar-section-title sidebar-section-title-link sidebar-tree-heading">
            <IconFolder size={14} stroke={1.7} />
            <span>{t('nav.digitalEmployees', 'Digital Employees')}</span>
          </NavLink>
          {sortedAgents.map((agent) => {
            const badge = getAgentBadgeStatus(agent);
            const sourceBadge = getAgentSourceBadge(agent, user, t);
            const avatarChar = ((Array.from(agent.name || '?')[0] as string) || '?').toUpperCase();
            const isExpanded = expandedAgentIds.has(String(agent.id));
            const agentSessions = effectiveSessionsByAgentId[String(agent.id)] || [];
            const sessionsLoading = sessionLoadingByAgentId[String(agent.id)];
            const isAgentRowActive = String(activeAgentId || '') === String(agent.id) && !activeSessionId;
            return (
              <div key={agent.id} className="sidebar-agent-item">
                <div className="sidebar-agent-row">
                  <button
                    type="button"
                    className="sidebar-agent-disclosure"
                    aria-label={isExpanded ? t('nav.collapseAgentSessions', 'Collapse sessions') : t('nav.expandAgentSessions', 'Expand sessions')}
                    aria-expanded={isExpanded}
                    onClick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      toggleAgentSessions(String(agent.id));
                    }}
                  >
                    {isExpanded ? <IconChevronDown size={13} stroke={1.7} /> : <IconChevronRight size={13} stroke={1.7} />}
                  </button>
                  <button
                    type="button"
                    className={`sidebar-item sidebar-agent-link ${isAgentRowActive ? 'active' : ''}`}
                    title={agent.name}
                    aria-label={`Toggle ${agent.name} sessions`}
                    aria-expanded={isExpanded}
                    onClick={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      toggleAgentSessions(String(agent.id));
                    }}
                  >
                    <span className="sidebar-item-icon" style={{ position: 'relative' }}>
                      <span className={`agent-avatar${isLocalAgentRuntimeType(agent) ? ' openclaw' : ''}`}>{avatarChar}</span>
                      {isLocalAgentRuntimeType(agent) && (
                        <span className="agent-avatar-link" style={{ display: 'flex' }}>
                          {isLocalAgentType(agent) ? (
                            <IconDeviceDesktop size={10} stroke={2.5} />
                          ) : (
                            <IconArrowUpRight size={10} stroke={2.5} />
                          )}
                        </span>
                      )}
                      {badge && <span className={`agent-avatar-badge ${badge}`} />}
                    </span>
                    <span className="sidebar-item-text">{agent.name}</span>
                    {sourceBadge && !isSidebarCollapsed && <span className="sidebar-agent-source-badge">{sourceBadge}</span>}
                  </button>
                  {!isSidebarCollapsed && (
                    <span className="sidebar-agent-actions" aria-hidden={false}>
                      <button
                        type="button"
                        className="sidebar-agent-action"
                        aria-label={`New conversation with ${agent.name}`}
                        title={t('agent.chat.newSession', 'New Conversation')}
                        onClick={(event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          void handleCreateSession(String(agent.id));
                        }}
                      >
                        <IconPlus size={13} stroke={1.8} />
                      </button>
                      <NavLink
                        to={`/agents/${agent.id}?manage=true#status`}
                        className={() => 'sidebar-agent-action'}
                        aria-label={`Open ${agent.name} details`}
                        title={t('agent.details', 'Details')}
                      >
                        <IconArrowUpRight size={13} stroke={1.8} />
                      </NavLink>
                    </span>
                  )}
                </div>
                {isExpanded && !isSidebarCollapsed && (
                  <div className="sidebar-agent-sessions" data-testid={`sidebar-agent-sessions-${agent.id}`}>
                    {sessionsLoading ? (
                      <div className="sidebar-session-muted">{t('common.loading', 'Loading')}</div>
                    ) : agentSessions.length === 0 ? (
                      <div className="sidebar-session-muted">{t('agent.chat.noSessionsYet', 'No conversations yet.')}</div>
                    ) : (
                      agentSessions.slice(0, 8).map((session: ChatSession | any) => {
                        const tag = getSessionTag(session, t);
                        const isActiveSession = String(activeAgentId || '') === String(agent.id)
                          && (
                            String(activeSessionId || '') === String(session.id)
                            || String(activeSessionId || '') === String(session.chat_session_id || '')
                          );
                        return (
                          <div key={session.id} className={`sidebar-session-row ${isActiveSession ? 'active' : ''}`}>
                            <NavLink
                              to={`/agents/${agent.id}?session_id=${encodeURIComponent(String(session.id))}#chat`}
                              className={() => `sidebar-session-item ${isActiveSession ? 'active' : ''}`}
                              title={session.title}
                            >
                              <span className="sidebar-session-title">{session.title || t('agent.chat.session', 'Session')}</span>
                              {tag && <span className="sidebar-session-tag">{tag}</span>}
                              <span className="sidebar-session-meta">
                                {formatSessionTime(session, locale)}
                                {session.message_count ? ` · ${session.message_count}` : ''}
                              </span>
                            </NavLink>
                            <button
                              type="button"
                              className="sidebar-session-action"
                              aria-label={`Delete session ${session.title || t('agent.chat.session', 'Session')}`}
                              title={t('common.delete', 'Delete')}
                              onClick={(event) => {
                                event.preventDefault();
                                event.stopPropagation();
                                void handleDeleteSession(String(agent.id), session);
                              }}
                            >
                              <IconTrash size={12} stroke={1.8} />
                            </button>
                          </div>
                        );
                      })
                    )}
                  </div>
                )}
              </div>
            );
          })}
          {agents.length === 0 && (
            <div className="sidebar-empty-note">{t('nav.noEmployees', 'No digital employees yet')}</div>
          )}
          {user && (
            <div className="sidebar-create-agent-block" data-testid="sidebar-create-agent-block">
              <div className="sidebar-agent-row">
                <button
                  type="button"
                  className="sidebar-agent-disclosure"
                  aria-label={isCreateAgentExpanded ? t('nav.collapseAgentSessions', 'Collapse sessions') : t('nav.expandAgentSessions', 'Expand sessions')}
                  aria-expanded={isCreateAgentExpanded}
                  onClick={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    setIsCreateAgentExpanded((prev) => !prev);
                    if (createAgentId) void loadAgentSessions(createAgentId);
                  }}
                >
                  {isCreateAgentExpanded ? <IconChevronDown size={13} stroke={1.7} /> : <IconChevronRight size={13} stroke={1.7} />}
                </button>
                <button
                  type="button"
                  className={`sidebar-item sidebar-agent-link sidebar-create-agent-link ${isCreateAgentActive && !activeSessionId ? 'active' : ''}`}
                  title={t('nav.createAgent', 'Create Agent')}
                  aria-label={`Toggle ${t('nav.createAgent', 'Create Agent')} sessions`}
                  aria-expanded={isCreateAgentExpanded}
                  onClick={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    if (!createAgentId) {
                      navigate(createAgentHref);
                      return;
                    }
                    setIsCreateAgentExpanded((prev) => !prev);
                    void loadAgentSessions(createAgentId);
                  }}
                >
                  <span className="sidebar-item-icon" style={{ position: 'relative' }}>
                    <span className="agent-avatar create-agent-avatar">
                      <IconMessageCircle size={13} stroke={1.8} />
                    </span>
                  </span>
                  <span className="sidebar-item-text">{t('nav.createAgent', 'Create Agent')}</span>
                  {!isSidebarCollapsed && <span className="sidebar-agent-source-badge">{t('nav.hrAgentBadge', 'HR')}</span>}
                </button>
                {!isSidebarCollapsed && createAgentId && (
                  <span className="sidebar-agent-actions" aria-hidden={false}>
                    <button
                      type="button"
                      className="sidebar-agent-action"
                      aria-label={`New conversation with ${t('nav.createAgent', 'Create Agent')}`}
                      title={t('agent.chat.newSession', 'New Conversation')}
                      onClick={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        void handleCreateSession(createAgentId);
                      }}
                    >
                      <IconPlus size={13} stroke={1.8} />
                    </button>
                  </span>
                )}
              </div>
              {isCreateAgentExpanded && !isSidebarCollapsed && (
                <div className="sidebar-agent-sessions" data-testid="sidebar-create-agent-sessions">
                  {!createAgentId || createAgentSessionsLoading ? (
                    <div className="sidebar-session-muted">{t('common.loading', 'Loading')}</div>
                  ) : createAgentSessions.length === 0 ? (
                    <div className="sidebar-session-muted">{t('agent.chat.noSessionsYet', 'No conversations yet.')}</div>
                  ) : (
                    createAgentSessions.slice(0, 8).map((session: ChatSession | any) => {
                      const tag = getSessionTag(session, t);
                      const isActiveSession = activeAgentId === createAgentId && String(activeSessionId || '') === String(session.id);
                      return (
                        <div key={session.id} className={`sidebar-session-row ${isActiveSession ? 'active' : ''}`}>
                          <NavLink
                            to={`/agents/${createAgentId}?session_id=${encodeURIComponent(String(session.id))}#chat`}
                            className={() => `sidebar-session-item ${isActiveSession ? 'active' : ''}`}
                            title={session.title}
                          >
                            <span className="sidebar-session-title">{session.title || t('agent.chat.session', 'Session')}</span>
                            {tag && <span className="sidebar-session-tag">{tag}</span>}
                            <span className="sidebar-session-meta">
                              {formatSessionTime(session, locale)}
                              {session.message_count ? ` · ${session.message_count}` : ''}
                            </span>
                          </NavLink>
                          <button
                            type="button"
                            className="sidebar-session-action"
                            aria-label={`Delete session ${session.title || t('agent.chat.session', 'Session')}`}
                            title={t('common.delete', 'Delete')}
                            onClick={(event) => {
                              event.preventDefault();
                              event.stopPropagation();
                              if (createAgentId) void handleDeleteSession(createAgentId, session);
                            }}
                          >
                            <IconTrash size={12} stroke={1.8} />
                          </button>
                        </div>
                      );
                    })
                  )}
                </div>
              )}
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
                {canSeeControlPlane && (
                  <NavLink to="/enterprise/dashboard" className="account-dropdown-item">
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
                  <span>{i18n.language === 'zh' ? 'English' : '中文'}</span>
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
