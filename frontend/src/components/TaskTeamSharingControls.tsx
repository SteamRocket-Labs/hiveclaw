import { useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { businessTeamsApi } from '../api/domains/businessTeams';
import { teamTasksApi, type TaskSharingChange } from '../api/domains/teamTasks';
import type { Task } from '../types';

export default function TaskTeamSharingControls({ task, scopeKey, onChanged }: {
  task: Task; scopeKey: string; onChanged: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [review, setReview] = useState<{ task: Task; mode: 'share' | 'revoke' } | null>(null);
  const [teamId, setTeamId] = useState('');
  const command = useRef<{ signature: string; body: TaskSharingChange } | null>(null);
  const teams = useQuery({ queryKey: ['task-sharing-teams', scopeKey], queryFn: businessTeamsApi.list, enabled: review?.mode === 'share' });
  const team = useQuery({ queryKey: ['task-sharing-team', scopeKey, teamId], queryFn: () => businessTeamsApi.read(teamId), enabled: review?.mode === 'share' && Boolean(teamId) });
  const change = useMutation({ mutationFn: (body: TaskSharingChange) => teamTasksApi.share(task.agent_id, task.id, body),
    onSuccess: async () => { setReview(null); command.current = null; await onChanged(); }, onError: onChanged });
  function open(mode: 'share' | 'revoke') {
    setReview({ task, mode }); setTeamId(task.business_team_id || ''); command.current = null; change.reset();
  }
  function submit(form: HTMLFormElement) {
    if (!review) return;
    const fields = new FormData(form);
    const proposal = { business_team_id: review.mode === 'share' ? teamId : null,
      responsible_user_id: review.mode === 'share' ? String(fields.get('responsible')) : null,
      expected_updated_at: review.task.updated_at, reason: String(fields.get('reason') || '') };
    const signature = JSON.stringify(proposal);
    if (command.current?.signature !== signature) command.current = { signature, body: { ...proposal, request_id: crypto.randomUUID() } };
    change.mutate(command.current!.body);
  }
  return <div>
    <button type="button" className="btn btn-ghost" onClick={() => open('share')}>
      {task.business_team_id ? t('teamTasks.updateShared', 'Review and update shared details') : t('teamTasks.share', 'Share task with a team')}
    </button>
    {task.business_team_id && <button type="button" className="btn btn-ghost" onClick={() => open('revoke')}>
      {t('teamTasks.makePrivate', 'Make private and return responsibility to the creator')}
    </button>}
    {review && <form onSubmit={(event) => { event.preventDefault(); submit(event.currentTarget); }}>
      <p>{t('teamTasks.disclosure', 'Share only this reviewed title and goal, status and responsibility. Private conversations, logs, results and files keep their own permissions. Later goal edits require another explicit share.')}</p>
      <strong>{review.task.title}</strong><p>{review.task.description}</p>
      <p>{t('teamTasks.routines', 'Changing responsibility pauses the previous person’s routines on this employee for review. It does not rerun work or transfer their permissions.')}</p>
      {review.mode === 'share' && <>
        <label>{t('teamTasks.team', 'Team')}<select className="form-input" required value={teamId} onChange={(event) => setTeamId(event.target.value)}>
          <option value="">{t('teamTasks.chooseTeam', 'Choose a team')}</option>
          {!teams.isError && teams.data?.teams.filter((value) => value.status === 'active').map((value) => <option key={value.id} value={value.id}>{value.name}</option>)}
        </select></label>
        <label>{t('teamTasks.responsible', 'Responsible member')}<select key={teamId} className="form-input" name="responsible" required defaultValue={review.task.responsible_user_id || review.task.created_by}>
          <option value="">{t('teamTasks.chooseMember', 'Choose an active member')}</option>
          {!team.isError && team.data?.members.filter((person) => person.authority && person.user_id).map((person) => <option key={person.id} value={person.user_id!}>{person.display_name}</option>)}
        </select></label>
      </>}
      <label>{t('teamTasks.reason', 'Reason')}<input className="form-input" name="reason" required maxLength={1000} /></label>
      {(change.isError || teams.isError || team.isError) && <p role="alert">{t('teamTasks.unknown', 'Could not confirm the change. Refresh to inspect the current task; an exact retry is safe.')}</p>}
      <button type="submit" className="btn btn-secondary" disabled={change.isPending || (review.mode === 'share' && (!team.data || team.isError || teams.isError))}>
        {review.mode === 'share' ? t('teamTasks.confirmShare', 'Share reviewed task details') : t('teamTasks.confirmPrivate', 'Confirm private work and creator responsibility')}
      </button>
      <button type="button" className="btn btn-ghost" disabled={change.isPending} onClick={() => { setReview(null); void onChanged(); }}>
        {t('teamTasks.reviewLatest', 'Close and review the latest task')}
      </button>
    </form>}
  </div>;
}
