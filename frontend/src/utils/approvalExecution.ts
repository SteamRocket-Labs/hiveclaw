export type ApprovalExecutionLike = {
  status?: string | null;
  tool_name?: string | null;
  execution_status?: string | null;
  execution_receipt?: {
    continuation_status?: string | null;
  } | null;
};

export type ApprovalExecutionPresentation = {
  key: string;
  label: string;
  tone: 'warning' | 'success' | 'error' | 'neutral';
  agentClassName: 'is-pending' | 'is-approved' | 'is-rejected';
};

export type ApprovalContinuationPresentation = {
  status: string;
  label: string;
  tone: 'warning' | 'success' | 'error';
};

const CONTINUATION_PRESENTATIONS: Record<string, ApprovalContinuationPresentation> = {
  queued: { status: 'queued', label: 'Continuing original session', tone: 'warning' },
  continuing: { status: 'continuing', label: 'Continuing original session', tone: 'warning' },
  delivered: { status: 'delivered', label: 'Original session resumed', tone: 'success' },
  retrying: { status: 'retrying', label: 'Continuation retrying', tone: 'warning' },
  needs_reconciliation: {
    status: 'needs_reconciliation',
    label: 'Continuation needs attention',
    tone: 'error',
  },
};

const EXECUTION_PRESENTATIONS: Record<string, ApprovalExecutionPresentation> = {
  pending: { key: 'pending', label: 'Pending', tone: 'warning', agentClassName: 'is-pending' },
  approved: { key: 'approved', label: 'Approved', tone: 'success', agentClassName: 'is-approved' },
  rejected: { key: 'rejected', label: 'Rejected', tone: 'error', agentClassName: 'is-rejected' },
  queued: { key: 'queued', label: 'Queued', tone: 'warning', agentClassName: 'is-pending' },
  executing: { key: 'executing', label: 'Executing', tone: 'warning', agentClassName: 'is-pending' },
  succeeded: { key: 'succeeded', label: 'Succeeded', tone: 'success', agentClassName: 'is-approved' },
  failed: { key: 'failed', label: 'Failed', tone: 'error', agentClassName: 'is-rejected' },
  needs_reconciliation: {
    key: 'needs_reconciliation',
    label: 'Needs reconciliation',
    tone: 'error',
    agentClassName: 'is-rejected',
  },
  needs_reapproval: {
    key: 'needs_reapproval',
    label: 'Needs reapproval',
    tone: 'warning',
    agentClassName: 'is-pending',
  },
};

export function approvalExecutionPresentation(
  approval: ApprovalExecutionLike,
): ApprovalExecutionPresentation {
  const decisionStatus = String(approval.status || 'pending').trim().toLowerCase();
  if (decisionStatus === 'pending' || decisionStatus === 'rejected') {
    return EXECUTION_PRESENTATIONS[decisionStatus];
  }
  if (!approval.tool_name) {
    return EXECUTION_PRESENTATIONS.approved;
  }
  const executionStatus = String(approval.execution_status || 'approved').trim().toLowerCase();
  return EXECUTION_PRESENTATIONS[executionStatus] || {
    key: 'unknown',
    label: 'Status unavailable',
    tone: 'neutral',
    agentClassName: 'is-pending',
  };
}

export function approvalContinuationPresentation(
  approval: ApprovalExecutionLike,
): ApprovalContinuationPresentation | null {
  const status = String(approval.execution_receipt?.continuation_status || '').trim().toLowerCase();
  return CONTINUATION_PRESENTATIONS[status] || null;
}
