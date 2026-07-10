import { useState, useEffect } from 'react';
import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../api/domains/agents';
import { taskApi } from '../api/domains/tasks';
import { activityApi } from '../api/domains/activity';
import type { ToolFailureSummary } from '../api/domains/activity';
import type { Agent, Task } from '../types';
import './Dashboard.css';

/* ────── Inline SVG Icons (monochrome) ────── */

const Icons = {
    users: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="6" cy="5" r="2.5" />
            <path d="M1.5 14v-1a3.5 3.5 0 017 0v1" />
            <circle cx="11.5" cy="5.5" r="2" />
            <path d="M14.5 14v-.5a3 3 0 00-3-3" />
        </svg>
    ),
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
    clock: (
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="8" cy="8" r="6" />
            <path d="M8 4.5V8l2.5 1.5" />
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

const priorityColor = (p: string) => {
    switch (p) {
        case 'urgent': return 'var(--error)';
        case 'high': return 'var(--warning)';
        case 'medium': return 'var(--accent-primary)';
        default: return 'var(--text-tertiary)';
    }
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

const statusColor = (s: string) => {
    switch (s) {
        case 'running': return 'var(--status-running)';
        case 'idle': return 'var(--status-idle)';
        case 'error': return 'var(--status-error)';
        case 'stopped': return 'var(--status-stopped)';
        default: return 'var(--text-tertiary)';
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
}: {
    summaries: AgentToolFailureSnapshot[];
    onSelectAgent?: (agentId: string) => void;
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

/* ────── Summary Stats Bar ────── */

function StatsBar({ agents, allTasks }: { agents: Agent[]; allTasks: Task[] }) {
    const { t } = useTranslation();
    const totalAgents = agents.length;
    const activeAgents = agents.filter(a => a.status === 'running' || a.status === 'idle').length;
    const pendingTasks = allTasks.filter(t => t.status === 'pending' || t.status === 'doing').length;
    const completedToday = allTasks.filter(t => {
        if (t.status !== 'done' || !t.completed_at) return false;
        const today = new Date();
        const completed = new Date(t.completed_at);
        return completed.toDateString() === today.toDateString();
    }).length;
    const totalTokensToday = agents.reduce((sum, a) => sum + (a.tokens_used_today || 0), 0);
    const recentlyActive = agents.filter(a => {
        if (!a.last_active_at) return false;
        return Date.now() - new Date(a.last_active_at).getTime() < 3600000;
    }).length;

    const stats = [
        { icon: Icons.users, label: t('dashboard.stats.agents'), value: totalAgents, sub: t('dashboard.stats.online', { count: activeAgents }) },
        { icon: Icons.tasks, label: t('dashboard.stats.activeTasks'), value: pendingTasks, sub: t('dashboard.stats.completedToday', { count: completedToday }) },
        { icon: Icons.zap, label: t('dashboard.stats.todayTokens'), value: formatTokens(totalTokensToday), sub: t('dashboard.stats.allAgentsTotal') },
        { icon: Icons.clock, label: t('dashboard.stats.recentlyActive'), value: recentlyActive, sub: t('dashboard.stats.lastHour') },
    ];

    return (
        <div className="dashboard-stats-bar">
            {stats.map((s, i) => (
                <div key={i} className="dashboard-stat-cell">
                    <div className="dashboard-stat-label">
                        <span className="dashboard-stat-icon">{s.icon}</span> {s.label}
                    </div>
                    <div className="dashboard-stat-value">
                        {s.value}
                    </div>
                    <div className="dashboard-stat-sub">{s.sub}</div>
                </div>
            ))}
        </div>
    );
}

/* ────── Agent Row ────── */

function AgentRow({ agent, tasks, recentActivity }: {
    agent: Agent;
    tasks: Task[];
    recentActivity: any[];
}) {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const pendingTasks = tasks.filter(t => t.status === 'pending' || t.status === 'doing');
    const latestActivity = recentActivity[0];

    // Token usage bar
    const maxTokens = agent.max_tokens_per_day || 0;
    const usedTokens = agent.tokens_used_today || 0;
    const tokenPct = maxTokens > 0 ? Math.min(100, (usedTokens / maxTokens) * 100) : 0;

    return (
        <div
            onClick={() => navigate(`/agents/${agent.id}`)}
            className="dashboard-agent-row"
        >
            {/* Agent Info */}
            <div className="dashboard-agent-info">
                <div className="dashboard-agent-avatar">
                    {Icons.bot}
                </div>
                <div className="dashboard-min0">
                    <div className="dashboard-agent-name">
                        {agent.name}
                        <span className="dashboard-agent-status" style={{ color: statusColor(agent.status) }}>
                            <span className="dashboard-status-dot" style={{ background: statusColor(agent.status) }} />
                            {statusLabel(agent.status, t)}
                        </span>
                    </div>
                    <div className="dashboard-agent-role">
                        {agent.role_description || '-'}
                    </div>
                </div>
            </div>

            {/* Latest Activity / Tasks */}
            <div className="dashboard-min0">
                {latestActivity ? (
                    <div className="dashboard-activity-line">
                        <span className="dashboard-activity-time">
                            {timeAgo(latestActivity.created_at, t)}
                        </span>
                        {latestActivity.summary}
                    </div>
                ) : (
                    <div className="dashboard-muted-row">{t('dashboard.noActivity')}</div>
                )}
                {pendingTasks.length > 0 && (
                    <div className="dashboard-task-chips">
                        {pendingTasks.slice(0, 3).map(t => (
                            <span key={t.id} className="dashboard-task-chip">
                                <span className="dashboard-priority-dot" style={{ background: priorityColor(t.priority) }} />
                                {t.title}
                            </span>
                        ))}
                        {pendingTasks.length > 3 && (
                            <span className="dashboard-task-more">
                                +{pendingTasks.length - 3}
                            </span>
                        )}
                    </div>
                )}
            </div>

            {/* Token Usage */}
            <div>
                <div className="dashboard-token-line">
                    {formatTokens(usedTokens)}
                    {maxTokens > 0 && <span className="dashboard-token-max"> / {formatTokens(maxTokens)}</span>}
                </div>
                {maxTokens > 0 ? (
                    <div className="dashboard-token-track">
                        <div
                            className="dashboard-token-fill"
                            style={{
                                width: `${tokenPct}%`,
                                background: tokenPct > 80 ? 'var(--error)' : tokenPct > 50 ? 'var(--warning)' : 'var(--text-tertiary)',
                            }}
                        />
                    </div>
                ) : (
                    <div className="dashboard-token-nolimit">{t('dashboard.noLimit')}</div>
                )}
            </div>

            {/* Last Active */}
            <div className="dashboard-agent-lastactive">
                {timeAgo(agent.last_active_at, t)}
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
                            {agent?.name || act.agent_id?.slice(0, 6)}
                        </span>
                        <span className="dashboard-activity-summary">
                            {act.summary}
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
};

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
    allTasks,
    allActivities,
    agentActivities,
    toolFailureSnapshots,
    onNavigate,
}: {
    agents: Agent[];
    isLoading: boolean;
    allTasks: Task[];
    allActivities: any[];
    agentActivities: Record<string, any[]>;
    toolFailureSnapshots: AgentToolFailureSnapshot[];
    onNavigate: (path: string) => void;
}) {
    const { t } = useTranslation();
    const hour = new Date().getHours();
    const greeting = hour < 6
        ? t('dashboard.greeting.lateNight')
        : hour < 12
            ? t('dashboard.greeting.morning')
            : hour < 18
                ? t('dashboard.greeting.afternoon')
                : t('dashboard.greeting.evening');
    const activeAgents = agents.filter(a => a.status === 'running' || a.status === 'idle');
    const pendingTasks = allTasks.filter(task => task.status === 'pending' || task.status === 'doing');
    const needsYou = pendingTasks
        .filter(task => task.priority === 'urgent' || task.priority === 'high' || task.status === 'pending')
        .slice(0, 4);
    const inProgress = [
        ...pendingTasks.filter(task => task.status === 'doing').slice(0, 4).map(task => ({
            id: task.id,
            title: task.title,
            detail: agents.find(agent => agent.id === task.agent_id)?.name || task.agent_id,
            badge: t('dashboard.labels.task', 'Task'),
            to: `/agents/${task.agent_id}`,
        })),
        ...activeAgents.slice(0, Math.max(0, 4 - pendingTasks.filter(task => task.status === 'doing').length)).map(agent => ({
            id: agent.id,
            title: agent.name,
            detail: agent.role_description || t('employees.noRole', 'No role description yet'),
            badge: statusLabel(agent.status, t),
            to: `/agents/${agent.id}`,
        })),
    ].slice(0, 4);
    const totalTokensToday = agents.reduce((sum, agent) => sum + (agent.tokens_used_today || 0), 0);
    const totalTokensMonth = agents.reduce((sum, agent) => sum + (agent.tokens_used_month || 0), 0);
    const completedToday = allTasks.filter(task => {
        if (task.status !== 'done' || !task.completed_at) return false;
        return new Date(task.completed_at).toDateString() === new Date().toDateString();
    }).length;
    const latestActivities = allActivities.slice(0, 5);
    const actionCards: WorkspaceHomeAction[] = [
        {
            title: t('dashboard.home.assignWork', 'Assign work'),
            description: t('dashboard.home.assignWorkDesc', 'Choose a digital employee and start a new session.'),
            to: '/agents?assign=true',
            icon: Icons.tasks,
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
                        {t('dashboard.home.summary', '{{attention}} items need confirmation, {{active}} digital employees are working.', {
                            attention: needsYou.length,
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
                        data-navigation-target={action.to}
                        onClick={() => onNavigate(action.to)}
                    >
                        <span className="workspace-action-icon">{action.icon}</span>
                        <strong>{action.title}</strong>
                        <small>{action.description}</small>
                    </button>
                ))}
            </section>

            <div className="workspace-home-grid">
                <section className="workspace-panel workspace-panel-wide">
                    <SectionHeader eyebrow={t('dashboard.home.needsYouEyebrow', 'Needs you')} title={t('dashboard.home.needsYou', 'Needs you')} />
                    {needsYou.length === 0 ? (
                        <p className="workspace-muted">{t('dashboard.home.noNeedsYou', 'No pending confirmations right now.')}</p>
                    ) : (
                        <div className="workspace-list">
                            {needsYou.map(task => (
                                <button key={task.id} type="button" className="workspace-list-row" onClick={() => onNavigate(`/agents/${task.agent_id}`)}>
                                    <span className={`workspace-priority-dot ${task.priority}`} />
                                    <span>
                                        <strong>{task.title}</strong>
                                        <small>{agents.find(agent => agent.id === task.agent_id)?.name || task.agent_id}</small>
                                    </span>
                                    <span className="workspace-row-badge">{t('dashboard.labels.task', 'Task')}</span>
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
                            <span>{t('dashboard.stats.activeTasks')}</span>
                            <strong>{pendingTasks.length}</strong>
                        </div>
                        <div>
                            <span>{t('dashboard.stats.completedToday', { count: completedToday })}</span>
                            <strong>{completedToday}</strong>
                        </div>
                    </div>
                </section>

                <section className="workspace-panel workspace-panel-wide">
                    <SectionHeader
                        eyebrow={t('dashboard.home.inProgressEyebrow', 'In progress')}
                        title={t('dashboard.home.inProgress', 'In progress')}
                        action={<button className="workspace-text-action" onClick={() => onNavigate('/automations')}>{t('dashboard.home.viewAllTasks', 'View all')}</button>}
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

    const [allTasks, setAllTasks] = useState<Task[]>([]);
    const [allActivities, setAllActivities] = useState<any[]>([]);
    const [agentActivities, setAgentActivities] = useState<Record<string, any[]>>({});
    const [agentToolFailures, setAgentToolFailures] = useState<Record<string, ToolFailureSummary>>({});

    useEffect(() => {
        if (agents.length === 0) return;
        const fetchData = async () => {
            try {
                const taskResults = await Promise.allSettled(agents.map(a => taskApi.list(a.id)));
                const tasks: Task[] = [];
                taskResults.forEach(r => { if (r.status === 'fulfilled') tasks.push(...r.value); });
                setAllTasks(tasks);
            } catch (e) { console.error('Failed to fetch tasks:', e); }

            try {
                const actResults = await Promise.allSettled(agents.map(a => activityApi.list(a.id, 5)));
                const activities: any[] = [];
                const perAgent: Record<string, any[]> = {};
                actResults.forEach((r, i) => {
                    if (r.status === 'fulfilled') {
                        perAgent[agents[i].id] = r.value;
                        activities.push(...r.value.map((v: any) => ({ ...v, agent_id: agents[i].id })));
                    }
                });
                activities.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
                setAllActivities(activities.slice(0, 20));
                setAgentActivities(perAgent);
            } catch (e) { console.error('Failed to fetch activities:', e); }

            try {
                const summaryResults = await Promise.allSettled(agents.map(a => activityApi.getToolFailureSummary(a.id, 24, 200)));
                const perAgentSummary: Record<string, ToolFailureSummary> = {};
                summaryResults.forEach((r, i) => {
                    if (r.status === 'fulfilled') {
                        perAgentSummary[agents[i].id] = r.value;
                    }
                });
                setAgentToolFailures(perAgentSummary);
            } catch (e) { console.error('Failed to fetch tool failure summaries:', e); }
        };
        fetchData();
        const interval = setInterval(fetchData, 30000);
        return () => clearInterval(interval);
    }, [agents.map(a => a.id).join(',')]);

    const toolFailureSnapshots = agents
        .filter(agent => agentToolFailures[agent.id])
        .map(agent => ({
            agentId: agent.id,
            agentName: agent.name,
            summary: agentToolFailures[agent.id],
        }));

    return (
        <DashboardHomeShell
            agents={agents}
            isLoading={isLoading}
            allTasks={allTasks}
            allActivities={allActivities}
            agentActivities={agentActivities}
            toolFailureSnapshots={toolFailureSnapshots}
            onNavigate={navigate}
        />
    );
}
