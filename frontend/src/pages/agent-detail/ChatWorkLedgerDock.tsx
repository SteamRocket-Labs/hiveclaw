import { useMemo } from 'react';
import type React from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { IconCheck, IconSquare, IconSquareFilled } from '@tabler/icons-react';

import { autonomyApi, type RuntimeWorkLedgerItem, type RuntimeWorkLedgerView } from '../../api/domains/autonomy';
import DeepResearchStreamPanel from './DeepResearchStreamPanel';

interface ChatWorkLedgerDockProps {
  agentId: string;
  runtimeTaskId?: string | null;
  sessionId?: string | null;
  title?: string;
  showDeepResearchStream?: boolean;
  live?: boolean;
}

type CanonicalTaskStatus = 'pending' | 'in_progress' | 'completed';

const COMPLETE_STATUSES = new Set(['complete', 'completed', 'done', 'skipped']);

function normalizeStatus(value: string | null | undefined): string {
  return String(value || 'pending').trim().toLowerCase();
}

function taskStatus(value: string | null | undefined): CanonicalTaskStatus {
  const normalized = normalizeStatus(value);
  if (COMPLETE_STATUSES.has(normalized)) return 'completed';
  if (normalized === 'running' || normalized === 'in_progress') return 'in_progress';
  return 'pending';
}

function statusLabel(status: string, t: ReturnType<typeof useTranslation>['t']): string {
  const normalized = normalizeStatus(status);
  if (COMPLETE_STATUSES.has(normalized)) return t('agent.chat.workLedger.statusComplete', 'Done');
  if (normalized === 'running' || normalized === 'in_progress') {
    return t('agent.chat.workLedger.statusRunning', 'Running');
  }
  if (normalized === 'blocked' || normalized === 'failed') return t('agent.chat.workLedger.statusBlocked', 'Blocked');
  return t('agent.chat.workLedger.statusPending', 'Pending');
}

function progressRatio(data: RuntimeWorkLedgerView | undefined): number {
  const total = data?.counts?.todos_total ?? data?.todo_items?.length ?? 0;
  if (!total) return 0;
  const complete = data?.counts?.todos_complete ?? (data?.todo_items ?? []).filter((item) => taskStatus(item.status) === 'completed').length;
  return Math.max(0, Math.min(1, complete / total));
}

function currentTodo(items: RuntimeWorkLedgerItem[] | undefined): RuntimeWorkLedgerItem | null {
  const list = items ?? [];
  return (
    list.find((item) => taskStatus(item.status) === 'in_progress') ??
    list.find((item) => taskStatus(item.status) === 'pending') ??
    null
  );
}

function taskText(item: RuntimeWorkLedgerItem): string {
  return item.content || item.subject || item.title;
}

function taskActiveText(item: RuntimeWorkLedgerItem): string {
  return item.activeForm || item.active_form || taskText(item);
}

function byTaskIdAsc(a: RuntimeWorkLedgerItem, b: RuntimeWorkLedgerItem): number {
  const aNum = Number.parseInt(a.id, 10);
  const bNum = Number.parseInt(b.id, 10);
  if (!Number.isNaN(aNum) && !Number.isNaN(bNum)) return aNum - bNum;
  return a.id.localeCompare(b.id);
}

function taskCounts(items: RuntimeWorkLedgerItem[]) {
  const completed = items.filter((item) => taskStatus(item.status) === 'completed').length;
  const pending = items.filter((item) => taskStatus(item.status) === 'pending').length;
  const inProgress = items.length - completed - pending;
  return { completed, pending, inProgress, total: items.length };
}

export default function ChatWorkLedgerDock({
  agentId,
  runtimeTaskId,
  sessionId,
  title,
  showDeepResearchStream = false,
  live = false,
}: ChatWorkLedgerDockProps) {
  const { t } = useTranslation();
  const sessionQuery = useQuery({
    queryKey: ['chat-session-work-ledger', agentId, sessionId],
    queryFn: () => autonomyApi.getSessionWorkLedger(agentId, sessionId as string),
    enabled: Boolean(agentId && sessionId),
    refetchInterval: live ? 3000 : false,
    retry: false,
  });
  const runtimeTaskKey = runtimeTaskId ? String(runtimeTaskId) : '';
  const sessionData = sessionQuery.data;
  const sessionRuntimeTaskKey = sessionData?.runtime_task_id ? String(sessionData.runtime_task_id) : '';
  const sessionMatchesRuntime = !runtimeTaskKey || sessionRuntimeTaskKey === runtimeTaskKey;
  const preferRuntimeLedger = Boolean(runtimeTaskKey && sessionData && !sessionMatchesRuntime);
  const runtimeQueryEnabled = Boolean(
    agentId && runtimeTaskKey && (!sessionId || sessionQuery.isError || preferRuntimeLedger),
  );
  const runtimeQuery = useQuery({
    queryKey: ['chat-work-ledger', agentId, runtimeTaskId],
    queryFn: () => autonomyApi.getRuntimeWorkLedger(agentId, runtimeTaskId as string),
    enabled: runtimeQueryEnabled,
    refetchInterval: live ? 3000 : false,
    retry: false,
  });
  const data = preferRuntimeLedger ? runtimeQuery.data : (sessionData ?? runtimeQuery.data);
  const isLoading = sessionQuery.isLoading || (runtimeQueryEnabled && runtimeQuery.isLoading);
  const missingLiveLedger = live && Boolean(sessionId || runtimeTaskKey) && !data && !isLoading;
  const error = data || missingLiveLedger ? null : (sessionQuery.error ?? runtimeQuery.error);

  const activeTodo = currentTodo(data?.todo_items);
  const nextTodo = useMemo(() => {
    const todos = data?.todo_items ?? [];
    if (!activeTodo) return todos.find((item) => taskStatus(item.status) !== 'completed') ?? null;
    const activeIndex = todos.findIndex((item) => item.id === activeTodo.id);
    return todos.slice(activeIndex + 1).find((item) => taskStatus(item.status) !== 'completed') ?? null;
  }, [activeTodo, data?.todo_items]);
  const ratio = progressRatio(data);
  const percent = Math.round(ratio * 100);
  const displayTitle = title || t('agent.chat.workLedger.taskTitle', 'Agent tasks');
  const displayStatus = data?.status || (isLoading ? 'loading' : 'running');
  const todoItems = data?.todo_items ?? [];
  const counts = taskCounts(todoItems);
  const verification = data?.verification ?? [];
  const progress = data?.progress ?? [];
  const failures = data?.failures ?? [];
  const displayTaskId = data?.runtime_task_id || runtimeTaskId || sessionId || '';
  const hasOpenTasks = todoItems.some((item) => taskStatus(item.status) !== 'completed');
  const taskSummary = `${counts.total} tasks (${counts.completed} done, ${
    counts.inProgress > 0 ? `${counts.inProgress} in progress, ` : ''
  }${counts.pending} open)`;

  if (!data && !isLoading && !missingLiveLedger) {
    return null;
  }

  return (
    <div
      data-testid="chat-work-ledger-dock"
      style={{
        borderTop: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)',
        padding: '8px 16px',
      }}
    >
      <details
        open={Boolean(error) || live || hasOpenTasks}
        style={{
          border: '1px solid var(--border-subtle)',
          borderRadius: '8px',
          background: 'var(--bg-secondary)',
          overflow: 'hidden',
        }}
      >
        <summary
          style={{
            cursor: 'pointer',
            listStyle: 'none',
            display: 'grid',
            gap: '6px',
            padding: '10px 12px',
            userSelect: 'none',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
            <span style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)' }}>{displayTitle}</span>
            <span
              style={{
                flexShrink: 0,
                fontSize: '10px',
                padding: '2px 7px',
                borderRadius: '999px',
                background: live ? 'rgba(16,185,129,0.12)' : 'var(--bg-tertiary)',
                color: live ? 'var(--accent-primary)' : 'var(--text-tertiary)',
                fontWeight: 700,
              }}
            >
              {statusLabel(displayStatus, t)}
            </span>
            {displayTaskId && (
              <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                {displayTaskId.slice(0, 8)}
              </span>
            )}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: '8px', alignItems: 'center' }}>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                <span style={{ fontWeight: 700, color: 'var(--text-secondary)' }}>{taskSummary}</span>
              </div>
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                {t('agent.chat.workLedger.current', 'Current')}: {' '}
                <span style={{ color: 'var(--text-secondary)' }}>
                  {activeTodo
                    ? taskActiveText(activeTodo)
                    : data?.current_phase || t('agent.chat.workLedger.loading', 'Loading work state...')}
                </span>
              </div>
              {nextTodo && (
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {t('agent.chat.workLedger.next', 'Next')}: {' '}
                  <span style={{ color: 'var(--text-secondary)' }}>{taskText(nextTodo)}</span>
                </div>
              )}
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
              {counts.completed}/{counts.total} {t('agent.chat.workLedger.todos', 'todos')}
            </div>
          </div>
          <div style={{ height: '4px', borderRadius: '999px', background: 'var(--bg-tertiary)', overflow: 'hidden' }}>
            <div
              style={{
                width: `${percent}%`,
                height: '100%',
                borderRadius: '999px',
                background: 'var(--accent-primary)',
                transition: 'width 160ms ease',
              }}
            />
          </div>
        </summary>

        <div style={{ borderTop: '1px solid var(--border-subtle)', padding: '10px 12px', display: 'grid', gap: '10px' }}>
          {error && (
            <div style={{ fontSize: '12px', color: 'var(--warning)' }}>
              {t('agent.chat.workLedger.loadFailed', 'Work ledger is not available yet.')}
            </div>
          )}
          <TaskList items={todoItems} />
          {verification.length > 0 && (
            <WorkLedgerList title={t('agent.chat.workLedger.verificationTitle', 'Verification')} items={verification} />
          )}
          {progress.length > 0 && (
            <div>
              <SectionTitle>{t('agent.chat.workLedger.progressTitle', 'Progress')}</SectionTitle>
              <div style={{ display: 'grid', gap: '5px' }}>
                {progress.slice(-5).map((item) => (
                  <div key={item.id} style={{ fontSize: '11px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                    <span style={{ color: 'var(--text-tertiary)' }}>{statusLabel(item.status, t)}: </span>
                    {item.delta}
                  </div>
                ))}
              </div>
            </div>
          )}
          {failures.length > 0 && (
            <div>
              <SectionTitle>{t('agent.chat.workLedger.blockersTitle', 'Blockers')}</SectionTitle>
              <div style={{ display: 'grid', gap: '5px' }}>
                {failures.map((item) => (
                  <div key={item.id} style={{ fontSize: '11px', color: 'var(--warning)', lineHeight: 1.5 }}>
                    {item.error}
                  </div>
                ))}
              </div>
            </div>
          )}
          {showDeepResearchStream && live && data?.runtime_task_id && (
            <DeepResearchStreamPanel agentId={agentId} taskId={data.runtime_task_id} />
          )}
          {data?.path && (
            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)' }}>
              {data.path}
            </div>
          )}
        </div>
      </details>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-tertiary)', marginBottom: '5px' }}>
      {children}
    </div>
  );
}

function TaskList({ items }: { items: RuntimeWorkLedgerItem[] }) {
  const { t } = useTranslation();
  if (items.length === 0) return null;

  const maxDisplay = 10;
  const sorted = [...items].sort(byTaskIdAsc);
  const needsTruncation = sorted.length > maxDisplay;
  const visibleItems = needsTruncation
    ? [...items]
        .sort((a, b) => {
          const statusOrder: Record<CanonicalTaskStatus, number> = {
            in_progress: 0,
            pending: 1,
            completed: 2,
          };
          const byStatus = statusOrder[taskStatus(a.status)] - statusOrder[taskStatus(b.status)];
          return byStatus || byTaskIdAsc(a, b);
        })
        .slice(0, maxDisplay)
    : sorted;
  const hiddenItems = needsTruncation ? items.filter((item) => !visibleItems.some((visible) => visible.id === item.id)) : [];
  const hiddenCounts = taskCounts(hiddenItems);
  const hiddenSummary = hiddenItems.length
    ? [
        hiddenCounts.inProgress ? `${hiddenCounts.inProgress} in progress` : '',
        hiddenCounts.pending ? `${hiddenCounts.pending} pending` : '',
        hiddenCounts.completed ? `${hiddenCounts.completed} completed` : '',
      ]
        .filter(Boolean)
        .join(', ')
    : '';

  return (
    <div data-testid="agent-task-list">
      <SectionTitle>{t('agent.chat.workLedger.todoTitle', 'Todo')}</SectionTitle>
      <div style={{ display: 'grid', gap: '6px' }}>
        {visibleItems.map((item) => {
          const status = taskStatus(item.status);
          const blockedBy = item.blockedBy ?? [];
          const isCompleted = status === 'completed';
          const isInProgress = status === 'in_progress';
          const isBlocked = blockedBy.length > 0 && !isCompleted;
          return (
            <div
              key={item.id}
              style={{
                display: 'grid',
                gridTemplateColumns: '18px minmax(0, 1fr)',
                gap: '8px',
                alignItems: 'start',
                fontSize: '11px',
                lineHeight: 1.45,
              }}
            >
              <TaskStatusIcon status={status} />
              <div style={{ minWidth: 0 }}>
                <span
                  style={{
                    color: isCompleted || isBlocked ? 'var(--text-tertiary)' : 'var(--text-secondary)',
                    fontWeight: isInProgress ? 700 : 500,
                    textDecoration: isCompleted ? 'line-through' : 'none',
                  }}
                >
                  {taskText(item)}
                </span>
                {item.owner && (
                  <span style={{ color: 'var(--text-tertiary)' }}>
                    {' '}
                    (@{item.owner})
                  </span>
                )}
                {isBlocked && (
                  <span style={{ color: 'var(--text-tertiary)' }}>
                    {' '}
                    blocked by {blockedBy.map((id) => `#${id}`).join(', ')}
                  </span>
                )}
                {isInProgress && !isBlocked && taskActiveText(item) !== taskText(item) && (
                  <div style={{ color: 'var(--text-tertiary)', marginTop: '1px' }}>{taskActiveText(item)}...</div>
                )}
              </div>
            </div>
          );
        })}
        {hiddenSummary && (
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>... +{hiddenSummary}</div>
        )}
      </div>
    </div>
  );
}

function TaskStatusIcon({ status }: { status: CanonicalTaskStatus }) {
  const shared = { size: 13, stroke: 2 } as const;
  if (status === 'completed') {
    return <IconCheck {...shared} color="var(--accent-primary)" style={{ marginTop: '2px' }} />;
  }
  if (status === 'in_progress') {
    return <IconSquareFilled size={10} color="var(--accent-primary)" style={{ marginTop: '4px' }} />;
  }
  return <IconSquare {...shared} color="var(--text-tertiary)" style={{ marginTop: '2px' }} />;
}

function WorkLedgerList({ title, items }: { title: string; items: RuntimeWorkLedgerItem[] }) {
  const { t } = useTranslation();
  if (items.length === 0) return null;
  return (
    <div>
      <SectionTitle>{title}</SectionTitle>
      <div style={{ display: 'grid', gap: '5px' }}>
        {items.map((item) => {
          const complete = taskStatus(item.status) === 'completed';
          return (
            <div
              key={item.id}
              style={{
                display: 'grid',
                gridTemplateColumns: '72px minmax(0, 1fr)',
                gap: '8px',
                alignItems: 'start',
                fontSize: '11px',
                lineHeight: 1.5,
              }}
            >
              <span style={{ color: complete ? 'var(--accent-primary)' : 'var(--text-tertiary)', fontWeight: 600 }}>
                {statusLabel(item.status, t)}
              </span>
              <span style={{ color: complete ? 'var(--text-tertiary)' : 'var(--text-secondary)' }}>{taskText(item)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
