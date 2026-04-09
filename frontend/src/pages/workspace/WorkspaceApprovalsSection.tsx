import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../../api/domains/enterprise';

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
}

export default function WorkspaceApprovalsSection({
  selectedTenantId,
}: WorkspaceApprovalsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: approvals = [] } = useQuery({
    queryKey: ['approvals', selectedTenantId],
    queryFn: () => enterpriseApi.listApprovals(selectedTenantId || undefined),
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
      {(approvals as WorkspaceApproval[]).map((approval) => (
        <div
          key={approval.id}
          className="card"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}
        >
          <div>
            <div style={{ fontWeight: 500 }}>{approval.action_type}</div>
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {approval.agent_name || `Agent ${approval.agent_id.slice(0, 8)}`} · {new Date(approval.created_at).toLocaleString()}
            </div>
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
            <span className={`badge ${approval.status === 'approved' ? 'badge-success' : 'badge-error'}`}>
              {approval.status === 'approved' ? t('common.approved', 'Approved') : t('common.rejected', 'Rejected')}
            </span>
          )}
        </div>
      ))}
      {approvals.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
          {t('common.noData', 'No data')}
        </div>
      ) : null}
    </div>
  );
}
