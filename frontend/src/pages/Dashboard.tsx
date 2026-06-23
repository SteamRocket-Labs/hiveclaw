import { useState, useEffect } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../api/domains/agents';
import { taskApi } from '../api/domains/tasks';
import { activityApi } from '../api/domains/activity';
import type { ToolFailureSummary } from '../api/domains/activity';
import type { Agent, Task } from '../types';

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

    const pillStyle: CSSProperties = {
        display: 'inline-flex',
        alignItems: 'center',
        gap: '6px',
        padding: '4px 8px',
        borderRadius: 'var(--radius-sm)',
        background: 'var(--bg-tertiary)',
        color: 'var(--text-secondary)',
        fontSize: '12px',
        fontWeight: 500,
        border: '1px solid var(--border-subtle)',
    };

    const renderCountList = <T extends CountRow>(
        title: string,
        rows: T[],
        emptyLabel: string,
        rowRenderer?: (row: T, index: number) => React.ReactNode,
    ) => (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                {title}
            </div>
            {rows.length === 0 ? (
                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{emptyLabel}</div>
            ) : (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
                    {rows.slice(0, 5).map((row, index) => rowRenderer ? rowRenderer(row, index) : (
                        <span key={`${row.label}-${index}`} style={pillStyle}>
                            <span>{row.label}</span>
                            <span style={{ color: 'var(--text-tertiary)' }}>{row.count}</span>
                        </span>
                    ))}
                </div>
            )}
        </div>
    );

    return (
        <div style={{
            border: '1px solid var(--border-subtle)',
            borderRadius: 'var(--radius-lg)',
            overflow: 'hidden',
            marginBottom: '32px',
        }}>
            <div style={{
                padding: '12px 16px',
                borderBottom: '1px solid var(--border-subtle)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
            }}>
                <h3 style={{
                    margin: 0,
                    fontSize: '13px',
                    fontWeight: 500,
                    display: 'flex',
                    alignItems: 'center',
                    gap: '6px',
                    color: 'var(--text-secondary)',
                }}>
                    <span style={{ display: 'flex', opacity: 0.6 }}>{Icons.activity}</span>
                    {t('dashboard.toolFailuresTitle')}
                </h3>
                <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    {t('dashboard.toolFailuresWindow', { count: 24 })}: {overview.totalErrors}
                </span>
            </div>
            <div style={{ padding: '16px', display: 'grid', gap: '16px' }}>
                {renderCountList(
                    t('dashboard.topFailingAgents'),
                    overview.byAgent,
                    t('dashboard.noToolFailures'),
                    (row, index) => (
                        <button
                            key={`${row.label}-${index}`}
                            type="button"
                            style={{
                                ...pillStyle,
                                cursor: onSelectAgent ? 'pointer' : 'default',
                            }}
                            onClick={() => onSelectAgent?.(row.agentId)}
                        >
                            <span>{row.label}</span>
                            <span style={{ color: 'var(--text-tertiary)' }}>{row.count}</span>
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
        <div style={{
            display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '1px',
            background: 'var(--border-subtle)', borderRadius: 'var(--radius-lg)',
            overflow: 'hidden', marginBottom: '24px',
            border: '1px solid var(--border-subtle)',
        }}>
            {stats.map((s, i) => (
                <div key={i} style={{
                    background: 'var(--bg-secondary)', padding: '16px 20px',
                    display: 'flex', flexDirection: 'column', gap: '2px',
                }}>
                    <div style={{
                        fontSize: '12px', color: 'var(--text-tertiary)',
                        display: 'flex', alignItems: 'center', gap: '6px',
                        marginBottom: '4px',
                    }}>
                        <span style={{ display: 'flex', opacity: 0.7 }}>{s.icon}</span> {s.label}
                    </div>
                    <div style={{ fontSize: '24px', fontWeight: 600, color: 'var(--text-primary)', letterSpacing: '-0.02em' }}>
                        {s.value}
                    </div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{s.sub}</div>
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
            style={{
                display: 'grid',
                gridTemplateColumns: '220px 1fr 150px 100px',
                alignItems: 'center', gap: '16px',
                padding: '12px 16px',
                borderRadius: 'var(--radius-md)',
                cursor: 'pointer', transition: 'background 120ms ease',
            }}
            onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = 'var(--bg-hover)'; }}
            onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = 'transparent'; }}
        >
            {/* Agent Info */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 }}>
                <div style={{
                    width: '32px', height: '32px', borderRadius: 'var(--radius-md)',
                    background: 'var(--bg-tertiary)', border: '1px solid var(--border-subtle)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    color: 'var(--text-tertiary)', flexShrink: 0,
                }}>
                    {Icons.bot}
                </div>
                <div style={{ minWidth: 0 }}>
                    <div style={{
                        fontWeight: 500, fontSize: '13px', display: 'flex',
                        alignItems: 'center', gap: '8px', color: 'var(--text-primary)',
                    }}>
                        {agent.name}
                        <span style={{
                            display: 'inline-flex', alignItems: 'center', gap: '4px',
                            fontSize: '11px', fontWeight: 400,
                            color: statusColor(agent.status),
                        }}>
                            <span style={{
                                width: '6px', height: '6px', borderRadius: '50%',
                                background: statusColor(agent.status),
                                display: 'inline-block',
                            }} />
                            {statusLabel(agent.status, t)}
                        </span>
                    </div>
                    <div style={{
                        fontSize: '12px', color: 'var(--text-tertiary)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                        {agent.role_description || '-'}
                    </div>
                </div>
            </div>

            {/* Latest Activity / Tasks */}
            <div style={{ minWidth: 0 }}>
                {latestActivity ? (
                    <div style={{
                        fontSize: '12px', color: 'var(--text-secondary)',
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                        <span style={{ color: 'var(--text-tertiary)', marginRight: '6px' }}>
                            {timeAgo(latestActivity.created_at, t)}
                        </span>
                        {latestActivity.summary}
                    </div>
                ) : (
                    <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('dashboard.noActivity')}</div>
                )}
                {pendingTasks.length > 0 && (
                    <div style={{ display: 'flex', gap: '4px', marginTop: '4px', flexWrap: 'wrap' }}>
                        {pendingTasks.slice(0, 3).map(t => (
                            <span key={t.id} style={{
                                fontSize: '11px', padding: '1px 6px',
                                borderRadius: 'var(--radius-sm)', background: 'var(--bg-tertiary)',
                                color: 'var(--text-secondary)', maxWidth: '140px',
                                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                display: 'inline-flex', alignItems: 'center', gap: '3px',
                            }}>
                                <span style={{ width: '4px', height: '4px', borderRadius: '50%', background: priorityColor(t.priority), flexShrink: 0 }} />
                                {t.title}
                            </span>
                        ))}
                        {pendingTasks.length > 3 && (
                            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', padding: '1px 4px' }}>
                                +{pendingTasks.length - 3}
                            </span>
                        )}
                    </div>
                )}
            </div>

            {/* Token Usage */}
            <div>
                <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '3px' }}>
                    {formatTokens(usedTokens)}
                    {maxTokens > 0 && <span style={{ opacity: 0.6 }}> / {formatTokens(maxTokens)}</span>}
                </div>
                {maxTokens > 0 ? (
                    <div style={{
                        height: '3px', background: 'var(--bg-tertiary)',
                        borderRadius: '2px', overflow: 'hidden',
                    }}>
                        <div style={{
                            height: '100%', borderRadius: '2px',
                            width: `${tokenPct}%`,
                            background: tokenPct > 80 ? 'var(--error)' : tokenPct > 50 ? 'var(--warning)' : 'var(--text-tertiary)',
                            transition: 'width 0.3s',
                        }} />
                    </div>
                ) : (
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', opacity: 0.5 }}>{t('dashboard.noLimit')}</div>
                )}
            </div>

            {/* Last Active */}
            <div style={{ textAlign: 'right', fontSize: '12px', color: 'var(--text-tertiary)' }}>
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
            <div style={{ textAlign: 'center', padding: '32px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                {t('dashboard.noActivity')}
            </div>
        );
    }

    return (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
            {activities.map((act, i) => {
                const agent = agentMap.get(act.agent_id);
                return (
                    <div key={act.id || i} style={{
                        display: 'flex', gap: '12px', padding: '7px 12px',
                        fontSize: '13px', alignItems: 'flex-start',
                    }}>
                        <span style={{
                            color: 'var(--text-tertiary)', whiteSpace: 'nowrap',
                            fontFamily: 'var(--font-mono)', fontSize: '11px',
                            minWidth: '52px', paddingTop: '2px',
                        }}>
                            {timeAgo(act.created_at, t)}
                        </span>
                        <span style={{
                            fontSize: '11px', padding: '1px 6px',
                            borderRadius: 'var(--radius-sm)', background: 'var(--bg-tertiary)',
                            color: 'var(--text-secondary)', whiteSpace: 'nowrap', flexShrink: 0,
                            fontWeight: 500,
                        }}>
                            {agent?.name || act.agent_id?.slice(0, 6)}
                        </span>
                        <span style={{
                            color: 'var(--text-secondary)', flex: 1,
                            overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                        }}>
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
            title: t('dashboard.home.createViaHr', 'Create via HR'),
            description: t('dashboard.home.createViaHrDesc', 'Start the governed HR Agent creation flow.'),
            to: '/agents/new',
            icon: Icons.plus,
        },
        {
            title: t('dashboard.home.assignTask', 'Assign task'),
            description: t('dashboard.home.assignTaskDesc', 'Open a task or session entry point for an employee.'),
            to: '/automations',
            icon: Icons.tasks,
        },
        {
            title: t('dashboard.home.automation', 'Automation'),
            description: t('dashboard.home.automationDesc', 'Review scheduled work and workflow candidates.'),
            to: '/automations',
            icon: Icons.zap,
        },
        {
            title: t('dashboard.home.assets', 'Assets'),
            description: t('dashboard.home.assetsDesc', 'Browse documents, research outputs, and reusable assets.'),
            to: '/documents',
            icon: Icons.activity,
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
                    <button key={action.title} type="button" className="workspace-action-card" onClick={() => onNavigate(action.to)}>
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
        refetchInterval: 15000,
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
