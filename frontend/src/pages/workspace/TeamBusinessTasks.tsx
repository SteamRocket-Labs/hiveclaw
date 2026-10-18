import { useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { teamTasksApi, type SharedTeamTask, type TaskResponsibilityChange } from '../../api/domains/teamTasks';
import { chatApi } from '../../api/domains/chat';

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
  const command = useRef<{ signature: string; body: TaskResponsibilityChange } | null>(null);
  const query = useQuery({ queryKey: ['team-business-tasks', scopeKey, teamId, offset], queryFn: () => teamTasksApi.list(teamId, offset), refetchInterval: 5000 });
  const handover = useMutation({ mutationFn: ({ taskId, body }: { taskId: string; body: TaskResponsibilityChange }) => teamTasksApi.handover(teamId, taskId, body),
    onSuccess: async (result) => { setReview(null); command.current = null; setPaused(result.paused_routine_count); await query.refetch(); },
    onError: () => { void query.refetch(); } });
  const openWork = useMutation({ mutationFn: (task: SharedTeamTask) => chatApi.createSession(task.agent_id),
    onSuccess: (session, task) => { window.location.href = `/agents/${task.agent_id}?session_id=${encodeURIComponent(session.id)}#chat`; } });
  function submit(form: HTMLFormElement) {
    if (!review) return;
    const fields = new FormData(form);
    const proposal = { responsible_user_id: String(fields.get('responsible')), expected_updated_at: review.updated_at,
      reason: String(fields.get('reason') || '') };
    const signature = JSON.stringify(proposal);
    if (command.current?.signature !== signature) command.current = { signature, body: { ...proposal, request_id: crypto.randomUUID() } };
    handover.mutate({ taskId: review.id, body: command.current!.body });
  }
  return <section aria-label={t('teamTasks.title', 'Shared team tasks')}>
    <h4>{t('teamTasks.title', 'Shared team tasks')}</h4>
    <p>{t('teamTasks.boundary', 'Leads see explicitly shared tasks; members see tasks assigned to them. Continuing work uses your own authorized employee session.')}</p>
    {(query.isError || handover.isError || openWork.isError) && <div role="alert">
      <p>{t('teamTasks.unknown', 'Could not confirm the change. Refresh to inspect the current task; an exact retry is safe.')}</p>
      <button type="button" className="btn btn-ghost" onClick={() => { void query.refetch(); openWork.reset(); }}>{t('common.refresh', 'Refresh')}</button>
    </div>}
    {paused !== null && <p role="status">{t('teamTasks.paused', '{{count}} routines paused for review.', { count: paused })}</p>}
    {query.isLoading && <p>{t('common.loading', 'Loading...')}</p>}
    {!query.isError && query.data?.tasks.map((task) => <article key={task.id} className="card card-pad-sm">
      <strong>{task.title}</strong><p>{task.description}</p>
      <p>{task.agent_name} · {statusLabels[task.status]}</p>
      <p>{t('teamTasks.responsibility', 'Responsible: {{name}} · Created by {{creator}}', { name: task.responsible_name, creator: task.created_by_name })}</p>
      <button type="button" className="btn btn-ghost" disabled={openWork.isPending} onClick={() => openWork.mutate(task)}>{t('teamTasks.myWork', 'Open my own work')}</button>
      {task.can_handover && <button type="button" className="btn btn-ghost" onClick={() => { setReview(task); command.current = null; handover.reset(); }}>{t('teamTasks.handover', 'Hand over responsibility')}</button>}
    </article>)}
    {!query.isError && query.data?.tasks.length === 0 && <p>{t('common.noData', 'No data')}</p>}
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
