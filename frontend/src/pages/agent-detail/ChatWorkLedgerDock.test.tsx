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
  }>,
  sessionData: undefined as RuntimeWorkLedgerView | undefined,
  runtimeData: undefined as RuntimeWorkLedgerView | undefined,
  sessionError: false,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: {
    queryKey: unknown[];
    enabled?: boolean;
    refetchInterval?: false | number | ((...args: unknown[]) => unknown);
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
      return { data: queryHarness.runtimeData, isLoading: false, isError: false, error: null };
    }
    return { data: undefined, isLoading: false, isError: false, error: null };
  },
}));

vi.mock('./DeepResearchStreamPanel', () => ({
  default: ({ taskId }: { taskId: string }) => <div>stream:{taskId}</div>,
}));

function ledger(runtimeTaskId: string, title: string): RuntimeWorkLedgerView {
  return {
    schema: 'agent_work_ledger_view.v1',
    runtime_task_id: runtimeTaskId,
    status: 'running',
    current_phase: title,
    todo_items: [{ id: `${runtimeTaskId}-todo`, title, status: 'running', required: true }],
    counts: { todos_total: 1, todos_complete: 0, todos_open: 1 },
  };
}

describe('ChatWorkLedgerDock', () => {
  beforeEach(() => {
    queryHarness.calls.length = 0;
    queryHarness.sessionData = undefined;
    queryHarness.runtimeData = undefined;
    queryHarness.sessionError = false;
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

  it('does not poll idle session work ledgers', () => {
    queryHarness.sessionData = ledger('task-idle', 'Idle todo');

    renderToStaticMarkup(<ChatWorkLedgerDock agentId="agent-1" sessionId="session-1" live={false} />);

    const sessionCall = queryHarness.calls.find((call) => String(call.queryKey[0]) === 'chat-session-work-ledger');
    expect(sessionCall?.enabled).toBe(true);
    expect(sessionCall?.refetchInterval).toBe(false);
  });
});
