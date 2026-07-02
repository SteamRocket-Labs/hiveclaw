import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { IconCheck, IconSquare, IconSquareFilled } from '@tabler/icons-react';

import { autonomyApi, type RuntimeWorkLedgerItem, type RuntimeWorkLedgerView } from '../../api/domains/autonomy';

interface ChatWorkLedgerDockProps {
  agentId: string;
  runtimeTaskId?: string | null;
  sessionId?: string | null;
  live?: boolean;
  /** Deprecated: task details are now revealed by hover/focus, not click collapse. */
  initialCollapsed?: boolean;
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

function taskProgressLabel(
  items: RuntimeWorkLedgerItem[],
  t: ReturnType<typeof useTranslation>['t'],
): string {
  const sorted = [...items].sort(byTaskIdAsc);
  const openIndexes = sorted
    .map((item, index) => (taskStatus(item.status) === 'completed' ? null : index + 1))
    .filter((value): value is number => typeof value === 'number');
  if (openIndexes.length === 0) {
    return t('agent.chat.workLedger.allDoneCount', '{{count}} tasks complete', { count: sorted.length });
  }
  const start = Math.min(...openIndexes);
  const end = Math.max(...openIndexes);
  if (start === end) {
    return t('agent.chat.workLedger.singleTaskProgress', 'Task {{index}} of {{total}}', {
      index: start,
      total: sorted.length,
    });
  }
  return t('agent.chat.workLedger.taskRangeProgress', 'Task {{start}}-{{end}} of {{total}}', {
    start,
    end,
    total: sorted.length,
  });
}

export default function ChatWorkLedgerDock({
  agentId,
  runtimeTaskId,
  sessionId,
  live = false,
}: ChatWorkLedgerDockProps) {
  const { t } = useTranslation();
  const sessionQuery = useQuery({
    queryKey: ['chat-session-work-ledger', agentId, sessionId],
    queryFn: () => autonomyApi.getSessionWorkLedger(agentId, sessionId as string),
    enabled: Boolean(agentId && sessionId),
    refetchInterval: live ? 5000 : false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: live,
    staleTime: 0,
    retry: live ? 3 : false,
  });
  const runtimeTaskKey = runtimeTaskId ? String(runtimeTaskId) : '';
  const sessionData = sessionQuery.data;
  const sessionRuntimeTaskKey = sessionData?.runtime_task_id ? String(sessionData.runtime_task_id) : '';
  const sessionScopedLedger = Boolean(sessionData && !sessionRuntimeTaskKey);
  const sessionMatchesRuntime = !runtimeTaskKey || sessionRuntimeTaskKey === runtimeTaskKey;
  const preferRuntimeLedger = Boolean(runtimeTaskKey && sessionData && !sessionMatchesRuntime && !sessionScopedLedger);
  const runtimeQueryEnabled = Boolean(
    agentId && runtimeTaskKey && (!sessionId || sessionQuery.isError || preferRuntimeLedger),
  );
  const runtimeQuery = useQuery({
    queryKey: ['chat-work-ledger', agentId, runtimeTaskId],
    queryFn: () => autonomyApi.getRuntimeWorkLedger(agentId, runtimeTaskId as string),
    enabled: runtimeQueryEnabled,
    refetchInterval: live ? 5000 : false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: live,
    staleTime: 0,
    retry: live ? 3 : false,
  });
  const data = preferRuntimeLedger ? runtimeQuery.data : (sessionData ?? runtimeQuery.data);
  const isLoading = sessionQuery.isLoading || (runtimeQueryEnabled && runtimeQuery.isLoading);
  const todoItems = data?.todo_items ?? [];
  const displayItems = isLoading
    ? [
        {
          id: 'work-ledger-loading',
          title: t('agent.chat.workLedger.loading', 'Loading work state...'),
          status: 'pending',
          required: false,
        },
      ]
    : todoItems;

  if ((!data && !isLoading) || displayItems.length === 0) {
    return null;
  }
  const counts = taskCounts(displayItems);
  const countLabel = taskProgressLabel(displayItems, t);

  return (
    <div data-testid="chat-work-ledger-dock" className="chat-work-ledger-dock" tabIndex={0}>
      <div data-testid="chat-work-ledger-summary" className="chat-work-ledger-summary">
        <span className={`chat-work-ledger-dot ${counts.inProgress > 0 ? 'is-running' : ''}`} aria-hidden="true" />
        <span>{countLabel}</span>
      </div>
      <div data-testid="chat-work-ledger-popover" className="chat-work-ledger-popover">
        <div className="chat-work-ledger-popover-title">
          {t('agent.chat.workLedger.todoTitle', 'Todo')}
        </div>
        <TaskList items={displayItems} />
      </div>
    </div>
  );
}

function TaskList({ items }: { items: RuntimeWorkLedgerItem[] }) {
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
    <div id="agent-task-list" data-testid="agent-task-list" style={{ marginTop: '8px' }}>
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
