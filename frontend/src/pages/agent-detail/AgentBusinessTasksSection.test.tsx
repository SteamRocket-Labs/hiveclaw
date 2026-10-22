import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import type { BusinessTaskDetail } from '../../api/domains/tasks';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Task } from '../../types';
import type { PlanRequest } from '../../api/domains/plans';
import AgentBusinessTasksSection, {
  buildBusinessTaskCreatePlanDraft,
  buildBusinessTaskRetryPlanDraft,
  readBusinessTaskActionPlan,
} from './AgentBusinessTasksSection';
import { AGENT_DETAIL_TABS, AGENT_WORKBENCH_AREAS } from './agentDetailPolicy';

const queryData = vi.hoisted(() => ({ tasks: [] as Task[], plans: [] as PlanRequest[], detail: undefined as BusinessTaskDetail | undefined, detailError: false }));

vi.mock('../../stores', () => ({ useAuthStore: (select: (state: unknown) => unknown) => select({ user: { id: 'owner' } }) }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback: string) => fallback,
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey, enabled = true }: { queryKey: unknown[]; enabled?: boolean }) => {
    if (!enabled) return { data: undefined, isLoading: false, isError: false, error: null, refetch: vi.fn() };
    if (queryKey[0] === 'business-tasks') {
      return { data: queryData.tasks, isLoading: false, isError: false, error: null, refetch: vi.fn() };
    }
    if (queryKey[0] === 'business-task-plans') {
      return { data: queryData.plans, isLoading: false, isError: false, error: null, refetch: vi.fn() };
    }
    if (queryKey[0] === 'business-task-detail') return { data: queryData.detail, isLoading: false, isError: queryData.detailError, error: null, refetch: vi.fn() };
    return { data: undefined, isLoading: false, isError: false, error: null, refetch: vi.fn() };
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}));

describe('AgentBusinessTasksSection', () => {
  beforeEach(() => {
    queryData.tasks = [];
    queryData.detail = undefined;
    queryData.detailError = false;
    queryData.plans = [];
  });

  it('has one dedicated assignment tab inside Conversation & Tasks', () => {
    expect(AGENT_DETAIL_TABS).toContain('tasks');
    expect(AGENT_WORKBENCH_AREAS.find((area) => area.id === 'conversation')?.tabs).toEqual([
      'chat',
      'tasks',
      'aware',
    ]);
  });

  it('builds a hash-bindable create plan and persists crash-recovery metadata', () => {
    const draft = buildBusinessTaskCreatePlanDraft({
      request_id: 'create-request-1',
      title: 'Prepare board report',
      description: 'Use verified figures',
      priority: 'high',
      due_date: '2026-07-20T09:00:00.000Z',
    });

    expect(draft.task).toEqual({
      request_id: 'create-request-1',
      title: 'Prepare board report',
      description: 'Use verified figures',
      type: 'todo',
      priority: 'high',
      due_date: '2026-07-20T09:00:00.000Z',
    });
    expect(draft.plan.fill?.authorization_scopes).toEqual([
      {
        action_kind: 'start_long_task',
        target_ref: 'task:new',
        arguments: draft.task,
        summary: 'Start the business assignment “Prepare board report”',
        max_uses: 1,
      },
    ]);
    expect(draft.plan.metadata?.business_task_action).toEqual({
      action: 'create',
      request_id: 'create-request-1',
      task: draft.task,
    });

    const minimal = buildBusinessTaskCreatePlanDraft({
      request_id: 'create-request-2',
      title: 'Minimal assignment',
    });
    expect(minimal.task).toEqual({
      request_id: 'create-request-2',
      title: 'Minimal assignment',
      description: null,
      type: 'todo',
      priority: 'medium',
      due_date: null,
    });
    expect(minimal.plan.fill.authorization_scopes[0].arguments).toEqual(minimal.task);
  });

  it('keeps retry idempotency outside the exact action artifact and can recover it from the plan', () => {
    const task = {
      id: 'task-1',
      title: 'Prepare board report',
      description: 'Use verified figures',
      type: 'todo',
      priority: 'high',
      due_date: '2026-07-20T09:00:00.000Z',
    } as Task;
    const draft = buildBusinessTaskRetryPlanDraft(task, 'retry-request-1');

    expect(draft.plan.fill?.authorization_scopes?.[0]).toMatchObject({
      action_kind: 'start_long_task',
      target_ref: 'task:task-1:run',
      arguments: {
        task_id: 'task-1',
        title: 'Prepare board report',
        description: 'Use verified figures',
        type: 'todo',
        priority: 'high',
        due_date: '2026-07-20T09:00:00.000Z',
      },
    });
    expect(draft.plan.fill?.authorization_scopes?.[0]?.arguments).not.toHaveProperty('request_id');

    const recovered = readBusinessTaskActionPlan({
      id: 'plan-1',
      metadata: draft.plan.metadata,
      plan_json: { authorization_scopes: draft.plan.fill?.authorization_scopes },
    } as unknown as PlanRequest);
    expect(recovered).toEqual({ action: 'retry', request_id: 'retry-request-1', task_id: 'task-1' });
  });

  it('visibly separates durable assignments from the conversational Work Ledger', () => {
    queryData.tasks = [
      {
        id: 'task-1',
        agent_id: 'agent-1',
        title: 'Prepare board report',
        type: 'todo',
        status: 'failed',
        priority: 'high',
        assignee: 'self',
        created_by: 'user-1',
        request_id: 'create-request-1',
        request_hash: 'hash',
        execution_attempt: 1,
        created_at: '2026-07-12T00:00:00Z',
        updated_at: '2026-07-12T00:01:00Z',
        recovery_state: 'retry_available',
        recovery_message: 'Provider timed out',
        actions: { can_cancel: false, can_retry: true, can_reconcile: false },
        dependencies: [],
        stages: [{ id: 'terminal', label: 'Final outcome', status: 'failed' }],
      },
    ];

    const markup = renderToStaticMarkup(<MemoryRouter><AgentBusinessTasksSection agentId="agent-1" /></MemoryRouter>);

    expect(markup).toContain('data-testid="agent-business-tasks-section"');
    expect(markup).toContain('Business assignments');
    expect(markup).toContain('Work Ledger');
    expect(markup).toContain('Prepare board report');
    expect(markup).toContain('Prepare retry plan');
  });

  it('shows the original native permission controls inside assignment evidence', () => {
    queryData.detail = {
      task: { id: 'task-1', agent_id: 'agent-1', title: 'Prepare report', status: 'doing',
        last_execution_status: 'awaiting_permission', active_runtime_task_id: 'run-1', reflection_session_id: 'native-1',
        recovery_state: 'none', actions: { can_cancel: true, can_retry: false, can_reconcile: false },
        dependencies: [], stages: [] } as unknown as Task,
      logs: [], permissions: [{ session_id: 'native-1', can_resolve: true, request: {
        permission_request_id: 'permission-1', requested_by_user_id: 'owner', session_id: 'native-1',
        runtime_task_id: 'run-1', tool_name: 'fs_write', arguments: { path: 'workspace/report.md' },
        allow_session_allowed: true,
      } }],
    } as unknown as BusinessTaskDetail;
    const markup = renderToStaticMarkup(<MemoryRouter initialEntries={['/?task_id=task-1']}>
      <AgentBusinessTasksSection agentId="agent-1" /></MemoryRouter>);
    expect(markup).toContain('workspace/report.md');
    expect(markup).toContain('Allow once');
    expect(markup).toContain('Allow for this session');
    expect(markup).toContain('Deny');
    queryData.detailError = true;
    const unavailable = renderToStaticMarkup(<MemoryRouter initialEntries={['/?task_id=task-1']}>
      <AgentBusinessTasksSection agentId="agent-1" /></MemoryRouter>);
    expect(unavailable).toContain('Permission details could not be refreshed.');
    expect(unavailable).not.toContain('Allow once');
    expect(unavailable).not.toContain('workspace/report.md');
  });

});
