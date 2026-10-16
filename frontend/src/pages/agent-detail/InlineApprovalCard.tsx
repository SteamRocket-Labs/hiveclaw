import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { chatApi } from '../../api/domains/chat';
import ApprovalRequestSummary from '../../components/ApprovalRequestSummary';
import { approvalContinuationPresentation, approvalExecutionPresentation } from '../../utils/approvalExecution';

export default function InlineApprovalCard({ agentId, sessionId, approvalId }: {
  agentId: string; sessionId: string; approvalId: string;
}) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const key = ['session-approval', agentId, sessionId, approvalId] as const;
  const query = useQuery({
    queryKey: key,
    queryFn: () => chatApi.getApproval(agentId, sessionId, approvalId),
    retry: false,
    refetchInterval: (current) => {
      const data = current.state.data;
      if (!data) return false;
      return data.status === 'pending' || ['queued', 'executing'].includes(data.execution_status || '')
        || ['queued', 'continuing', 'retrying'].includes(data.execution_receipt?.continuation_status || '') ? 3_000 : false;
    },
  });
  const resolve = useMutation({
    mutationFn: (action: 'approve' | 'reject') => chatApi.resolveApproval(agentId, sessionId, approvalId, action),
    onSuccess: (data) => client.setQueryData(key, data),
    onError: async (error) => {
      setActionError(error.message);
      // Re-read the durable decision after a lost response before offering another action.
      await query.refetch();
    },
  });
  const executionStatus = query.data?.execution_status;
  useEffect(() => {
    if (executionStatus && ['succeeded', 'failed', 'needs_reapproval', 'needs_reconciliation'].includes(executionStatus)) {
      void client.invalidateQueries({ queryKey: ['hr-creation-draft', agentId] });
      void client.invalidateQueries({ queryKey: ['hr-recoverable-drafts'] });
    }
  }, [client, agentId, executionStatus]);

  if (!query.data) return <div role="status">
    {query.isError ? t('agent.chat.approval.loadFailed', 'Approval status could not load.') : t('common.loading', 'Loading…')}
    {query.isError && <button type="button" className="btn btn-secondary" onClick={() => void query.refetch()}>
      {t('common.retry', 'Retry')}
    </button>}
  </div>;

  const data = query.data;
  const execution = approvalExecutionPresentation(data);
  const continuation = approvalContinuationPresentation(data);
  const canDecide = data.can_resolve && data.status === 'pending' && !query.isError;
  return <div className="card card-pad-sm" style={{ display: 'grid', gap: '8px' }}>
    <strong>{t('agent.chat.approval.title', 'Company approval')}</strong>
    <ApprovalRequestSummary approval={data} showDetails />
    <div role="status">{t(`approvalExecution.${execution.key}`, execution.label)}</div>
    {continuation && <div>{t(`approvalContinuation.${continuation.status}`, continuation.label)}</div>}
    {data.status === 'pending' && !data.can_resolve && <p>{t('agent.chat.approval.waiting', 'Waiting for the responsible approver.')}</p>}
    {actionError && <div role="alert">{actionError}</div>}
    {query.isError && <div role="alert">
      {t('agent.chat.approval.loadFailed', 'Approval status could not load.')}
      <button type="button" className="btn btn-secondary" onClick={() => void query.refetch()}>{t('common.retry', 'Retry')}</button>
    </div>}
    {canDecide && <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px' }}>
      {(['approve', 'reject'] as const).map((action) => <button key={action} type="button"
        className={`btn ${action === 'approve' ? 'btn-primary' : 'btn-danger'}`}
        disabled={resolve.isPending || query.isFetching}
        onClick={() => { setActionError(null); resolve.mutate(action); }}>
        {action === 'approve' ? t('agent.approvals.approve', 'Approve') : t('agent.approvals.reject', 'Reject')}
      </button>)}
    </div>}
  </div>;
}
