import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { agentApi } from '../../api/domains/agents';
import ApprovalRequestSummary from '../../components/ApprovalRequestSummary';
import {
  approvalContinuationPresentation,
  approvalExecutionPresentation,
  type ApprovalExecutionLike,
} from '../../utils/approvalExecution';
import type { ApprovalRequestLike } from '../../utils/approvalRequestPresentation';
import './AgentApprovalsSection.css';

type AgentApprovalsSectionProps = {
  agentId: string;
};

type AgentApproval = ApprovalExecutionLike & ApprovalRequestLike & {
  id: string;
  status: string;
  created_at?: string | null;
  resolved_at?: string | null;
};

export default function AgentApprovalsSection({ agentId }: AgentApprovalsSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: approvals = [], refetch: refetchApprovals } = useQuery({
    queryKey: ['agent-approvals', agentId],
    queryFn: () => agentApi.getApprovals(agentId),
    enabled: !!agentId,
    refetchInterval: 15000,
  });

  const resolveMutation = useMutation({
    mutationFn: async ({ approvalId, action }: { approvalId: string; action: string }) => {
      return agentApi.resolveApproval(agentId, approvalId, { action });
    },
    onSuccess: () => {
      refetchApprovals();
      queryClient.invalidateQueries({ queryKey: ['notifications-unread'] });
    },
  });

  const approvalRecords = approvals as AgentApproval[];
  const pending = approvalRecords.filter((approval) => approval.status === 'pending');
  const resolved = approvalRecords.filter((approval) => approval.status !== 'pending');

  const renderStatus = (approval: AgentApproval) => {
    const presentation = approvalExecutionPresentation(approval);
    return {
      ...presentation,
      text: t(`approvalExecution.${presentation.key}`, presentation.label),
    };
  };

  return (
    <div className="agent-approvals">
      {pending.length > 0 && (
        <>
          <h4 className="agent-approvals-heading is-alert">
            {t('agent.approvals.pendingCount', { count: pending.length })}
          </h4>
          {pending.map((approval) => {
            const presentation = renderStatus(approval);
            return <div key={approval.id} className="card agent-approvals-item">
              <div className="agent-approvals-row agent-approvals-row-head">
                <span className={`agent-approvals-status ${presentation.agentClassName}`}>{presentation.text}</span>
                <ApprovalRequestSummary approval={approval} />
                <span className="agent-approvals-spacer" />
                <span className="agent-approvals-time">
                  {approval.created_at ? new Date(approval.created_at).toLocaleString() : ''}
                </span>
              </div>
              <ApprovalRequestSummary approval={approval} showDetails showTitle={false} />
              <div className="agent-approvals-actions">
                <button
                  className="btn btn-primary"
                  onClick={() => resolveMutation.mutate({ approvalId: approval.id, action: 'approve' })}
                  disabled={resolveMutation.isPending}
                >
                  {t('agent.approvals.approve')}
                </button>
                <button
                  className="btn btn-danger"
                  onClick={() => resolveMutation.mutate({ approvalId: approval.id, action: 'reject' })}
                  disabled={resolveMutation.isPending}
                >
                  {t('agent.approvals.reject')}
                </button>
              </div>
            </div>;
          })}
          <div className="agent-approvals-divider" />
        </>
      )}

      <h4 className="agent-approvals-heading">
        {t('agent.approvals.history')}
      </h4>
      {resolved.length === 0 && pending.length === 0 && (
        <div className="agent-approvals-empty">
          {t('agent.approvals.noRecords')}
        </div>
      )}
      {resolved.map((approval) => {
        const presentation = renderStatus(approval);
        const continuation = approvalContinuationPresentation(approval);
        return <div key={approval.id} className="card card-pad-sm agent-approvals-item-resolved">
          <div className="agent-approvals-row">
            <span className={`agent-approvals-status ${presentation.agentClassName}`}>{presentation.text}</span>
            <ApprovalRequestSummary approval={approval} />
            <span className="agent-approvals-spacer" />
            <span className="agent-approvals-time-sm">
              {approval.resolved_at ? new Date(approval.resolved_at).toLocaleString() : ''}
            </span>
          </div>
          {continuation && (
            <div className={`agent-approvals-continuation is-${continuation.tone}`}>
              {t(`approvalContinuation.${continuation.status}`, continuation.label)}
            </div>
          )}
        </div>;
      })}
    </div>
  );
}
