import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { IconCheck, IconChevronRight, IconSquare, IconSquareFilled } from '@tabler/icons-react';

import { autonomyApi, type RuntimeWorkLedgerItem, type RuntimeWorkLedgerView } from '../../api/domains/autonomy';

interface ChatWorkLedgerDockProps {
  agentId: string;
  runtimeTaskId?: string | null;
  sessionId?: string | null;
  live?: boolean;
  operatorView?: boolean;
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
  operatorView = false,
}: ChatWorkLedgerDockProps) {
  const { t } = useTranslation();
  const sessionQuery = useQuery({
    queryKey: ['chat-session-work-ledger', agentId, sessionId, operatorView ? 'operator' : 'owner'],
    queryFn: () => autonomyApi.getSessionWorkLedger(
      agentId,
      sessionId as string,
      operatorView ? { operatorView: true, reason: 'Agent session administration' } : undefined,
    ),
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
    queryKey: ['chat-work-ledger', agentId, runtimeTaskId, operatorView ? 'operator' : 'owner'],
    queryFn: () => autonomyApi.getRuntimeWorkLedger(
      agentId,
      runtimeTaskId as string,
      operatorView ? { operatorView: true, reason: 'Agent session administration' } : undefined,
    ),
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
  const summary = (
    <span data-testid="chat-work-ledger-summary" className="chat-work-ledger-summary" aria-live="polite">
      <span className={`chat-work-ledger-dot ${counts.inProgress > 0 ? 'is-running' : ''}`} aria-hidden="true" />
      <span className="chat-work-ledger-heading">{t('agent.chat.workLedger.todoTitle', 'Todo')}</span>
      <span className="chat-work-ledger-progress">{countLabel}</span>
    </span>
  );

  if (live) {
    return (
      <section
        data-testid="chat-work-ledger-dock"
        data-presentation="persistent"
        className="chat-work-ledger-dock is-live"
        aria-label={t('agent.chat.workLedger.title', 'Agent work ledger')}
      >
        {summary}
        <div data-testid="chat-work-ledger-panel" className="chat-work-ledger-panel">
          <TaskList items={displayItems} prioritizeActive />
        </div>
      </section>
    );
  }

  return (
    <details
      data-testid="chat-work-ledger-dock"
      data-presentation="disclosure"
      className="chat-work-ledger-dock is-terminal"
    >
      <summary className="chat-work-ledger-disclosure-toggle">
        {summary}
        <IconChevronRight className="chat-work-ledger-chevron" size={14} stroke={2.2} aria-hidden="true" />
      </summary>
      <div data-testid="chat-work-ledger-panel" className="chat-work-ledger-panel">
        <TaskList items={displayItems} />
      </div>
    </details>
  );
}

function TaskList({
  items,
  prioritizeActive = false,
}: {
  items: RuntimeWorkLedgerItem[];
  prioritizeActive?: boolean;
}) {
  if (items.length === 0) return null;

  const statusOrder: Record<CanonicalTaskStatus, number> = {
    in_progress: 0,
    pending: 1,
    completed: 2,
  };
  const visibleItems = [...items].sort((a, b) => {
    if (prioritizeActive) {
      const byStatus = statusOrder[taskStatus(a.status)] - statusOrder[taskStatus(b.status)];
      if (byStatus) return byStatus;
    }
    return byTaskIdAsc(a, b);
  });

  return (
    <div id="agent-task-list" data-testid="agent-task-list" className="chat-work-ledger-list">
      <div className="chat-work-ledger-list-grid">
        {visibleItems.map((item) => {
          const status = taskStatus(item.status);
          const blockedBy = item.blockedBy ?? [];
          const isCompleted = status === 'completed';
          const isInProgress = status === 'in_progress';
          const isBlocked = blockedBy.length > 0 && !isCompleted;
          return (
            <div
              key={item.id}
              className={`chat-work-ledger-item is-${status}${isBlocked ? ' is-blocked' : ''}`}
            >
              <TaskStatusIcon status={status} />
              <div className="chat-work-ledger-item-body">
                <span className="chat-work-ledger-item-title">
                  {taskText(item)}
                </span>
                {item.owner && (
                  <span className="chat-work-ledger-item-meta">
                    {' '}
                    (@{item.owner})
                  </span>
                )}
                {isBlocked && (
                  <span className="chat-work-ledger-item-meta">
                    {' '}
                    blocked by {blockedBy.map((id) => `#${id}`).join(', ')}
                  </span>
                )}
                {isInProgress && !isBlocked && taskActiveText(item) !== taskText(item) && (
                  <div className="chat-work-ledger-active-form">{taskActiveText(item)}...</div>
                )}
              </div>
            </div>
          );
        })}
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
