import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { IconCheck, IconChevronDown, IconChevronRight, IconSquare, IconSquareFilled } from '@tabler/icons-react';

import { autonomyApi, type RuntimeWorkLedgerItem, type RuntimeWorkLedgerView } from '../../api/domains/autonomy';

interface ChatWorkLedgerDockProps {
  agentId: string;
  runtimeTaskId?: string | null;
  sessionId?: string | null;
  live?: boolean;
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

export default function ChatWorkLedgerDock({
  agentId,
  runtimeTaskId,
  sessionId,
  live = false,
  initialCollapsed = false,
}: ChatWorkLedgerDockProps) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(initialCollapsed);
  const sessionQuery = useQuery({
    queryKey: ['chat-session-work-ledger', agentId, sessionId],
    queryFn: () => autonomyApi.getSessionWorkLedger(agentId, sessionId as string),
    enabled: Boolean(agentId && sessionId),
    refetchInterval: live ? 3000 : false,
    refetchOnMount: 'always',
    refetchOnWindowFocus: live,
    staleTime: 0,
    retry: live ? 3 : false,
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
  const openCount = counts.pending + counts.inProgress;
  const countLabel = openCount > 0
    ? `${openCount}/${counts.total} ${t('agent.chat.workLedger.openSuffix', 'open')}`
    : t('agent.chat.workLedger.allDone', 'All done');

  return (
    <div
      data-testid="chat-work-ledger-dock"
      style={{
        borderTop: '1px solid var(--border-subtle)',
        background: 'var(--bg-elevated)',
        padding: '8px 16px',
      }}
    >
      <div
        style={{
          border: '1px solid var(--border-subtle)',
          borderRadius: '8px',
          background: 'var(--bg-secondary)',
          overflow: 'hidden',
          padding: '10px 12px',
        }}
      >
        <button
          type="button"
          data-testid="chat-work-ledger-toggle"
          aria-expanded={!collapsed}
          aria-controls="agent-task-list"
          onClick={() => setCollapsed((value) => !value)}
          style={{
            width: '100%',
            border: 'none',
            background: 'transparent',
            padding: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '8px',
            cursor: 'pointer',
            color: 'inherit',
            textAlign: 'left',
          }}
        >
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '6px', minWidth: 0 }}>
            {collapsed
              ? <IconChevronRight size={13} stroke={2} color="var(--text-tertiary)" />
              : <IconChevronDown size={13} stroke={2} color="var(--text-tertiary)" />}
            <span style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-tertiary)' }}>
              {t('agent.chat.workLedger.todoTitle', 'Todo')}
            </span>
          </span>
          <span style={{ fontSize: '10px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
            {countLabel}
          </span>
        </button>
        {!collapsed && <TaskList items={displayItems} />}
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
