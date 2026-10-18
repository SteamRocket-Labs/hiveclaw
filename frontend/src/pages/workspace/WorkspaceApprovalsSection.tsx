import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../../api/domains/enterprise';
import ApprovalRequestSummary from '../../components/ApprovalRequestSummary';
import { approvalContinuationPresentation, approvalExecutionPresentation } from '../../utils/approvalExecution';
import type { ApprovalRequestLike } from '../../utils/approvalRequestPresentation';
import { useAuthStore } from '../../stores';
import ApprovalReviewerControls from '../../components/ApprovalReviewerControls';

interface WorkspaceApprovalsSectionProps {
  selectedTenantId: string;
  businessTeamId?: string;
}

interface WorkspaceApproval extends ApprovalRequestLike {
  id: string;
  action_type: string;
  agent_id: string;
  agent_name?: string | null;
  created_at: string;
  status: 'pending' | 'approved' | 'rejected';
  tool_name?: string | null;
  execution_status?: string | null;
  execution_receipt?: { continuation_status?: string | null } | null;
  team_reviewers?: Array<{ user_id: string; name: string; grant_id: string | null }> | null;
}

export default function WorkspaceApprovalsSection({
  selectedTenantId,
  businessTeamId,
}: WorkspaceApprovalsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const userId = useAuthStore((state) => state.user?.id);
  const queryKey = ['approvals', selectedTenantId, businessTeamId ?? null, userId];
  const list = useQuery({
    queryKey,
    queryFn: () => enterpriseApi.listApprovals(selectedTenantId || undefined, businessTeamId),
    refetchInterval: 5000,
  });
  const approvals = list.isError ? [] : list.data ?? [];

  const resolveApproval = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'approve' | 'reject' }) =>
      enterpriseApi.resolveApproval(id, { action }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
    },
  });


  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      {(list.isError || resolveApproval.isError) && (
        <div role="alert">
          <p>{t('featureHub.businessTeams.retry', 'Could not save this change. Refresh and try again.')}</p>
          <button type="button" className="btn btn-ghost" onClick={() => { resolveApproval.reset(); void list.refetch(); }}>
            {t('featureHub.businessTeams.refresh', 'Refresh')}
          </button>
        </div>
      )}
      {(approvals as WorkspaceApproval[]).map((approval) => {
        const presentation = approvalExecutionPresentation(approval);
        const continuation = approvalContinuationPresentation(approval);
        const label = t(`approvalExecution.${presentation.key}`, presentation.label);
        return <div
          key={approval.id}
          className="card"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
        >
          <div>
            <ApprovalRequestSummary
              approval={approval}
              showDetails={approval.status === 'pending'}
            />
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {approval.agent_name || `Agent ${approval.agent_id.slice(0, 8)}`} · {new Date(approval.created_at).toLocaleString()}
            </div>
            {continuation ? (
              <div className={`badge badge-${continuation.tone}`} style={{ marginTop: '4px' }}>
                {t(`approvalContinuation.${continuation.status}`, continuation.label)}
              </div>
            ) : null}
            <ApprovalReviewerControls approval={approval} onChanged={() => { void list.refetch(); }} />
          </div>
          {approval.status === 'pending' ? (
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                className="btn btn-primary"
                onClick={() => resolveApproval.mutate({ id: approval.id, action: 'approve' })}
                disabled={resolveApproval.isPending}
              >
                {t('common.confirm', 'Confirm')}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => resolveApproval.mutate({ id: approval.id, action: 'reject' })}
                disabled={resolveApproval.isPending}
              >
                {t('common.reject', 'Reject')}
              </button>
            </div>
          ) : (
            <span className={`badge badge-${presentation.tone}`}>
              {label}
            </span>
          )}
        </div>;
      })}
      {approvals.length === 0 && !list.isError && !list.isLoading ? (
        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
          {t('common.noData', 'No data')}
        </div>
      ) : null}
    </div>
  );
}
