import { useState, useEffect } from 'react';
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../api/domains/agents';
import { dashboardApi } from '../api/domains/dashboard';
import { chatApi, type ChatSession } from '../api/domains/chat';
import type { ToolFailureSummary } from '../api/domains/activity';
import type { Agent } from '../types';
import { activityDisplaySummary } from './agent-detail/activityDisplay';
import {
    buildAssignmentHandoff,
    buildAssignmentSessionTitle,
    type AssignmentIntent,
} from './assignmentHandoff';
import './Dashboard.css';

/* ────── Inline SVG Icons (monochrome) ────── */

const Icons = {
    tasks: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="2" y="2" width="12" height="12" rx="2" />
            <path d="M5.5 8l2 2 3.5-3.5" />
        </svg>
    ),
    zap: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8.5 1.5L3 9h4.5l-.5 5.5L13 7H8.5l.5-5.5z" />
        </svg>
    ),
    activity: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M1 8h3l2-5 3 10 2-5h4" />
        </svg>
    ),
    plus: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M8 3v10M3 8h10" />
        </svg>
    ),
    bot: (
        <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="5" width="12" height="10" rx="2" />
            <circle cx="7" cy="10" r="1" fill="currentColor" stroke="none" />
            <circle cx="11" cy="10" r="1" fill="currentColor" stroke="none" />
            <path d="M9 2v3M6 2h6" />
        </svg>
    ),
};

/* ────── Helpers ────── */

const timeAgo = (dateStr: string | undefined, t: any) => {
    if (!dateStr) return '-';
    const diff = Date.now() - new Date(dateStr).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return t('dashboard.justNow');
    if (mins < 60) return t('dashboard.minutesAgo', { count: mins });
    const hours = Math.floor(mins / 60);
    if (hours < 24) return t('dashboard.hoursAgo', { count: hours });
    return t('dashboard.daysAgo', { count: Math.floor(hours / 24) });
};

const statusLabel = (s: string, t: any) => {
    switch (s) {
        case 'running': return t('dashboard.status.running');
        case 'idle': return t('dashboard.status.idle');
        case 'stopped': return t('dashboard.status.stopped');
        case 'error': return t('dashboard.status.error');
        case 'creating': return t('dashboard.status.creating');
        case 'disconnected': return t('dashboard.status.disconnected');
        default: return s;
    }
};

const formatTokens = (n: number) => {
    if (!n) return '0';
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
    return String(n);
};

type CountRow = {
    label: string;
    count: number;
};

type AgentCountRow = CountRow & {
    agentId: string;
    agentName: string;
};

export type AgentToolFailureSnapshot = {
    agentId: string;
    agentName: string;
    summary: ToolFailureSummary;
};

export type CrossAgentToolFailureOverview = {
    totalErrors: number;
    byAgent: AgentCountRow[];
    byTool: CountRow[];
    byProvider: CountRow[];
    byErrorClass: CountRow[];
    byHttpStatus: CountRow[];
};

const sortCountRows = <T extends { count: number }>(rows: T[]) =>
    rows.sort((a, b) => b.count - a.count);

const accumulateCount = (target: Map<string, number>, label: string | undefined, count: number) => {
    if (!label || count <= 0) return;
    target.set(label, (target.get(label) || 0) + count);
};

const toCountRows = (counts: Map<string, number>): CountRow[] =>
    sortCountRows(Array.from(counts.entries()).map(([label, count]) => ({ label, count })));

export function summarizeCrossAgentToolFailures(
    summaries: AgentToolFailureSnapshot[],
): CrossAgentToolFailureOverview {
    const toolCounts = new Map<string, number>();
    const providerCounts = new Map<string, number>();
    const errorClassCounts = new Map<string, number>();
    const httpStatusCounts = new Map<string, number>();

    const byAgent = sortCountRows(
        summaries
            .filter(({ summary }) => summary.total_errors > 0)
            .map(({ agentId, agentName, summary }) => ({
                agentId,
                agentName,
                label: agentName,
                count: summary.total_errors,
            })),
    );

    summaries.forEach(({ summary }) => {
        summary.by_tool.forEach(row => accumulateCount(toolCounts, row.tool_name, row.count));
        summary.by_provider.forEach(row => accumulateCount(providerCounts, row.provider, row.count));
        summary.by_error_class.forEach(row => accumulateCount(errorClassCounts, row.error_class, row.count));
        summary.by_http_status.forEach(row => accumulateCount(httpStatusCounts, row.http_status ? String(row.http_status) : undefined, row.count));
    });

    return {
        totalErrors: summaries.reduce((sum, { summary }) => sum + summary.total_errors, 0),
        byAgent,
        byTool: toCountRows(toolCounts),
        byProvider: toCountRows(providerCounts),
        byErrorClass: toCountRows(errorClassCounts),
        byHttpStatus: toCountRows(httpStatusCounts),
    };
}

export function ToolFailureOverview({
    summaries,
    onSelectAgent,
    truncated = false,
}: {
    summaries: AgentToolFailureSnapshot[];
    onSelectAgent?: (agentId: string) => void;
    truncated?: boolean;
}) {
    const { t } = useTranslation();
    const overview = summarizeCrossAgentToolFailures(summaries);

    const renderCountList = <T extends CountRow>(
        title: string,
        rows: T[],
        emptyLabel: string,
        rowRenderer?: (row: T, index: number) => React.ReactNode,
    ) => (
        <div className="dashboard-count-group">
            <div className="dashboard-count-title">
                {title}
            </div>
            {rows.length === 0 ? (
                <div className="dashboard-count-empty">{emptyLabel}</div>
            ) : (
                <div className="dashboard-pill-row">
                    {rows.slice(0, 5).map((row, index) => rowRenderer ? rowRenderer(row, index) : (
                        <span key={`${row.label}-${index}`} className="dashboard-pill">
                            <span>{row.label}</span>
                            <span className="dashboard-pill-count">{row.count}</span>
                        </span>
                    ))}
                </div>
            )}
        </div>
    );

    return (
        <div className="dashboard-failures-card">
            <div className="dashboard-failures-head">
                <h3 className="dashboard-failures-title">
                    <span className="dashboard-failures-title-icon">{Icons.activity}</span>
                    {t('dashboard.toolFailuresTitle')}
                </h3>
                <span className="dashboard-failures-window">
                    {t('dashboard.toolFailuresWindow', { count: 24 })}: {overview.totalErrors}
                    {truncated ? ` · ${t('dashboard.toolFailuresSampled', 'latest bounded sample')}` : ''}
                </span>
            </div>
            <div className="dashboard-failures-body">
                {renderCountList(
                    t('dashboard.topFailingAgents'),
                    overview.byAgent,
                    t('dashboard.noToolFailures'),
                    (row, index) => (
                        <button
                            key={`${row.label}-${index}`}
                            type="button"
                            className={`dashboard-pill dashboard-pill-btn${onSelectAgent ? ' dashboard-pill-btn--active' : ''}`}
                            onClick={() => onSelectAgent?.(row.agentId)}
                        >
                            <span>{row.label}</span>
                            <span className="dashboard-pill-count">{row.count}</span>
                        </button>
                    ),
                )}
                {renderCountList(t('dashboard.topFailingTools'), overview.byTool, t('dashboard.noToolFailures'))}
                {renderCountList(t('dashboard.topProviders'), overview.byProvider, t('dashboard.noToolFailures'))}
                {renderCountList(t('dashboard.topErrorClasses'), overview.byErrorClass, t('dashboard.noToolFailures'))}
                {renderCountList(t('dashboard.topHttpStatuses'), overview.byHttpStatus, t('dashboard.noToolFailures'))}
            </div>
        </div>
    );
}

/* ────── Recent Activity Feed ────── */


function ActivityFeed({ activities, agents }: { activities: any[]; agents: Agent[] }) {
    const { t } = useTranslation();
    const agentMap = new Map(agents.map(a => [a.id, a]));

    if (activities.length === 0) {
        return (
            <div className="dashboard-activity-empty">
                {t('dashboard.noActivity')}
            </div>
        );
    }

    return (
        <div className="dashboard-activity-feed">
            {activities.map((act, i) => {
                const agent = agentMap.get(act.agent_id);
                return (
                    <div key={act.id || i} className="dashboard-activity-row">
                        <span className="dashboard-activity-ts">
                            {timeAgo(act.created_at, t)}
                        </span>
                        <span className="dashboard-tag">
                            {agent?.name || t('dashboard.activity.unknownAgent', 'Digital employee')}
                        </span>
                        <span className="dashboard-activity-summary">
                            {activityDisplaySummary(act, t)}
                        </span>
                    </div>
                );
            })}
        </div>
    );
}

type WorkspaceHomeAction = {
    title: string;
    description: string;
    to: string;
    icon: ReactNode;
    kind?: 'assign';
};

export interface AssignWorkRequest {
    agentId: string;
    content: string;
    intent: AssignmentIntent;
}

function SectionHeader({ eyebrow, title, action }: { eyebrow: string; title: string; action?: React.ReactNode }) {
    return (
        <div className="workspace-section-header">
            <div>
                <span className="workspace-section-eyebrow">{eyebrow}</span>
                <h2>{title}</h2>
            </div>
            {action}
        </div>
    );
}

function EmptyWorkspaceState({ onNavigate }: { onNavigate: (path: string) => void }) {
    const { t } = useTranslation();
    return (
        <section className="workspace-empty-state">
            <div className="workspace-empty-icon">{Icons.bot}</div>
            <h2>{t('dashboard.emptyTitle')}</h2>
            <p>{t('dashboard.emptyDesc')}</p>
            <button className="btn btn-primary" onClick={() => onNavigate('/agents/new')}>
                {Icons.plus} {t('dashboard.createFirst')}
            </button>
            <small>{t('dashboard.emptyHint')}</small>
        </section>
    );
}

export function DashboardHomeShell({
    agents,
    isLoading,
    recentSessions,
    allActivities,
    toolFailureSnapshots,
    sessionCount,
    toolFailuresTruncated = false,
    onNavigate,
    onAssignWork,
    initialAssignWorkOpen = false,
}: {
    agents: Agent[];
    isLoading: boolean;
    recentSessions: ChatSession[];
    allActivities: any[];
    toolFailureSnapshots: AgentToolFailureSnapshot[];
    sessionCount?: number;
    toolFailuresTruncated?: boolean;
    onNavigate: (path: string) => void;
    onAssignWork?: (request: AssignWorkRequest) => Promise<void>;
    initialAssignWorkOpen?: boolean;
}) {
    const { t } = useTranslation();
    const [assignWorkOpen, setAssignWorkOpen] = useState(initialAssignWorkOpen);
    const [assignAgentId, setAssignAgentId] = useState(agents[0]?.id || '');
    const [assignContent, setAssignContent] = useState('');
    const [assignIntent, setAssignIntent] = useState<AssignmentIntent>('execute');
    const [assignSubmitting, setAssignSubmitting] = useState(false);
    const [assignError, setAssignError] = useState('');
    useEffect(() => {
        if (agents.some((agent) => agent.id === assignAgentId)) return;
        setAssignAgentId(agents[0]?.id || '');
    }, [agents, assignAgentId]);
    const submitAssignment = async () => {
        const content = assignContent.trim();
        if (!assignAgentId || !content || !onAssignWork) {
            setAssignError(t('dashboard.home.assignRequired', 'Choose an employee and describe the work.'));
            return;
        }
        setAssignSubmitting(true);
        setAssignError('');
        try {
            await onAssignWork({ agentId: assignAgentId, content, intent: assignIntent });
        } catch (error) {
            setAssignError(error instanceof Error ? error.message : t('dashboard.home.assignFailed', 'Could not start this work.'));
        } finally {
            setAssignSubmitting(false);
        }
    };
    const hour = new Date().getHours();
    const greeting = hour < 6
        ? t('dashboard.greeting.lateNight')
        : hour < 12
            ? t('dashboard.greeting.morning')
            : hour < 18
                ? t('dashboard.greeting.afternoon')
                : t('dashboard.greeting.evening');
    const activeAgents = agents.filter(a => a.status === 'running' || a.status === 'idle');
    const recentWork = [...recentSessions]
        .sort((left, right) => new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime())
        .slice(0, 4);
    const inProgress = activeAgents.slice(0, 4).map(agent => ({
            id: agent.id,
            title: agent.name,
            detail: agent.role_description || t('employees.noRole', 'No role description yet'),
            badge: statusLabel(agent.status, t),
            to: `/agents/${agent.id}`,
        }));
    const totalTokensToday = agents.reduce((sum, agent) => sum + (agent.tokens_used_today || 0), 0);
    const totalTokensMonth = agents.reduce((sum, agent) => sum + (agent.tokens_used_month || 0), 0);
    const latestActivities = allActivities.slice(0, 5);
    const actionCards: WorkspaceHomeAction[] = [
        {
            title: t('dashboard.home.assignWork', 'Assign work'),
            description: t('dashboard.home.assignWorkDesc', 'Choose a digital employee and start a new session.'),
            to: '/agents?assign=true',
            icon: Icons.tasks,
            kind: 'assign',
        },
        {
            title: t('dashboard.home.automation', 'Automation'),
            description: t('dashboard.home.automationDesc', 'Review scheduled work and workflow candidates.'),
            to: '/automations',
            icon: Icons.zap,
        },
        {
            title: t('dashboard.home.knowledge', 'Knowledge'),
            description: t('dashboard.home.knowledgeDesc', 'Search and manage your personal knowledge base.'),
            to: '/knowledge',
            icon: Icons.activity,
        },
        {
            title: t('dashboard.home.localAgents', 'Local Agents'),
            description: t('dashboard.home.localAgentsDesc', 'Connect and continue work on local runtimes.'),
            to: '/local-agents',
            icon: Icons.bot,
        },
    ];

    if (isLoading) {
        return (
            <main className="workspace-home">
                <div className="workspace-loading">{t('common.loading')}</div>
            </main>
        );
    }

    if (agents.length === 0) {
        return (
            <main className="workspace-home">
                <EmptyWorkspaceState onNavigate={onNavigate} />
            </main>
        );
    }

    return (
        <main className="workspace-home">
            <header className="workspace-home-hero">
                <div>
                    <span className="workspace-home-kicker">{t('dashboard.home.eyebrow', 'My Workspace')}</span>
                    <h1>{greeting}</h1>
                    <p>
                        {t('dashboard.home.summary', '{{recent}} recent sessions, {{active}} digital employees are available.', {
                            recent: recentWork.length,
                            active: activeAgents.length,
                        })}
                    </p>
                </div>
                <button className="btn btn-primary" onClick={() => onNavigate('/agents/new')}>
                    {Icons.plus} {t('nav.newAgent')}
                </button>
            </header>

            <section className="workspace-action-grid" aria-label={t('dashboard.home.quickActions', 'Quick actions')}>
                {actionCards.map(action => (
                    <button
                        key={action.title}
                        type="button"
                        className="workspace-action-card"
                        data-navigation-target={action.kind === 'assign' ? 'assign-work-dialog' : action.to}
                        onClick={() => action.kind === 'assign' ? setAssignWorkOpen(true) : onNavigate(action.to)}
                    >
                        <span className="workspace-action-icon">{action.icon}</span>
                        <strong>{action.title}</strong>
                        <small>{action.description}</small>
                    </button>
                ))}
            </section>

            {assignWorkOpen && (
                <div className="workspace-assign-backdrop" role="presentation">
                    <section className="workspace-assign-dialog" role="dialog" aria-modal="true" aria-label={t('dashboard.home.assignWork', 'Assign work')}>
                        <div className="workspace-assign-header">
                            <div>
                                <span className="workspace-home-kicker">{t('dashboard.home.newSession', 'New session')}</span>
                                <h2>{t('dashboard.home.assignWork', 'Assign work')}</h2>
                            </div>
                            <button type="button" className="workspace-assign-close" aria-label={t('common.close', 'Close')} onClick={() => setAssignWorkOpen(false)}>×</button>
                        </div>
                        <label className="workspace-assign-field">
                            <span>{t('dashboard.home.assignTo', 'Assign to')}</span>
                            <select value={assignAgentId} onChange={(event) => setAssignAgentId(event.target.value)}>
                                {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
                            </select>
                        </label>
                        <label className="workspace-assign-field">
                            <span>{t('dashboard.home.request', 'Request')}</span>
                            <textarea
                                rows={6}
                                value={assignContent}
                                onChange={(event) => setAssignContent(event.target.value)}
                                placeholder={t('dashboard.home.requestPlaceholder', 'Describe the outcome you want this employee to deliver.')}
                            />
                        </label>
                        <div className="workspace-assign-field">
                            <span>{t('dashboard.home.executionMode', 'Execution mode')}</span>
                            <div className="workspace-assign-intents" role="radiogroup" aria-label={t('dashboard.home.executionMode', 'Execution mode')}>
                                {([
                                    ['execute', t('dashboard.home.executeNow', 'Execute now')],
                                    ['plan', t('dashboard.home.planFirst', 'Plan first')],
                                    ['goal', t('dashboard.home.runAsGoal', 'Run as goal')],
                                ] as Array<[AssignmentIntent, string]>).map(([intent, label]) => (
                                    <button
                                        key={intent}
                                        type="button"
                                        role="radio"
                                        aria-checked={assignIntent === intent}
                                        className={assignIntent === intent ? 'is-selected' : ''}
                                        onClick={() => setAssignIntent(intent)}
                                    >
                                        {label}
                                    </button>
                                ))}
                            </div>
                        </div>
                        {assignError && <p className="workspace-assign-error" role="alert">{assignError}</p>}
                        <div className="workspace-assign-actions">
                            <button type="button" className="btn btn-ghost" onClick={() => setAssignWorkOpen(false)}>{t('common.cancel', 'Cancel')}</button>
                            <button type="button" className="btn btn-primary" disabled={assignSubmitting || !assignContent.trim()} onClick={() => void submitAssignment()}>
                                {assignSubmitting ? t('dashboard.home.starting', 'Starting…') : t('dashboard.home.openSession', 'Open session')}
                            </button>
                        </div>
                    </section>
                </div>
            )}

            <div className="workspace-home-grid">
                <section className="workspace-panel workspace-panel-wide">
                    <SectionHeader eyebrow={t('dashboard.home.recentWorkEyebrow', 'Sessions')} title={t('dashboard.home.recentWork', 'Recent work')} />
                    {recentWork.length === 0 ? (
                        <p className="workspace-muted">{t('dashboard.home.noRecentWork', 'No sessions yet. Assign work to get started.')}</p>
                    ) : (
                        <div className="workspace-list">
                            {recentWork.map(session => (
                                <button key={session.id} type="button" className="workspace-list-row" onClick={() => onNavigate(`/agents/${session.agent_id}/sessions/${session.id}`)}>
                                    <span className="workspace-status-dot" />
                                    <span>
                                        <strong>{session.title || t('agent.chat.session', 'Session')}</strong>
                                        <small>{agents.find(agent => agent.id === session.agent_id)?.name || t('dashboard.home.digitalEmployee', 'Digital employee')}</small>
                                    </span>
                                    <span className="workspace-row-badge">{timeAgo(session.updated_at, t)}</span>
                                </button>
                            ))}
                        </div>
                    )}
                </section>

                <section className="workspace-panel">
                    <SectionHeader eyebrow={t('dashboard.home.thisMonthEyebrow', 'This month')} title={t('dashboard.home.thisMonth', 'This month')} />
                    <div className="workspace-usage-stack">
                        <div>
                            <span>{t('dashboard.stats.todayTokens')}</span>
                            <strong>{formatTokens(totalTokensToday)}</strong>
                        </div>
                        <div>
                            <span>{t('dashboard.stats.allAgentsTotal')}</span>
                            <strong>{formatTokens(totalTokensMonth)}</strong>
                        </div>
                        <div>
                            <span>{t('dashboard.home.sessions', 'Sessions')}</span>
                            <strong>{sessionCount ?? recentSessions.length}</strong>
                        </div>
                        <div>
                            <span>{t('dashboard.stats.agents', 'Digital employees')}</span>
                            <strong>{agents.length}</strong>
                        </div>
                    </div>
                </section>

                <section className="workspace-panel workspace-panel-wide">
                    <SectionHeader
                        eyebrow={t('dashboard.home.inProgressEyebrow', 'In progress')}
                        title={t('dashboard.home.inProgress', 'Available employees')}
                        action={<button className="workspace-text-action" onClick={() => onNavigate('/agents')}>{t('dashboard.home.viewAllEmployees', 'View all')}</button>}
                    />
                    <div className="workspace-list">
                        {inProgress.length === 0 ? (
                            <p className="workspace-muted">{t('dashboard.home.noInProgress', 'No active work is running.')}</p>
                        ) : inProgress.map(row => (
                            <button key={row.id} type="button" className="workspace-list-row" onClick={() => onNavigate(row.to)}>
                                <span className="workspace-status-dot" />
                                <span>
                                    <strong>{row.title}</strong>
                                    <small>{row.detail}</small>
                                </span>
                                <span className="workspace-row-badge">{row.badge}</span>
                            </button>
                        ))}
                    </div>
                </section>

                <section className="workspace-panel">
                    <SectionHeader eyebrow={t('dashboard.home.activityEyebrow', 'Activity')} title={t('dashboard.home.activity', 'Activity')} />
                    {latestActivities.length === 0 ? (
                        <p className="workspace-muted">{t('dashboard.noActivity')}</p>
                    ) : (
                        <ActivityFeed activities={latestActivities} agents={agents} />
                    )}
                </section>
            </div>

            {toolFailureSnapshots.length > 0 && (
                <ToolFailureOverview
                    summaries={toolFailureSnapshots}
                    truncated={toolFailuresTruncated}
                    onSelectAgent={(agentId) => onNavigate(`/agents/${agentId}`)}
                />
            )}
        </main>
    );
}

/* ────── Main Dashboard ────── */

export default function Dashboard() {
    const navigate = useNavigate();
    const currentTenant = localStorage.getItem('current_tenant_id') || '';

    const { data: agents = [], isLoading } = useQuery({
        queryKey: ['agents', currentTenant],
        queryFn: () => agentApi.list(currentTenant || undefined),
        staleTime: 30000,
        refetchInterval: 60000,
    });

    const { data: overview, isLoading: isOverviewLoading } = useQuery({
        queryKey: ['dashboard-overview', currentTenant],
        queryFn: () => dashboardApi.getOverview(currentTenant || undefined),
        enabled: agents.length > 0,
        staleTime: 15000,
        refetchInterval: 30000,
    });

    const recentSessions = overview?.recent_sessions || [];
    const allActivities = overview?.recent_activities || [];
    const agentToolFailures: Record<string, ToolFailureSummary> = overview?.tool_failures || {};

    const toolFailureSnapshots = agents
        .filter(agent => agentToolFailures[agent.id])
        .map(agent => ({
            agentId: agent.id,
            agentName: agent.name,
            summary: agentToolFailures[agent.id],
        }));

    const assignWork = async ({ agentId, content, intent }: AssignWorkRequest) => {
        const handoff = buildAssignmentHandoff(content, intent);
        const session = await chatApi.createSession(agentId, buildAssignmentSessionTitle(handoff.content));
        navigate(`/agents/${agentId}/sessions/${session.id}`, {
            state: { assignmentDraft: handoff },
        });
    };

    return (
        <DashboardHomeShell
            agents={agents}
            isLoading={isLoading || (agents.length > 0 && isOverviewLoading)}
            recentSessions={recentSessions}
            allActivities={allActivities}
            toolFailureSnapshots={toolFailureSnapshots}
            sessionCount={overview?.session_count}
            toolFailuresTruncated={overview?.query_evidence.failure_rows_truncated || false}
            onNavigate={navigate}
            onAssignWork={assignWork}
        />
    );
}
