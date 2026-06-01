import { useMemo } from 'react';
import type React from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

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

const ACTIVE_STATUSES = new Set(['running', 'in_progress', 'pending', 'blocked']);
const COMPLETE_STATUSES = new Set(['complete', 'completed', 'done', 'skipped']);

function normalizeStatus(value: string | null | undefined): string {
  return String(value || 'pending').trim().toLowerCase();
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
  const complete = data?.counts?.todos_complete ?? 0;
  return Math.max(0, Math.min(1, complete / total));
}

function currentTodo(items: RuntimeWorkLedgerItem[] | undefined): RuntimeWorkLedgerItem | null {
  const list = items ?? [];
  return (
    list.find((item) => {
      const status = normalizeStatus(item.status);
      return status === 'running' || status === 'in_progress' || status === 'blocked';
    }) ??
    list.find((item) => ACTIVE_STATUSES.has(normalizeStatus(item.status))) ??
    null
  );
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
    refetchInterval: live ? 3000 : 10000,
    retry: false,
  });
  const runtimeQuery = useQuery({
    queryKey: ['chat-work-ledger', agentId, runtimeTaskId],
    queryFn: () => autonomyApi.getRuntimeWorkLedger(agentId, runtimeTaskId as string),
    enabled: Boolean(agentId && runtimeTaskId && (!sessionId || sessionQuery.isError)),
    refetchInterval: live ? 3000 : false,
    retry: live ? 3 : 1,
  });
  const data = sessionQuery.data ?? runtimeQuery.data;
  const isLoading = sessionQuery.isLoading || runtimeQuery.isLoading;
  const error = data ? null : (sessionQuery.error ?? runtimeQuery.error);

  const activeTodo = currentTodo(data?.todo_items);
  const nextTodo = useMemo(() => {
    const todos = data?.todo_items ?? [];
    if (!activeTodo) return todos.find((item) => !COMPLETE_STATUSES.has(normalizeStatus(item.status))) ?? null;
    const activeIndex = todos.findIndex((item) => item.id === activeTodo.id);
    return todos.slice(activeIndex + 1).find((item) => !COMPLETE_STATUSES.has(normalizeStatus(item.status))) ?? null;
  }, [activeTodo, data?.todo_items]);
  const ratio = progressRatio(data);
  const percent = Math.round(ratio * 100);
  const displayTitle = title || t('agent.chat.workLedger.title', 'Agent work ledger');
  const displayStatus = data?.status || (isLoading ? 'loading' : 'running');
  const counts = data?.counts ?? {};
  const todoItems = data?.todo_items ?? [];
  const verification = data?.verification ?? [];
  const progress = data?.progress ?? [];
  const failures = data?.failures ?? [];
  const displayTaskId = data?.runtime_task_id || runtimeTaskId || sessionId || '';

  if (!data && !isLoading) {
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
        open={Boolean(error)}
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
                {t('agent.chat.workLedger.current', 'Current')}: {' '}
                <span style={{ color: 'var(--text-secondary)' }}>
                  {activeTodo?.title || data?.current_phase || t('agent.chat.workLedger.loading', 'Loading work state...')}
                </span>
              </div>
              {nextTodo && (
                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>
                  {t('agent.chat.workLedger.next', 'Next')}: {' '}
                  <span style={{ color: 'var(--text-secondary)' }}>{nextTodo.title}</span>
                </div>
              )}
            </div>
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>
              {counts.todos_complete ?? 0}/{counts.todos_total ?? todoItems.length} {t('agent.chat.workLedger.todos', 'todos')}
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
          <WorkLedgerList title={t('agent.chat.workLedger.todoTitle', 'Todo')} items={todoItems} />
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

function WorkLedgerList({ title, items }: { title: string; items: RuntimeWorkLedgerItem[] }) {
  const { t } = useTranslation();
  if (items.length === 0) return null;
  return (
    <div>
      <SectionTitle>{title}</SectionTitle>
      <div style={{ display: 'grid', gap: '5px' }}>
        {items.map((item) => {
          const normalized = normalizeStatus(item.status);
          const complete = COMPLETE_STATUSES.has(normalized);
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
              <span style={{ color: complete ? 'var(--text-tertiary)' : 'var(--text-secondary)' }}>{item.title}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
