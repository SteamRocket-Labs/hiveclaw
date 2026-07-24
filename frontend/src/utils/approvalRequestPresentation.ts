export type ApprovalRequestLike = {
  action_type?: unknown;
  details?: unknown;
};

export type ApprovalRequestField = {
  key: 'command' | 'attachments';
  value: string | number;
  code: boolean;
};

export type ApprovalRequestPresentation = {
  actionKey: 'commandEscalation' | 'localAgentDispatch' | 'planAuthorization' | 'employeeAction';
  actionFallback: string;
  description: string | null;
  fields: ApprovalRequestField[];
};

const ACTION_PRESENTATIONS: Record<
  string,
  Pick<ApprovalRequestPresentation, 'actionKey' | 'actionFallback'>
> = {
  'workspace.command.escalation': {
    actionKey: 'commandEscalation',
    actionFallback: 'Run one workspace command',
  },
  'local_agent.execute': {
    actionKey: 'localAgentDispatch',
    actionFallback: 'Send work to a connected local employee',
  },
  plan_authorization: {
    actionKey: 'planAuthorization',
    actionFallback: 'Use a confirmed plan authorization',
  },
};

const FALLBACK_ACTION: Pick<ApprovalRequestPresentation, 'actionKey' | 'actionFallback'> = {
  actionKey: 'employeeAction',
  actionFallback: 'Review an employee action',
};

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function readText(details: Record<string, unknown>, key: 'reason' | 'summary' | 'command'): string | null {
  const value = details[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/**
 * Product-safe projection of an approval request.
 *
 * ApprovalRequest.details is an audit/execution envelope, not a UI contract. It
 * can contain UUIDs, hashes, raw tool arguments, policy snapshots, and secret-
 * bearing execution metadata. Keep that evidence in the governed audit path and
 * explicitly allow only fields an approver needs to make the business decision.
 */
export function approvalRequestPresentation(
  approval: ApprovalRequestLike,
): ApprovalRequestPresentation {
  const actionType = typeof approval.action_type === 'string' ? approval.action_type.trim() : '';
  const action = ACTION_PRESENTATIONS[actionType] || FALLBACK_ACTION;
  const details = asRecord(approval.details);
  const description = readText(details, actionType === 'plan_authorization' ? 'summary' : 'reason')
    || readText(details, actionType === 'plan_authorization' ? 'reason' : 'summary');
  const fields: ApprovalRequestField[] = [];

  if (actionType === 'workspace.command.escalation') {
    const command = readText(details, 'command');
    if (command) {
      fields.push({ key: 'command', value: command, code: true });
    }
  }

  if (actionType === 'local_agent.execute') {
    const attachmentCount = details.attachment_count;
    if (typeof attachmentCount === 'number' && Number.isSafeInteger(attachmentCount) && attachmentCount >= 0) {
      fields.push({ key: 'attachments', value: attachmentCount, code: false });
    }
  }

  return {
    ...action,
    description,
    fields,
  };
}
