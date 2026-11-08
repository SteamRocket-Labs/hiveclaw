export type ApprovalExecutionLike = {
  status?: string | null;
  tool_name?: string | null;
  execution_status?: string | null;
  execution_kind?: string | null;
  execution_receipt?: {
    continuation_status?: string | null;
    continuation_reason?: string | null;
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
  reasonKey?: string;
  label: string;
  tone: 'warning' | 'success' | 'error';
};

const CONTINUATION_PRESENTATIONS: Record<string, ApprovalContinuationPresentation> = {
  queued: { status: 'queued', label: 'Continuing original session', tone: 'warning' },
  continuing: { status: 'continuing', label: 'Continuing original session', tone: 'warning' },
  delivered: { status: 'delivered', label: 'Approval result returned', tone: 'success' },
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
    label: 'Needs admin attention',
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
  if (!approval.tool_name && approval.execution_kind !== 'browser_human') {
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
  const presentation = CONTINUATION_PRESENTATIONS[status];
  const reason = approval.execution_receipt?.continuation_reason || '';
  const reasons: Record<string, string> = {
    requester_inactive: 'This account is inactive. No changes were made.',
    agent_execute_denied: 'You do not have access to this employee.',
    user_prompt_submit_blocked: 'This input was blocked by a rule. Work did not continue.',
    user_prompt_submit_prevented: 'This input was blocked by a rule. Work did not continue.',
    admission_refused: 'This input was not accepted. Work did not continue.',
    approval_return_admission_refused: 'This input was not accepted. Work did not continue.',
    active_turn_conflict_after_admission: 'This conversation is processing another input. Try again later.',
    origin_run_not_suspended: 'The conversation has stopped or ended. Work did not continue.',
    approval_origin_unavailable: 'The original conversation is unavailable. Work did not continue.',
    approval_return_unavailable: 'The approval result could not be returned. Work did not continue.',
    origin_session_authority_unresolved: 'The original conversation is unavailable. Work did not continue.',
    human_input_secret_boundary_unavailable: 'Credential protection is unavailable. Try again later.',
  };
  return presentation && status === 'needs_reconciliation' && reasons[reason]
    ? { ...presentation, label: reasons[reason], reasonKey: `reasons.${reason}` }
    : presentation || null;
}
