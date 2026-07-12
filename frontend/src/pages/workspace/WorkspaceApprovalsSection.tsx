import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../../api/domains/enterprise';
import { approvalContinuationPresentation, approvalExecutionPresentation } from '../../utils/approvalExecution';

interface WorkspaceApprovalsSectionProps {
  selectedTenantId: string;
}

interface WorkspaceApproval {
  id: string;
  action_type: string;
  agent_id: string;
  agent_name?: string | null;
  created_at: string;
  status: 'pending' | 'approved' | 'rejected';
  tool_name?: string | null;
  execution_status?: string | null;
  execution_receipt?: { continuation_status?: string | null } | null;
}

export default function WorkspaceApprovalsSection({
  selectedTenantId,
}: WorkspaceApprovalsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: approvals = [] } = useQuery({
    queryKey: ['approvals', selectedTenantId],
    queryFn: () => enterpriseApi.listApprovals(selectedTenantId || undefined),
    refetchInterval: 5000,
  });

  const resolveApproval = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'approve' | 'reject' }) =>
      enterpriseApi.resolveApproval(id, { action }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['approvals', selectedTenantId] });
    },
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
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
            <div style={{ fontWeight: 500 }}>{approval.action_type}</div>
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {approval.agent_name || `Agent ${approval.agent_id.slice(0, 8)}`} · {new Date(approval.created_at).toLocaleString()}
            </div>
            {continuation ? (
              <div className={`badge badge-${continuation.tone}`} style={{ marginTop: '4px' }}>
                {t(`approvalContinuation.${continuation.status}`, continuation.label)}
              </div>
            ) : null}
          </div>
          {approval.status === 'pending' ? (
            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                className="btn btn-primary"
                onClick={() => resolveApproval.mutate({ id: approval.id, action: 'approve' })}
              >
                {t('common.confirm', 'Confirm')}
              </button>
              <button
                className="btn btn-danger"
                onClick={() => resolveApproval.mutate({ id: approval.id, action: 'reject' })}
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
      {approvals.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
          {t('common.noData', 'No data')}
        </div>
      ) : null}
    </div>
  );
}
