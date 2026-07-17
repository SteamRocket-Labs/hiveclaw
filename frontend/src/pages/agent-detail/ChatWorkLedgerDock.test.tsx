import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ChatWorkLedgerDock from './ChatWorkLedgerDock';
import type { RuntimeWorkLedgerView } from '../../api/domains/autonomy';

const queryHarness = vi.hoisted(() => ({
  calls: [] as Array<{
    queryKey: unknown[];
    enabled?: boolean;
    refetchInterval?: false | number | ((...args: unknown[]) => unknown);
    refetchOnMount?: unknown;
    refetchOnWindowFocus?: unknown;
    staleTime?: unknown;
    retry?: unknown;
  }>,
  sessionData: undefined as RuntimeWorkLedgerView | undefined,
  runtimeData: undefined as RuntimeWorkLedgerView | undefined,
  sessionError: false,
  runtimeError: false,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, values?: Record<string, string | number>) => {
      let output = fallback || _key;
      Object.entries(values || {}).forEach(([key, value]) => {
        output = output.replace(new RegExp(`{{\\s*${key}\\s*}}`, 'g'), String(value));
      });
      return output;
    },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: {
    queryKey: unknown[];
    enabled?: boolean;
    refetchInterval?: false | number | ((...args: unknown[]) => unknown);
    refetchOnMount?: unknown;
    refetchOnWindowFocus?: unknown;
    staleTime?: unknown;
    retry?: unknown;
  }) => {
    queryHarness.calls.push(options);
    if (options.enabled === false) {
      return { data: undefined, isLoading: false, isError: false, error: null };
    }
    const key = String(options.queryKey[0]);
    if (key === 'chat-session-work-ledger') {
      if (queryHarness.sessionError) {
        return { data: undefined, isLoading: false, isError: true, error: new Error('missing') };
      }
      return { data: queryHarness.sessionData, isLoading: false, isError: false, error: null };
    }
    if (key === 'chat-work-ledger') {
      if (queryHarness.runtimeError) {
        return { data: undefined, isLoading: false, isError: true, error: new Error('missing') };
      }
      return { data: queryHarness.runtimeData, isLoading: false, isError: false, error: null };
    }
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
}));
function ledger(runtimeTaskId: string, title: string): RuntimeWorkLedgerView {
  return {
    schema: 'agent_work_ledger_view.v1',
    runtime_task_id: runtimeTaskId,
    status: 'in_progress',
    current_phase: title,
    todo_items: [{ id: `${runtimeTaskId}-todo`, title, status: 'in_progress', required: true }],
    counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
  };
}

describe('ChatWorkLedgerDock', () => {
  beforeEach(() => {
    queryHarness.calls.length = 0;
    queryHarness.sessionData = undefined;
    queryHarness.runtimeData = undefined;
    queryHarness.sessionError = false;
    queryHarness.runtimeError = false;
  });

  it('uses the explicit runtime ledger when session data points at an older task', () => {
    queryHarness.sessionData = ledger('task-old', 'Old completed todo');
    queryHarness.runtimeData = ledger('task-current', 'Current running todo');

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock
        agentId="agent-1"
        sessionId="session-1"
        runtimeTaskId="task-current"
        live
      />,
    );

    expect(markup).toContain('Current running todo');
    expect(markup).not.toContain('Old completed todo');
    const runtimeCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'chat-work-ledger');
    expect(runtimeCall?.enabled).toBe(true);
  });

  it('falls back to the explicit runtime ledger when session lookup is missing', () => {
    queryHarness.sessionError = true;
    queryHarness.runtimeData = ledger('task-current', 'Runtime fallback todo');

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock
        agentId="agent-1"
        sessionId="session-1"
        runtimeTaskId="task-current"
        live
      />,
    );

    expect(markup).toContain('Runtime fallback todo');
  });

  it('keeps session-scoped ledger visible while an active run has no runtime ledger', () => {
    queryHarness.sessionData = {
      schema: 'agent_work_ledger_view.v1',
      runtime_task_id: null,
      session_id: 'session-1',
      status: 'running',
      current_phase: 'planning',
      todo_items: [{ id: 'todo-live', title: 'Live session todo', status: 'in_progress', required: true }],
      counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
    };

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock
        agentId="agent-1"
        sessionId="session-1"
        runtimeTaskId="task-current"
        live
      />,
    );

    expect(markup).toContain('Live session todo');
    const runtimeCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'chat-work-ledger');
    expect(runtimeCall?.enabled).toBe(false);
  });

  it('does not poll idle session work ledgers', () => {
    queryHarness.sessionData = ledger('task-idle', 'Idle todo');

    renderToStaticMarkup(<ChatWorkLedgerDock agentId="agent-1" sessionId="session-1" live={false} />);

    const sessionCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'chat-session-work-ledger');
    expect(sessionCall?.enabled).toBe(true);
    expect(sessionCall?.refetchInterval).toBe(false);
    expect(sessionCall?.refetchOnMount).toBe('always');
    expect(sessionCall?.retry).toBe(false);
  });

  it('retries and refreshes live session work ledgers without waiting for a page reload', () => {
    queryHarness.sessionData = ledger('task-current', 'Live todo');

    renderToStaticMarkup(<ChatWorkLedgerDock agentId="agent-1" sessionId="session-1" live />);

    const sessionCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'chat-session-work-ledger');
    expect(sessionCall?.refetchInterval).toBe(5000);
    expect(sessionCall?.refetchOnMount).toBe('always');
    expect(sessionCall?.refetchOnWindowFocus).toBe(true);
    expect(sessionCall?.staleTime).toBe(0);
    expect(sessionCall?.retry).toBe(3);
  });

  it('does not keep a live pending dock when the ledger is not created yet', () => {
    queryHarness.sessionError = true;
    queryHarness.runtimeError = true;

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock
        agentId="agent-1"
        sessionId="session-1"
        runtimeTaskId="task-current"
        live
      />,
    );

    expect(markup).not.toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).not.toContain('Loading work state...');
  });

  it('keeps the complete task ledger persistently visible beside the composer while work is live', () => {
    queryHarness.sessionData = {
      schema: 'agent_work_ledger_view.v1',
      runtime_task_id: 'task-current',
      status: 'in_progress',
      current_phase: 'Collect and grade sources',
      todo_items: [
        { id: '1', title: 'Plan research lanes', status: 'completed', required: true },
        {
          id: '2',
          title: 'Collect and grade sources',
          content: 'Collect and grade sources',
          activeForm: 'Collecting and grading sources',
          status: 'in_progress',
          required: true,
        },
        { id: '3', title: 'Write final report', status: 'pending', required: true },
      ],
      verification: [{ id: 'verify-1', title: 'Audit evidence links', status: 'pending' }],
      progress: [{ id: 'progress-1', status: 'completed', delta: 'Fetched source ledger' }],
      failures: [{ id: 'failure-1', attempt: 'fetch', error: 'Temporary source timeout' }],
      counts: { todos_total: 3, todos_complete: 1, todos_open: 2 },
      path: 'runtime_artifacts/long_tasks/task-current/work_ledger.json',
    };

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock
        agentId="agent-1"
        sessionId="session-1"
        runtimeTaskId="task-current"
        live
      />,
    );

    expect(markup).toContain('data-testid="agent-task-list"');
    expect(markup).toContain('data-testid="chat-work-ledger-summary"');
    expect(markup).toContain('data-testid="chat-work-ledger-panel"');
    expect(markup).toContain('data-presentation="persistent"');
    expect(markup).toContain('Task 2-3 of 3');
    expect(markup).toContain('Collecting and grading sources');
    expect(markup).not.toContain('chat-work-ledger-popover');
    expect(markup).not.toContain('<details');
    expect(markup).not.toContain('files changed');
    expect(markup).not.toContain('Agent tasks');
    expect(markup).not.toContain('3 tasks');
    expect(markup).not.toContain('Current');
    expect(markup).not.toContain('Next');
    expect(markup).not.toContain('task-current');
    expect(markup).not.toContain('Verification');
    expect(markup).not.toContain('Progress');
    expect(markup).not.toContain('Blockers');
    expect(markup).not.toContain('runtime_artifacts/long_tasks');
    expect(markup).not.toContain('Fetched source ledger');
    expect(markup).not.toContain('Temporary source timeout');
  });

  it('keeps the terminal task summary anchored and exposes details through an explicit disclosure', () => {
    queryHarness.sessionData = ledger('task-current', 'Collapsible todo');

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock
        agentId="agent-1"
        sessionId="session-1"
        runtimeTaskId="task-current"
      />,
    );

    expect(markup).toContain('data-testid="chat-work-ledger-dock"');
    expect(markup).toContain('Todo');
    expect(markup).toContain('data-testid="agent-task-list"');
    expect(markup).toContain('Collapsible todo');
    expect(markup).toContain('data-presentation="disclosure"');
    expect(markup).toContain('<details');
    expect(markup).toContain('<summary');
    expect(markup).not.toContain('chat-work-ledger-popover');
  });

  it('keeps every authorized task recoverable instead of silently truncating the ledger', () => {
    queryHarness.sessionData = {
      schema: 'agent_work_ledger_view.v1',
      runtime_task_id: 'task-current',
      status: 'in_progress',
      current_phase: 'Task 12',
      todo_items: Array.from({ length: 12 }, (_, index) => ({
        id: String(index + 1),
        title: `Task ${index + 1}`,
        status: index === 11 ? 'in_progress' : 'pending',
        required: true,
      })),
      counts: { todos_total: 12, todos_complete: 0, todos_open: 12 },
    };

    const markup = renderToStaticMarkup(
      <ChatWorkLedgerDock agentId="agent-1" sessionId="session-1" runtimeTaskId="task-current" live />,
    );

    expect(markup).toContain('Task 1');
    expect(markup).toContain('Task 10');
    expect(markup).toContain('Task 11');
    expect(markup).toContain('Task 12');
    expect(markup).not.toContain('... +');
  });
});
