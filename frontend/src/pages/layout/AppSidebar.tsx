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
  IconDatabase,
  IconDeviceDesktop,
  IconFolder,
  IconGitBranch,
  IconHome,
  IconLogout,
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
import { isA2ASession } from '../agent-detail/chatRuntime';

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
  { to: '/home', labelKey: 'nav.home', fallback: 'Home', icon: <IconHome size={15} stroke={1.6} />, end: true },
  { to: '/plaza', labelKey: 'nav.plaza', fallback: 'Agent Circle', icon: <IconSitemap size={15} stroke={1.6} /> },
  { to: '/automations', labelKey: 'nav.tasksAutomation', fallback: 'Automation', icon: <IconCheckbox size={15} stroke={1.6} /> },
  { to: '/knowledge', labelKey: 'nav.knowledge', fallback: 'Knowledge', icon: <IconDatabase size={15} stroke={1.6} /> },
  { to: '/local-agents', labelKey: 'nav.bridge', fallback: 'Local connection', icon: <IconDeviceDesktop size={15} stroke={1.6} /> },
];

const isLocalAgentRuntimeType = (agent: any): boolean => agent?.agent_type === 'local_agent';
const isLocalAgentType = (agent: any): boolean => agent?.agent_type === 'local_agent';

const getAgentBadgeStatus = (agent: any): string | null => {
  if (agent.status === 'error') return 'error';
  if (agent.status === 'creating') return 'creating';
  return null;
};

const getAgentSourceBadge = (agent: any, t: any): string | null => {
  if (isLocalAgentRuntimeType(agent)) return t('nav.localBadge', 'Local');
  if (agent.is_owner === false) return t('nav.publicBadge', 'Public');
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
  tenants: { id: string; name: string }[];
  currentTenant: string;
  onSwitchTenant: (tenantId: string) => void;
  isChinese: boolean;
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

export const COMPACT_SIDEBAR_MAX_WIDTH = 900;

export function isCompactSidebarViewport(width: number): boolean {
  return Number.isFinite(width) && width <= COMPACT_SIDEBAR_MAX_WIDTH;
}

const ACTIVE_AGENT_RE = /^\/agents\/([^/?#]+)/;
const ACTIVE_SESSION_PATH_RE = /^\/agents\/[^/?#]+\/sessions\/([^/?#]+)/;

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

function getActiveSessionId(pathname: string, search: string): string | null {
  const params = new URLSearchParams(search);
  const queryValue = params.get('session_id') || params.get('session');
  if (queryValue) return queryValue;
  const match = pathname.match(ACTIVE_SESSION_PATH_RE);
  const value = match?.[1];
  if (!value) return null;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function getSessionTag(session: ChatSession | any, t: any): string | null {
  return getSessionTags(session, t)[0] || null;
}

const BRANCH_SESSION_MODES = new Set(['branch', 'fork', 'rewind', 'edit', 'insert_before', 'insert_after', 'reply', 'regenerate']);

function sessionBranchMode(session: ChatSession | any): string {
  const metadata = session?.transcript_metadata_json && typeof session.transcript_metadata_json === 'object'
    ? session.transcript_metadata_json as Record<string, unknown>
    : {};
  return String(
    session?.branch_mode
      || metadata.branch_mode
      || metadata.mode
      || '',
  ).trim().toLowerCase();
}

export function isBranchSession(session: ChatSession | any): boolean {
  const mode = sessionBranchMode(session);
  if (mode && BRANCH_SESSION_MODES.has(mode)) return true;
  const id = session?.id == null ? '' : String(session.id);
  const parentId = session?.parent_session_id == null ? '' : String(session.parent_session_id);
  const rootId = session?.root_session_id == null ? '' : String(session.root_session_id);
  if (parentId && parentId !== id && rootId && rootId !== id) return true;
  return Boolean(parentId && parentId !== id && /\s+\(branch\)\s*$/i.test(String(session?.title || '')));
}

export function getSessionTags(session: ChatSession | any, t: any): string[] {
  const tags: string[] = [];
  const source = String(session.source_channel || session.thread_source || '').toLowerCase();
  if (isA2ASession(session)) {
    tags.push(t('nav.sessionA2aBadge', 'A2A'));
  }
  if (source === 'trigger' || source === 'schedule' || source === 'task' || session.runtime_task_id) {
    tags.push(t('nav.sessionTaskBadge', 'Task'));
  }
  if (source === 'feishu' || source === 'slack' || source === 'dingtalk' || source === 'wecom' || source === 'wechat_personal' || source === 'telegram' || source === 'email') {
    tags.push(t('nav.sessionImBadge', 'IM'));
  }
  if (source === 'local_bridge' || source === 'local_agent') tags.push(t('nav.localBadge', 'Local'));
  if (isBranchSession(session)) tags.push(t('nav.sessionBranchBadge', 'Branch'));
  return Array.from(new Set(tags));
}

export function displaySessionTitle(session: ChatSession | any, t: any): string {
  const fallback = t('agent.chat.session', 'Session');
  const title = String(session?.title || '').trim() || fallback;
  return isBranchSession(session) ? title.replace(/\s+\(branch\)\s*$/i, '').trim() || fallback : title;
}

export type SidebarSessionFamily = {
  root: ChatSession & Record<string, unknown>;
  children: Array<ChatSession & Record<string, unknown>>;
};

export function buildSidebarSessionFamilies(sessions: Array<ChatSession | any>): SidebarSessionFamily[] {
  const byId = new Map<string, ChatSession & Record<string, unknown>>();
  sessions.forEach((session) => {
    if (session?.id == null) return;
    byId.set(String(session.id), session as ChatSession & Record<string, unknown>);
  });

  const childrenByRootId = new Map<string, Array<ChatSession & Record<string, unknown>>>();
  const childIds = new Set<string>();
  sessions.forEach((session) => {
    if (!isBranchSession(session)) return;
    const id = String(session.id);
    const rootId = String(session.root_session_id || session.parent_session_id || '');
    const parentId = String(session.parent_session_id || '');
    const familyRootId = rootId && rootId !== id && byId.has(rootId)
      ? rootId
      : parentId && parentId !== id && byId.has(parentId)
        ? parentId
        : '';
    if (!familyRootId) return;
    const rows = childrenByRootId.get(familyRootId) || [];
    rows.push(session as ChatSession & Record<string, unknown>);
    childrenByRootId.set(familyRootId, rows);
    childIds.add(id);
  });

  return sessions
    .filter((session) => session?.id != null && !childIds.has(String(session.id)))
    .map((session) => {
      const id = String(session.id);
      return {
        root: session as ChatSession & Record<string, unknown>,
        children: childrenByRootId.get(id) || [],
      };
    });
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
  tenants,
  currentTenant,
  onSwitchTenant,
  isChinese,
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
  const activeSessionId = getActiveSessionId(location.pathname, location.search);
  const isCreateAgentRoute = location.pathname === '/agents/new';
  const [resolvedHrAgent, setResolvedHrAgent] = useState<HrAgentInfo | null>(providedHrAgent ?? null);
  const createAgentId = resolvedHrAgent?.id ? String(resolvedHrAgent.id) : null;
  const isCreateAgentActive = isCreateAgentRoute || (!!createAgentId && activeAgentId === createAgentId);
  const [expandedAgentIds, setExpandedAgentIds] = useState<Set<string>>(() => new Set(activeAgentId ? [activeAgentId] : []));
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

  const [expandedSessionFamilyIds, setExpandedSessionFamilyIds] = useState<Set<string>>(() => new Set());

  const isActiveSidebarSession = (agentId: string, session: ChatSession | any): boolean => {
    if (String(activeAgentId || '') !== String(agentId) || !activeSessionId) return false;
    const activeId = String(activeSessionId);
    return [session.id, session.chat_session_id]
      .filter((candidate) => candidate != null && String(candidate).length > 0)
      .some((candidate) => String(candidate) === activeId);
  };

  const toggleSessionFamily = (sessionId: string) => {
    setExpandedSessionFamilyIds((prev) => {
      const next = new Set(prev);
      if (next.has(sessionId)) next.delete(sessionId);
      else next.add(sessionId);
      return next;
    });
  };

  const renderSessionTags = (session: ChatSession | any) => {
    const tags = getSessionTags(session, t);
    if (tags.length === 0) return null;
    return (
      <span className="sidebar-session-tags">
        {tags.map((tag) => (
          <span key={tag} className="sidebar-session-tag">{tag}</span>
        ))}
      </span>
    );
  };

  const renderSessionRow = (
    agentId: string,
    session: ChatSession | any,
    options: {
      family?: SidebarSessionFamily;
      child?: boolean;
      open?: boolean;
      activeChild?: boolean;
    } = {},
  ) => {
    const title = displaySessionTitle(session, t);
    const sessionId = String(session.id);
    const isActiveSession = isActiveSidebarSession(agentId, session);
    const children = options.family?.children || [];
    const hasChildren = children.length > 0 && !options.child;
    const rowClassName = [
      'sidebar-session-row',
      isActiveSession ? 'active' : '',
      hasChildren ? 'has-children' : '',
      options.child ? 'branch-child' : '',
      options.activeChild ? 'has-active-branch' : '',
    ].filter(Boolean).join(' ');
    return (
      <div key={sessionId} className={rowClassName}>
        {hasChildren && (
          <button
            type="button"
            className="sidebar-session-family-toggle"
            aria-label={options.open ? t('nav.collapseBranchSessions', 'Collapse branches') : t('nav.expandBranchSessions', 'Expand branches')}
            aria-expanded={options.open}
            onClick={(event) => {
              event.preventDefault();
              event.stopPropagation();
              toggleSessionFamily(sessionId);
            }}
          >
            {options.open ? <IconChevronDown size={12} stroke={1.8} /> : <IconChevronRight size={12} stroke={1.8} />}
          </button>
        )}
        <NavLink
          to={`/agents/${agentId}?session_id=${encodeURIComponent(sessionId)}#chat`}
          className={() => `sidebar-session-item ${isActiveSession ? 'active' : ''}`}
          title={title}
        >
          <span className="sidebar-session-title">{title}</span>
          {renderSessionTags(session)}
          <span className="sidebar-session-meta">
            {formatSessionTime(session, locale)}
            {session.message_count ? ` · ${session.message_count}` : ''}
          </span>
        </NavLink>
        {hasChildren && (
          <span className="sidebar-session-branch-count" data-testid="sidebar-session-branch-count" title={t('nav.branchCount', '{{count}} branches', { count: children.length })}>
            <IconGitBranch size={10} stroke={1.9} />
            {children.length}
          </span>
        )}
        <button
          type="button"
          className="sidebar-session-action"
          aria-label={`Delete session ${title}`}
          title={t('common.delete', 'Delete')}
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            void handleDeleteSession(agentId, session);
          }}
        >
          <IconTrash size={12} stroke={1.8} />
        </button>
      </div>
    );
  };

  const renderSessionFamilies = (agentId: string, sessions: ChatSession[]) => buildSidebarSessionFamilies(sessions)
    .slice(0, 8)
    .map((family) => {
      const rootId = String(family.root.id);
      const activeChild = family.children.some((session) => isActiveSidebarSession(agentId, session));
      const open = family.children.length > 0 && (expandedSessionFamilyIds.has(rootId) || activeChild);
      return (
        <div key={rootId} className="sidebar-session-family" data-testid={`sidebar-session-family-${rootId}`}>
          {renderSessionRow(agentId, family.root, { family, open, activeChild })}
          {open && (
            <div className="sidebar-session-children" data-testid={`sidebar-session-children-${rootId}`}>
              {family.children.map((child) => renderSessionRow(agentId, child, { child: true }))}
            </div>
          )}
        </div>
      );
    });

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
            const sourceBadge = getAgentSourceBadge(agent, t);
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
                  <NavLink
                    to={`/agents/${agent.id}#chat`}
                    className={() => `sidebar-item sidebar-agent-link ${isAgentRowActive ? 'active' : ''}`}
                    title={agent.name}
                    aria-label={`Open ${agent.name}`}
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
                    <span className="sidebar-item-text">{agent.name}</span>
                    {sourceBadge && !isSidebarCollapsed && <span className="sidebar-agent-source-badge">{sourceBadge}</span>}
                  </NavLink>
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
                      renderSessionFamilies(String(agent.id), agentSessions)
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
              <button
                type="button"
                className={`sidebar-item sidebar-create-agent-link ${isCreateAgentActive ? 'active' : ''}`}
                title={t('nav.newDigitalEmployee', 'New digital employee')}
                onClick={() => {
                  if (createAgentId) void handleCreateSession(createAgentId);
                  else navigate('/agents/new');
                }}
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
