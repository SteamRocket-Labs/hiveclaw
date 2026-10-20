import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { teamTasksApi, type SharedTeamTask, type TaskResponsibilityChange } from '../../api/domains/teamTasks';
import { chatApi } from '../../api/domains/chat';
import { ApiError } from '../../api/core/errors';

export default function TeamBusinessTasks({ teamId, scopeKey }: { teamId: string; scopeKey: string }) {
  const { t } = useTranslation();
  const statusLabels: Record<string, string> = {
    pending: t('agent.businessTasks.status.pending', 'Queued'), doing: t('agent.businessTasks.status.doing', 'Running'),
    done: t('agent.businessTasks.status.done', 'Completed'), blocked: t('agent.businessTasks.status.blocked', 'Blocked'),
    failed: t('agent.businessTasks.status.failed', 'Failed'), cancelled: t('agent.businessTasks.status.cancelled', 'Cancelled'),
    needs_reconciliation: t('agent.businessTasks.status.needs_reconciliation', 'Review required'),
  };
  const [offset, setOffset] = useState(0);
  const [review, setReview] = useState<SharedTeamTask | null>(null);
  const [paused, setPaused] = useState<number | null>(null);
  const [workReview, setWorkReview] = useState<SharedTeamTask | null>(null);
  const [startedWork, setStartedWork] = useState<{ sessionId: string; agentId: string } | null>(null);
  const workCommand = useRef<{ signature: string; inputId: string } | null>(null);
  const scope = useRef({ scopeKey, teamId, active: true });
  scope.current.scopeKey = scopeKey;
  scope.current.teamId = teamId;
  useEffect(() => { scope.current.active = true; return () => { scope.current.active = false; }; }, []);
  const isCurrent = (owner: { scopeKey: string; teamId: string }) => scope.current.active
    && owner.scopeKey === scope.current.scopeKey && owner.teamId === scope.current.teamId;
  const command = useRef<{ signature: string; body: TaskResponsibilityChange } | null>(null);
  const query = useQuery({ queryKey: ['team-business-tasks', scopeKey, teamId, offset], queryFn: () => teamTasksApi.list(teamId, offset), refetchInterval: 5000 });
  const handover = useMutation({ mutationFn: ({ taskId, body, owner }: { taskId: string; body: TaskResponsibilityChange; owner: { scopeKey: string; teamId: string } }) => teamTasksApi.handover(owner.teamId, taskId, body),
    onSuccess: async (result, { owner }) => { if (!isCurrent(owner)) return; setReview(null); command.current = null; setPaused(result.paused_routine_count); await query.refetch(); },
    onError: (_error, { owner }) => { if (isCurrent(owner)) void query.refetch(); } });
  const openWork = useMutation({ mutationFn: async ({ task, content, inputId, owner }: {
    task: SharedTeamTask; content: string; inputId: string; owner: { scopeKey: string; teamId: string };
  }) => ({ result: await chatApi.createSessionRun(task.agent_id, { title: task.title, content,
    input_id: inputId, idempotency_key: `session-input:${inputId}`, parts: [task.work_context!] }), task, owner }),
    onSuccess: ({ result, task, owner }) => {
      if (!isCurrent(owner)) return;
      setStartedWork({ sessionId: result.session.id, agentId: task.agent_id }); setWorkReview(null); workCommand.current = null;
    } });
  function startWork(form: HTMLFormElement) {
    if (!workReview?.work_context) return;
    const content = String(new FormData(form).get('instructions') || '').trim();
    if (!content) return;
    const signature = JSON.stringify([workReview.id, workReview.work_context, content]);
    if (workCommand.current?.signature !== signature) workCommand.current = { signature, inputId: crypto.randomUUID() };
    openWork.mutate({ task: workReview, content, inputId: workCommand.current.inputId, owner: { scopeKey, teamId } });
  }
  function submit(form: HTMLFormElement) {
    if (!review) return;
    const fields = new FormData(form);
    const proposal = { responsible_user_id: String(fields.get('responsible')), expected_updated_at: review.updated_at,
      reason: String(fields.get('reason') || '') };
    const signature = JSON.stringify(proposal);
    if (command.current?.signature !== signature) command.current = { signature, body: { ...proposal, request_id: crypto.randomUUID() } };
    handover.mutate({ taskId: review.id, body: command.current!.body, owner: { scopeKey, teamId } });
  }
  return <section aria-label={t('teamTasks.title', 'Shared team tasks')}>
    <h4>{t('teamTasks.title', 'Shared team tasks')}</h4>
    <p>{t('teamTasks.boundary', 'Leads see explicitly shared tasks; members see tasks assigned to them. Continuing work uses your own authorized employee session.')}</p>
    {(query.isError || handover.isError || openWork.isError) && <div role="alert">
      <p>{openWork.error instanceof ApiError && openWork.error.status >= 400 && openWork.error.status < 500
        ? t('teamTasks.workUnavailable', 'The goal or your access changed. Refresh and review the current shared task before starting.')
        : t('teamTasks.unknown', 'Could not confirm the change. Refresh to inspect the current task; an exact retry is safe.')}</p>
      <button type="button" className="btn btn-ghost" onClick={() => { void query.refetch(); openWork.reset(); }}>{t('common.refresh', 'Refresh')}</button>
    </div>}
    {paused !== null && <p role="status">{t('teamTasks.paused', '{{count}} routines paused for review.', { count: paused })}</p>}
    {startedWork && <a className="btn btn-secondary" href={`/agents/${startedWork.agentId}?session_id=${encodeURIComponent(startedWork.sessionId)}#chat`}>{t('teamTasks.openStartedWork', 'Open submitted work')}</a>}
    {query.isLoading && <p>{t('common.loading', 'Loading...')}</p>}
    {!query.isError && query.data?.tasks.map((task) => <article key={task.id} className="card card-pad-sm">
      <strong>{task.title}</strong><p>{task.description}</p>
      <p>{task.agent_name} · {statusLabels[task.status]}</p>
      <p>{t('teamTasks.responsibility', 'Responsible: {{name}} · Created by {{creator}}', { name: task.responsible_name, creator: task.created_by_name })}</p>
      <button type="button" className="btn btn-ghost" disabled={openWork.isPending || !task.work_context} onClick={() => { setWorkReview(task); openWork.reset(); }}>{t('teamTasks.myWork', 'Open my own work')}</button>
      {task.can_handover && <button type="button" className="btn btn-ghost" onClick={() => { setReview(task); command.current = null; handover.reset(); }}>{t('teamTasks.handover', 'Hand over responsibility')}</button>}
    </article>)}
    {!query.isError && query.data?.tasks.length === 0 && <p>{t('common.noData', 'No data')}</p>}
    {workReview && <form onSubmit={(event) => { event.preventDefault(); startWork(event.currentTarget); }}>
      <strong>{workReview.title}</strong><p>{workReview.description}</p>
      <p>{statusLabels[workReview.status]}</p>
      <p>{t('teamTasks.workBoundary', 'Start a new private work with this reviewed shared goal. Previous execution keeps its current state; private conversations, results and permissions stay with their original owners.')}</p>
      <label>{t('teamTasks.instructions', 'Your instructions')}<textarea className="form-input" name="instructions" required /></label>
      <button type="submit" className="btn btn-secondary" disabled={openWork.isPending}>{t('teamTasks.startOwnWork', 'Start in my own work')}</button>
      <button type="button" className="btn btn-ghost" disabled={openWork.isPending} onClick={() => setWorkReview(null)}>{t('common.cancel', 'Cancel')}</button>
    </form>}
    {review && !query.isError && <form onSubmit={(event) => { event.preventDefault(); submit(event.currentTarget); }}>
      <strong>{review.title}</strong>
      <p>{t('teamTasks.routines', 'Changing responsibility pauses the previous person’s routines on this employee for review. It does not rerun work or transfer their permissions.')}</p>
      <label>{t('teamTasks.newResponsible', 'New responsible member')}<select className="form-input" name="responsible" required defaultValue="">
        <option value="" disabled>{t('teamTasks.chooseMember', 'Choose an active member')}</option>
        {query.data?.assignees.map((person) => <option key={person.id} value={person.id}>{person.name}</option>)}
      </select></label>
      <label>{t('teamTasks.reason', 'Reason')}<input className="form-input" name="reason" required maxLength={1000} /></label>
      <button type="submit" className="btn btn-secondary" disabled={handover.isPending}>{t('teamTasks.confirmHandover', 'Confirm responsibility handover')}</button>
      <button type="button" className="btn btn-ghost" disabled={handover.isPending} onClick={() => { setReview(null); void query.refetch(); }}>{t('teamTasks.reviewLatest', 'Close and review the latest task')}</button>
    </form>}
    {offset > 0 && <button type="button" className="btn btn-ghost" onClick={() => setOffset(Math.max(0, offset - 10))}>{t('teamTasks.previous', 'Previous tasks')}</button>}
    {!query.isError && query.data?.next_offset != null && <button type="button" className="btn btn-ghost" onClick={() => setOffset(query.data!.next_offset!)}>{t('teamTasks.more', 'More tasks')}</button>}
  </section>;
}
