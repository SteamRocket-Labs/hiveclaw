import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { enterpriseApi } from '../api/domains/enterprise';

export type ApprovalReviewerState = {
  id: string;
  status: string;
  team_reviewers?: Array<{ user_id: string; name: string; grant_id: string | null }> | null;
};

export default function ApprovalReviewerControls({ approval, onChanged }: {
  approval: ApprovalReviewerState;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const reviewer = useMutation({
    mutationFn: ({ userId, grantId }: { userId: string; grantId?: string }) => grantId
      ? enterpriseApi.revokeApprovalReviewer(approval.id, grantId)
      : enterpriseApi.grantApprovalReviewer(approval.id, { request_id: crypto.randomUUID(), user_id: userId }),
    onSuccess: onChanged,
  });
  if (!approval.team_reviewers?.length) return null;
  return <div>
    {reviewer.isError && <div role="alert">
      <p>{t('featureHub.businessTeams.retry', 'Could not save this change. Refresh and try again.')}</p>
      <button type="button" className="btn btn-ghost" onClick={() => { reviewer.reset(); onChanged(); }}>
        {t('featureHub.businessTeams.refresh', 'Refresh')}
      </button>
    </div>}
    {approval.status === 'pending' && approval.team_reviewers.some((person) => !person.grant_id) && (
      <form onSubmit={(event) => {
        event.preventDefault();
        const userId = String(new FormData(event.currentTarget).get('reviewer') || '');
        if (userId) reviewer.mutate({ userId });
      }}>
        <select name="reviewer" aria-label={t('approvalRequest.reviewer', 'Team reviewer')} required defaultValue="">
          <option value="" disabled>{t('approvalRequest.chooseReviewer', 'Choose a team lead')}</option>
          {approval.team_reviewers.filter((person) => !person.grant_id).map((person) => (
            <option key={person.user_id} value={person.user_id}>{person.name}</option>
          ))}
        </select>
        <button type="submit" className="btn btn-ghost" disabled={reviewer.isPending}>
          {t('approvalRequest.authorizeReviewer', 'Authorize for this approval')}
        </button>
      </form>
    )}
    {approval.team_reviewers.filter((person) => person.grant_id).map((person) => (
      <button key={person.user_id} type="button" className="btn btn-ghost" disabled={reviewer.isPending}
        onClick={() => reviewer.mutate({ userId: person.user_id, grantId: person.grant_id! })}>
        {t('approvalRequest.revokeReviewer', 'Remove authorization for {{name}}', { name: person.name })}
      </button>
    ))}
  </div>;
}
