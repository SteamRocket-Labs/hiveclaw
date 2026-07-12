import React from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { ApiError } from '../../api/core';
import {
  taskApi,
  type TaskCreateParams,
  type TaskTriggerParams,
} from '../../api/domains/tasks';
import {
  planApi,
  type PlanCreateInput,
  type PlanRequest,
} from '../../api/domains/plans';
import type { Task } from '../../types';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import './AgentBusinessTasksSection.css';

type BusinessTaskAction =
  | { action: 'create'; request_id: string; task: TaskCreateParams }
  | { action: 'retry'; request_id: string; task_id: string };

interface BusinessTaskAuthorizationScope {
  action_kind: 'start_long_task';
  target_ref: string;
  arguments: Record<string, unknown>;
  summary: string;
  max_uses: 1;
}

interface BusinessTaskPlanInput extends PlanCreateInput {
  fill: { authorization_scopes: BusinessTaskAuthorizationScope[] };
  metadata: { business_task_action: BusinessTaskAction };
}

export interface BusinessTaskPlanDraft {
  task?: TaskCreateParams;
  plan: BusinessTaskPlanInput;
}

const OPEN_PLAN_STATUSES = new Set([
  'draft',
  'planning',
  'planning_failed',
  'awaiting_confirmation',
  'confirmed',
]);

function cleanTaskCreate(input: TaskCreateParams): TaskCreateParams {
  return {
    request_id: input.request_id,
    title: input.title.trim(),
    description: input.description?.trim() || null,
    type: 'todo',
    priority: input.priority || 'medium',
    due_date: input.due_date || null,
  };
}

export function buildBusinessTaskCreatePlanDraft(input: TaskCreateParams): BusinessTaskPlanDraft {
  const task = cleanTaskCreate(input);
  const action: BusinessTaskAction = { action: 'create', request_id: task.request_id, task };
  const scope: BusinessTaskAuthorizationScope = {
    action_kind: 'start_long_task',
    target_ref: 'task:new',
    arguments: { ...task },
    summary: `Start the business assignment “${task.title}”`,
    max_uses: 1,
  };
  return {
    task,
    plan: {
      original_request: `Prepare a safe execution plan for this business assignment:\n\n${task.title}\n\n${task.description || ''}`,
      intent_type: 'long_task',
      source: 'business_task_workbench',
      fill: { authorization_scopes: [scope] },
      metadata: { business_task_action: action },
    },
  };
}

export function buildBusinessTaskRetryPlanDraft(task: Task, requestId: string): BusinessTaskPlanDraft {
  const argumentsPayload = {
    task_id: task.id,
    title: task.title,
    description: task.description || null,
    type: task.type,
    priority: task.priority,
    due_date: task.due_date || null,
  };
  const action: BusinessTaskAction = { action: 'retry', request_id: requestId, task_id: task.id };
  return {
    plan: {
      original_request: `Prepare a retry plan for the failed business assignment “${task.title}”. Review the prior failure before authorizing another attempt.`,
      intent_type: 'long_task',
      source: 'business_task_workbench',
      fill: {
        authorization_scopes: [
          {
            action_kind: 'start_long_task',
            target_ref: `task:${task.id}:run`,
            arguments: argumentsPayload,
            summary: `Retry the business assignment “${task.title}”`,
            max_uses: 1,
          },
        ],
      },
      metadata: { business_task_action: action },
    },
  };
}

export function readBusinessTaskActionPlan(plan: PlanRequest): BusinessTaskAction | null {
  const value = plan.metadata?.business_task_action;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const requestId = typeof record.request_id === 'string' ? record.request_id.trim() : '';
  if (record.action === 'create' && requestId && record.task && typeof record.task === 'object') {
    const task = record.task as TaskCreateParams;
    if (typeof task.title === 'string' && typeof task.request_id === 'string') {
      return { action: 'create', request_id: requestId, task: cleanTaskCreate(task) };
    }
  }
  const taskId = typeof record.task_id === 'string' ? record.task_id.trim() : '';
  if (record.action === 'retry' && requestId && taskId) {
    return { action: 'retry', request_id: requestId, task_id: taskId };
  }
  return null;
}

function requestId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`.slice(0, 128);
}

function statusFallback(status: string): string {
  return {
    pending: 'Queued',
    doing: 'Running',
    done: 'Completed',
    blocked: 'Blocked',
    failed: 'Failed',
    cancelled: 'Cancelled',
    needs_reconciliation: 'Review required',
  }[status] || 'Queued';
}

function planStatusFallback(status: string): string {
  return {
    draft: 'Draft',
    planning: 'Preparing',
    planning_failed: 'Plan failed',
    awaiting_confirmation: 'Needs confirmation',
    confirmed: 'Confirmed',
  }[status] || 'Preparing';
}

function recoveryFallback(state: string): string {
  return {
    none: 'No recovery action',
    needs_review: 'Needs review',
    retry_available: 'Retry available',
    complete: 'Complete',
    cancelled: 'Cancelled',
    runtime_evidence_missing: 'Runtime evidence missing',
  }[state] || 'No recovery action';
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError || error instanceof Error) return error.message;
  return fallback;
}

export default function AgentBusinessTasksSection({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [selectedTaskId, setSelectedTaskId] = React.useState<string | null>(null);
  const [title, setTitle] = React.useState('');
  const [description, setDescription] = React.useState('');
  const [priority, setPriority] = React.useState<TaskCreateParams['priority']>('medium');
  const [dueDate, setDueDate] = React.useState('');
  const [busyKey, setBusyKey] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const tasksQuery = useQuery({
    queryKey: ['business-tasks', agentId],
    queryFn: () => taskApi.list(agentId),
    refetchInterval: (query) =>
      (query.state.data || []).some((task) => task.actions?.can_cancel) ? 5_000 : false,
    refetchOnWindowFocus: true,
  });
  const plansQuery = useQuery({
    queryKey: ['business-task-plans', agentId],
    queryFn: () => planApi.list(agentId),
    refetchInterval: (query) =>
      (query.state.data || []).some((plan) => ['draft', 'planning'].includes(plan.status)) ? 4_000 : false,
    refetchOnWindowFocus: true,
  });
  const detailQuery = useQuery({
    queryKey: ['business-task-detail', agentId, selectedTaskId],
    queryFn: () => taskApi.get(agentId, selectedTaskId as string),
    enabled: Boolean(selectedTaskId),
    refetchInterval: selectedTaskId ? 5_000 : false,
  });

  const tasks = tasksQuery.data || [];
  const consumedRequests = new Set(
    tasks.flatMap((task) => [task.request_id, task.runtime_request_id].filter(Boolean) as string[]),
  );
  const actionPlans = (plansQuery.data || [])
    .map((plan) => ({ plan, action: readBusinessTaskActionPlan(plan) }))
    .filter(
      (item): item is { plan: PlanRequest; action: BusinessTaskAction } =>
        Boolean(item.action && OPEN_PLAN_STATUSES.has(item.plan.status) && !consumedRequests.has(item.action.request_id)),
    );

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['business-tasks', agentId] }),
      queryClient.invalidateQueries({ queryKey: ['business-task-plans', agentId] }),
      selectedTaskId
        ? queryClient.invalidateQueries({ queryKey: ['business-task-detail', agentId, selectedTaskId] })
        : Promise.resolve(),
    ]);
  };

  const prepareCreatePlan = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!title.trim() || busyKey) return;
    setBusyKey('create-plan');
    setError(null);
    try {
      const draft = buildBusinessTaskCreatePlanDraft({
        request_id: requestId('business-create'),
        title,
        description,
        priority,
        due_date: dueDate ? new Date(dueDate).toISOString() : undefined,
      });
      await planApi.create(agentId, draft.plan);
      setTitle('');
      setDescription('');
      setDueDate('');
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught, t('agent.businessTasks.errors.preparePlan', 'Could not prepare the assignment plan.')));
    } finally {
      setBusyKey(null);
    }
  };

  const prepareRetryPlan = async (task: Task) => {
    const key = `retry-plan:${task.id}`;
    if (busyKey) return;
    setBusyKey(key);
    setError(null);
    try {
      await planApi.create(agentId, buildBusinessTaskRetryPlanDraft(task, requestId('business-retry')).plan);
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught, t('agent.businessTasks.errors.prepareRetry', 'Could not prepare the retry plan.')));
    } finally {
      setBusyKey(null);
    }
  };

  const confirmAndStart = async (plan: PlanRequest, action: BusinessTaskAction) => {
    const key = `start:${plan.id}`;
    if (busyKey || !plan.plan_hash) return;
    setBusyKey(key);
    setError(null);
    try {
      if (plan.status === 'awaiting_confirmation') {
        await planApi.confirm(agentId, plan.id, {
          plan_version: plan.plan_version,
          plan_hash: plan.plan_hash,
        });
      }
      const provenance: TaskTriggerParams = {
        request_id: action.request_id,
        confirmed_plan_id: plan.id,
        confirmed_plan_version: plan.plan_version,
        confirmed_plan_hash: plan.plan_hash,
        confirmed_plan_session_id: plan.session_id || undefined,
      };
      if (action.action === 'create') {
        await taskApi.create(agentId, { ...action.task, ...provenance });
      } else {
        await taskApi.retry(agentId, action.task_id, provenance);
        setSelectedTaskId(action.task_id);
      }
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught, t('agent.businessTasks.errors.start', 'The confirmed assignment could not be started. Retry safely with the same plan.')));
    } finally {
      setBusyKey(null);
    }
  };

  const rejectPlan = async (plan: PlanRequest) => {
    if (busyKey) return;
    setBusyKey(`reject:${plan.id}`);
    setError(null);
    try {
      await planApi.reject(agentId, plan.id, { reason: 'Cancelled from the Business assignments workbench.' });
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught, t('agent.businessTasks.errors.discardPlan', 'Could not discard this plan.')));
    } finally {
      setBusyKey(null);
    }
  };

  const cancelTask = async (task: Task, event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busyKey) return;
    const reason = String(new FormData(event.currentTarget).get('reason') || '').trim();
    setBusyKey(`cancel:${task.id}`);
    setError(null);
    try {
      await taskApi.cancel(agentId, task.id, { reason });
      setSelectedTaskId(task.id);
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught, t('agent.businessTasks.errors.stop', 'Could not stop this assignment.')));
    } finally {
      setBusyKey(null);
    }
  };

  const reconcileTask = async (task: Task, event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busyKey) return;
    const form = new FormData(event.currentTarget);
    const reason = String(form.get('reason') || '').trim();
    const decision = form.get('decision') === 'close_without_retry' ? 'close_without_retry' : 'retry_safe';
    if (!reason) {
      setError(t('agent.businessTasks.errors.reconciliationReason', 'Describe what you verified before resolving this assignment.'));
      return;
    }
    setBusyKey(`reconcile:${task.id}`);
    setError(null);
    try {
      await taskApi.reconcile(agentId, task.id, { decision, reason });
      setSelectedTaskId(task.id);
      await refresh();
    } catch (caught) {
      setError(errorMessage(caught, t('agent.businessTasks.errors.reconcile', 'Could not record the reconciliation decision.')));
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <section className="business-tasks" data-testid="agent-business-tasks-section">
      <header className="business-tasks-header">
        <div>
          <span className="business-tasks-eyebrow">{t('agent.businessTasks.eyebrow', 'Durable work')}</span>
          <h2>{t('agent.businessTasks.title', 'Business assignments')}</h2>
          <p>{t('agent.businessTasks.subtitle', 'Create, monitor, stop, and safely retry work assigned to this Agent.')}</p>
        </div>
        <span className="business-tasks-count">{tasks.length}</span>
      </header>

      <div className="business-tasks-boundary" role="note">
        <strong>{t('agent.businessTasks.boundaryTitle', 'Two different task systems')}</strong>
        <span>
          {t(
            'agent.businessTasks.boundaryBody',
            'Business assignments are durable work you send to the Agent. The Work Ledger is the Agent’s private step list inside one conversation; it never starts work by itself.',
          )}
        </span>
      </div>

      {error && <div className="business-tasks-error" role="alert">{error}</div>}

      {actionPlans.length > 0 && (
        <div className="business-task-plans" data-testid="business-task-plan-queue">
          <h3>{t('agent.businessTasks.planQueue', 'Plans waiting for your decision')}</h3>
          {actionPlans.map(({ plan, action }) => (
            <article key={plan.id} className="business-task-plan">
              <div className="business-task-plan-meta">
                <span>
                  {action.action === 'create'
                    ? t('agent.businessTasks.newAssignment', 'New assignment')
                    : t('agent.businessTasks.retryReview', 'Retry review')}
                </span>
                <span>{t(`agent.businessTasks.planStatus.${plan.status}`, planStatusFallback(plan.status))}</span>
              </div>
              <h4>
                {plan.plan_json?.title
                  || (action.action === 'create'
                    ? action.task.title
                    : t('agent.businessTasks.retryAssignment', 'Retry assignment'))}
              </h4>
              {plan.plan_json?.plan_markdown && <MarkdownRenderer content={plan.plan_json.plan_markdown} />}
              {plan.status === 'planning_failed' && (
                <button type="button" className="btn btn-secondary" onClick={() => planApi.regenerate(agentId, plan.id).then(refresh)}>
                  {t('agent.businessTasks.retryPlanGeneration', 'Retry plan generation')}
                </button>
              )}
              {(plan.status === 'awaiting_confirmation' || plan.status === 'confirmed') && (
                <div className="business-task-plan-actions">
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={busyKey !== null || !plan.plan_hash}
                    onClick={() => confirmAndStart(plan, action)}
                  >
                    {plan.status === 'confirmed'
                      ? t('agent.businessTasks.resumeStart', 'Resume start')
                      : t('agent.businessTasks.confirmStart', 'Confirm and start')}
                  </button>
                  {plan.status === 'awaiting_confirmation' && (
                    <button type="button" className="btn btn-secondary" disabled={busyKey !== null} onClick={() => rejectPlan(plan)}>
                      {t('agent.businessTasks.discard', 'Discard')}
                    </button>
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      <div className="business-tasks-layout">
        <div className="business-task-list" aria-live="polite">
          <h3>{t('agent.businessTasks.assignments', 'Assignments')}</h3>
          {tasksQuery.isLoading ? (
            <div className="business-task-empty">{t('agent.businessTasks.loading', 'Loading assignments…')}</div>
          ) : tasksQuery.isError ? (
            <div className="business-task-empty is-error">{t('agent.businessTasks.loadFailed', 'Assignments could not be loaded.')}</div>
          ) : tasks.length === 0 ? (
            <div className="business-task-empty">{t('agent.businessTasks.empty', 'No business assignments yet.')}</div>
          ) : (
            tasks.map((task) => (
              <article key={task.id} className={`business-task-row is-${task.status}`}>
                <button type="button" className="business-task-row-main" onClick={() => setSelectedTaskId(task.id)}>
                  <span className={`business-task-status is-${task.status}`}>
                    {t(`agent.businessTasks.status.${task.status}`, statusFallback(task.status))}
                  </span>
                  <strong>{task.title}</strong>
                  <span>{task.description || t('agent.businessTasks.noDescription', 'No description')}</span>
                  <small>
                    {t('agent.businessTasks.attemptPriority', 'Attempt {{attempt}} · {{priority}} priority', {
                      attempt: task.execution_attempt,
                      priority: t(`agent.businessTasks.priority.${task.priority}`, task.priority),
                    })}
                  </small>
                </button>
                <ol className="business-task-stages" aria-label={t('agent.businessTasks.executionStages', 'Execution stages')}>
                  {(task.stages || []).map((stage) => (
                    <li
                      key={stage.id}
                      className={`is-${stage.status}`}
                      title={t(`agent.businessTasks.stage.${stage.id}`, stage.label)}
                    >
                      <span />
                      {t(`agent.businessTasks.stage.${stage.id}`, stage.label)}
                    </li>
                  ))}
                </ol>
                {task.recovery_message && task.status !== 'done' && (
                  <div className="business-task-recovery">{task.recovery_message}</div>
                )}
                <div className="business-task-actions">
                  {task.actions?.can_retry && (
                    <button type="button" className="btn btn-secondary" disabled={busyKey !== null} onClick={() => prepareRetryPlan(task)}>
                      {t('agent.businessTasks.prepareRetryPlan', 'Prepare retry plan')}
                    </button>
                  )}
                  {task.actions?.can_cancel && (
                    <details>
                      <summary>{t('agent.businessTasks.stopAssignment', 'Stop assignment')}</summary>
                      <form onSubmit={(event) => cancelTask(task, event)}>
                        <textarea name="reason" rows={2} placeholder={t('agent.businessTasks.stopReason', 'Reason (optional)')} />
                        <button type="submit" className="btn btn-secondary" disabled={busyKey !== null}>
                          {t('agent.businessTasks.stopSafely', 'Stop safely')}
                        </button>
                      </form>
                    </details>
                  )}
                  {task.actions?.can_reconcile && (
                    <details open>
                      <summary>{t('agent.businessTasks.reviewEffects', 'Review possible side effects')}</summary>
                      <form onSubmit={(event) => reconcileTask(task, event)}>
                        <textarea name="reason" rows={3} required placeholder={t('agent.businessTasks.whatVerified', 'What did you verify?')} />
                        <select name="decision" defaultValue="retry_safe">
                          <option value="retry_safe">{t('agent.businessTasks.noSideEffect', 'No side effect occurred — allow retry')}</option>
                          <option value="close_without_retry">{t('agent.businessTasks.closeWithoutRetry', 'Close without retry')}</option>
                        </select>
                        <button type="submit" className="btn btn-secondary" disabled={busyKey !== null}>
                          {t('agent.businessTasks.recordDecision', 'Record decision')}
                        </button>
                      </form>
                    </details>
                  )}
                </div>
              </article>
            ))
          )}
        </div>

        <aside className="business-task-sidebar">
          <form className="business-task-create" onSubmit={prepareCreatePlan}>
            <h3>{t('agent.businessTasks.newAssignment', 'New assignment')}</h3>
            <p>{t('agent.businessTasks.prepareNotice', 'Preparing a plan does not start execution. You review the plan first.')}</p>
            <label>
              <span>{t('agent.businessTasks.fieldTitle', 'Title')}</span>
              <input value={title} onChange={(event) => setTitle(event.currentTarget.value)} maxLength={500} required />
            </label>
            <label>
              <span>{t('agent.businessTasks.deliverable', 'What should be delivered?')}</span>
              <textarea value={description} onChange={(event) => setDescription(event.currentTarget.value)} rows={5} />
            </label>
            <div className="business-task-create-grid">
              <label>
                <span>{t('agent.businessTasks.fieldPriority', 'Priority')}</span>
                <select value={priority} onChange={(event) => setPriority(event.currentTarget.value as TaskCreateParams['priority'])}>
                  <option value="low">{t('agent.businessTasks.priority.low', 'Low')}</option>
                  <option value="medium">{t('agent.businessTasks.priority.medium', 'Medium')}</option>
                  <option value="high">{t('agent.businessTasks.priority.high', 'High')}</option>
                  <option value="urgent">{t('agent.businessTasks.priority.urgent', 'Urgent')}</option>
                </select>
              </label>
              <label>
                <span>{t('agent.businessTasks.due', 'Due')}</span>
                <input type="datetime-local" value={dueDate} onChange={(event) => setDueDate(event.currentTarget.value)} />
              </label>
            </div>
            <button type="submit" className="btn btn-primary" disabled={!title.trim() || busyKey !== null}>
              {busyKey === 'create-plan'
                ? t('agent.businessTasks.preparing', 'Preparing…')
                : t('agent.businessTasks.preparePlan', 'Prepare execution plan')}
            </button>
          </form>

          {selectedTaskId && (
            <div className="business-task-detail" data-testid="business-task-detail">
              <h3>{t('agent.businessTasks.evidence', 'Assignment evidence')}</h3>
              {detailQuery.isLoading ? (
                <p>{t('agent.businessTasks.loadingEvidence', 'Loading evidence…')}</p>
              ) : detailQuery.data ? (
                <>
                  <dl>
                    <div>
                      <dt>{t('agent.businessTasks.run', 'Run')}</dt>
                      <dd>{detailQuery.data.task.active_runtime_task_id?.slice(0, 8) || t('agent.businessTasks.notQueued', 'Not queued')}</dd>
                    </div>
                    <div>
                      <dt>{t('agent.businessTasks.state', 'State')}</dt>
                      <dd>{t(`agent.businessTasks.status.${detailQuery.data.task.status}`, statusFallback(detailQuery.data.task.status))}</dd>
                    </div>
                    <div>
                      <dt>{t('agent.businessTasks.recovery', 'Recovery')}</dt>
                      <dd>{t(
                        `agent.businessTasks.recoveryState.${detailQuery.data.task.recovery_state}`,
                        recoveryFallback(detailQuery.data.task.recovery_state),
                      )}</dd>
                    </div>
                  </dl>
                  {detailQuery.data.task.last_result && <pre>{detailQuery.data.task.last_result}</pre>}
                  {detailQuery.data.logs.length > 0 && (
                    <ol>
                      {detailQuery.data.logs.map((log) => <li key={log.id}>{log.content}</li>)}
                    </ol>
                  )}
                </>
              ) : (
                <p>{t('agent.businessTasks.selectEvidence', 'Select an assignment to inspect its evidence.')}</p>
              )}
            </div>
          )}
        </aside>
      </div>
    </section>
  );
}
