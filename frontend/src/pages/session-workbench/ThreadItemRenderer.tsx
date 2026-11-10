import React from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import {
  IconAlertTriangle,
  IconBan,
  IconCheck,
  IconBraces,
  IconChevronRight,
  IconClock,
  IconLoader2,
} from '@tabler/icons-react';

import type { ThreadItem, ThreadItemStatus, ThreadItemType } from '../../api/domains/threadItems.generated';
import { runtimeFailurePresentation, type RuntimeFailurePresentation } from './runtimeFailurePresentation';
import { approvalActionLabel } from './approvalActionPresentation';
import './ThreadItemWorkbench.css';

type Detail = { label: string; value: React.ReactNode };

const TITLES: Record<ThreadItemType, string> = {
  user_message: 'User message',
  agent_message: 'Agent message',
  reasoning: 'Reasoning',
  tool_call: 'Tool call',
  tool_result: 'Tool result',
  approval_request: 'Approval required',
  approval_decision: 'Approval decision',
  plan: 'Plan',
  workflow_activity: 'Workflow',
  subagent_activity: 'Sub-agent',
  agent_team_activity: 'Agent Team',
  peer_a2a_activity: 'A2A digital employee',
  context_compaction: 'Context compaction',
  artifact: 'Artifact',
  boundary: 'Run boundary',
  warning: 'Runtime warning',
  error: 'Runtime error',
  event: 'Runtime event',
};

function assertNever(value: never): never {
  throw new Error(`Unhandled ThreadItem renderer variant: ${String(value)}`);
}

function statusIcon(status: ThreadItemStatus, itemType: ThreadItemType): React.ReactNode {
  if (status === 'running') return <IconLoader2 className="thread-item-spinner" size={14} aria-hidden="true" />;
  if (status === 'waiting_user') return <IconClock size={14} aria-hidden="true" />;
  if (status === 'failed' || itemType === 'warning') return <IconAlertTriangle size={14} aria-hidden="true" />;
  if (status === 'cancelled') return <IconBan size={14} aria-hidden="true" />;
  return <IconCheck size={14} aria-hidden="true" />;
}

function detailsFor(item: ThreadItem, t: (key: string, fallback: string) => string, language: string, approvalArguments: unknown): Detail[] {
  const userAction = item.user_action;
  if (item.item_type === 'approval_request') {
    const data = item.item_data;
    const expires = userAction?.expires_at || data.expires_at;
    const args = approvalArguments && typeof approvalArguments === 'object' && !Array.isArray(approvalArguments)
      ? approvalArguments as Record<string, unknown> : null;
    const summary = args ? data.tool_name === 'run_command' ? { command: args.command, cwd: 'workspace/' }
      : data.tool_name === 'create_employee_from_request' ? { name: args.name, first_task: args.first_task }
        : ['send_email', 'reply_email'].includes(data.tool_name || '') ? { recipient: args.to || args.recipient, subject: args.subject, body: args.body }
          : {} : {};
    const proposalDetails = Object.entries(summary).flatMap(([label, value]) => {
      const text = Array.isArray(value) ? value.join(', ') : value;
      return typeof text === 'string' && text ? [{ label, value: text.length > 240 ? `${text.slice(0, 240)}…` : text }] : [];
    });
    return [
      { label: 'tool', value: approvalActionLabel(data.tool_name, t) },
      { label: 'expires', value: expires ? new Date(expires).toLocaleString(language) : null },
      { label: 'impact', value: data.tool_name === 'create_employee_from_request'
        ? t('sessionWorkbench.threadItem.value.employeePermanent', 'The employee can be deactivated, but cannot be deleted.')
        : userAction?.impact || (data.destructive ? t('sessionWorkbench.threadItem.value.destructive', 'Irreversible action') : t('sessionWorkbench.threadItem.value.reversible', 'Reversible or read-only')) },
      ...proposalDetails,
      ...(userAction?.details || []).flatMap((detail) => typeof detail.label === 'string' && typeof detail.value === 'string'
        && !proposalDetails.some(proposal => proposal.label === detail.label)
        ? [{ label: detail.label, value: detail.value }] : []),
    ];
  }
  if (userAction) {
    const safeActionDetails = (userAction.details || []).flatMap((detail) => {
      const label = typeof detail.label === 'string' ? detail.label : '';
      const value = typeof detail.value === 'string' ? detail.value : '';
      return label && value ? [{ label, value }] : [];
    });
    return [
      { label: 'impact', value: userAction.impact },
      { label: 'expires', value: userAction.expires_at },
      ...safeActionDetails,
    ];
  }
  switch (item.item_type) {
    case 'user_message':
    case 'agent_message':
      return [
        { label: 'sender', value: item.item_data.sender_name },
        { label: 'file', value: item.item_data.file_name },
      ];
    case 'reasoning':
      return [];
    case 'tool_call':
      return [{ label: 'tool', value: item.item_data.tool_name }];
    case 'tool_result':
      return [
        { label: 'tool', value: item.item_data.tool_name },
        { label: 'result', value: item.item_data.success ? t('sessionWorkbench.threadItem.value.success', 'success') : t('sessionWorkbench.threadItem.value.failure', 'failure') },
      ];
    case 'approval_decision':
      return [{ label: 'action', value: item.item_data.action === 'allow_once' ? t('agent.chat.permission.allowOnce', 'Allow once')
        : item.item_data.action === 'allow_session' ? t('agent.chat.permission.allowSession', 'Allow for this session')
          : item.item_data.action === 'deny' ? t('agent.chat.permission.deny', 'Deny') : item.item_data.action }];
    case 'plan':
      return [{ label: 'phase', value: item.item_data.phase }];
    case 'workflow_activity':
      return [{ label: 'label', value: item.item_data.label }];
    case 'subagent_activity':
      return [{ label: 'sub-agent', value: item.item_data.target_agent_name }];
    case 'agent_team_activity':
      return [{ label: 'team member', value: item.item_data.member_name || item.item_data.target_agent_name }];
    case 'peer_a2a_activity':
      return [
        { label: 'digital employee', value: item.item_data.target_agent_name },
        { label: 'session', value: item.item_data.read_only ? 'read-only' : null },
      ];
    case 'context_compaction':
      return [];
    case 'artifact':
      return [
        { label: 'path', value: item.item_data.path },
        { label: 'action', value: item.item_data.action },
      ];
    case 'boundary':
      return [
        { label: 'phase', value: item.item_data.phase },
        { label: 'reason', value: item.item_data.reason },
      ];
    case 'warning':
    case 'error':
      // Retryability is not a member-facing label pair: the retry action
      // itself (when retry is possible) is composed by the chat surface (B2 r2).
      return [];
    case 'event':
      return [];
    default:
      return assertNever(item);
  }
}

function shouldCollapse(item: ThreadItem): boolean {
  return [
    'tool_call',
    'tool_result',
    'workflow_activity',
    'subagent_activity',
    'agent_team_activity',
    'peer_a2a_activity',
    'reasoning',
    // The approval card speaks in user words on the surface; the exact tool
    // name, expiry and impact stay one disclosure away for operators (B2).
    'approval_request',
  ].includes(item.item_type);
}

function translateRuntimeFailure(t: TFunction, presentation: RuntimeFailurePresentation): string {
  switch (presentation.kind) {
    case 'delivery_unknown':
      return t('sessionWorkbench.threadItem.failure.deliveryUnknown', presentation.fallback);
    case 'auth_or_permission':
      return t('sessionWorkbench.threadItem.failure.authOrPermission', presentation.fallback);
    case 'model_missing':
      return t('sessionWorkbench.threadItem.failure.modelMissing', presentation.fallback);
    case 'quota_exhausted':
      return t('sessionWorkbench.threadItem.failure.quotaExhausted', presentation.fallback);
    case 'rate_limited':
      return t('sessionWorkbench.threadItem.failure.rateLimited', presentation.fallback);
    case 'retryable':
      return t('sessionWorkbench.threadItem.failure.retryable', presentation.fallback);
    case 'unavailable':
      return t('sessionWorkbench.threadItem.failure.unavailable', presentation.fallback);
    default:
      throw new Error(`Unhandled runtime failure presentation: ${String(presentation.kind)}`);
  }
}

export interface ThreadItemRendererProps {
  item: ThreadItem;
  selected?: boolean;
  onSelect?: (item: ThreadItem) => void;
  approvalActions?: React.ReactNode;
  /** The full permission-request arguments. The approver must be able to
   * read the whole proposal (recipients, subject, body) before deciding:
   * they render inside the card's single 详情 disclosure (B2 r3), with the
   * same requester access rule the resolve actions use. */
  approvalArguments?: unknown;
  actions?: React.ReactNode;
}

/**
 * One Chinese (locale) sentence naming the action in the member's words,
 * quoting the target (for example a digital employee's name) when the item's
 * safe argument details carry it. Raw tool ids and raw JSON never surface.
 */
function approvalSentence(item: ThreadItem, t: TFunction): string {
  const data = item.item_data as { tool_display_name?: string | null; tool_name?: string | null };
  const action = approvalActionLabel(data.tool_display_name || data.tool_name, t);
  const target = (item.user_action?.details || []).find(
    (detail) => typeof detail?.value === 'string'
      && ['name', 'title', 'recipient', 'target'].includes(String(detail?.label || '')),
  );
  return target
    ? t('sessionWorkbench.threadItem.permissionNeededWithTarget', 'This needs your approval: {{tool}} “{{name}}”.', { tool: action, name: target.value })
    : t('sessionWorkbench.threadItem.permissionNeeded', 'This needs your approval: {{tool}}.', { tool: action });
}

const USER_CONVERSATION_ITEM_TYPES = new Set<ThreadItemType>(['approval_request', 'warning', 'error', 'plan']);
const NON_BLOCKING_STATUS_EVENTS = new Set(['memory_context_degraded']);

export function shouldRenderThreadItemInConversation(item: ThreadItem, operatorView: boolean): boolean {
  if (!operatorView && item.item_type === 'approval_request'
    && (item.metadata?.approval_id || (item.event_type === 'permission_request'
      && !item.metadata?.permission_request_id && !item.user_action?.token))) return false;
  if (!operatorView && item.metadata?.runtime_action_from_result_integration === true
    && item.metadata?.notification_source === 'approval') return false;
  if (operatorView && item.audience === 'operator') return true;
  if (item.audience === 'operator') return false;
  if (NON_BLOCKING_STATUS_EVENTS.has(item.event_type)) return false;
  // The platform does not interpose source-access cards in the conversation
  // (product definition P3): when the model truly cannot answer within its
  // authorized evidence, it says so itself. Operators still see the raw item.
  if (item.event_type === 'source_access_notice') return false;
  // Run-phase boundary changes are internal state, not timeline cards: the
  // approval card and the run status already own what the member sees (B2 r2).
  if (item.item_type === 'boundary') return false;
  if (item.item_type === 'approval_decision' && item.event_type === 'permission_resolved') return true;
  if (item.item_type === 'context_compaction') return Boolean(item.user_summary);
  if (USER_CONVERSATION_ITEM_TYPES.has(item.item_type)) return true;
  return Boolean(item.user_action && item.user_action.kind !== 'open_artifact');
}

export function ThreadItemRenderer({
  item,
  selected = false,
  onSelect,
  approvalActions,
  approvalArguments,
  actions,
}: ThreadItemRendererProps) {
  const { t, i18n } = useTranslation();
  const [showArguments, setShowArguments] = React.useState(false);
  const sourceNotice = item.event_type === 'source_access_notice';
  const details = sourceNotice ? [] : detailsFor(item, t, i18n.language, approvalArguments).filter((detail) => detail.value !== null && detail.value !== undefined && detail.value !== '');
  const title = sourceNotice ? t('agent.chat.sourceAccess', 'Source access')
    : t(`sessionWorkbench.threadItem.type.${item.item_type}`, TITLES[item.item_type]);
  const status = sourceNotice ? t('agent.chat.sourceUnavailable', 'Unavailable')
    : t(`sessionWorkbench.threadItem.status.${item.item_status}`, item.item_status);
  const runtimeFailure = item.item_type === 'error' && item.event_type === 'runtime_failure'
    ? runtimeFailurePresentation(item.item_data.code, item.item_data.retryable === true, item.metadata?.delivery_state)
    : null;
  const visibleContent = sourceNotice
    ? t('agent.chat.groupSourcesUnavailable', 'Some source content is unavailable to you. Request access or refresh this conversation.')
    : runtimeFailure
    ? translateRuntimeFailure(t, runtimeFailure)
    : item.metadata?.runtime_action_from_result_integration === true && item.event_type === 'runtime_action_blocked'
    ? item.metadata?.notification_source === 'approval' && item.metadata?.status === 'blocked'
      ? t('approvalExecution.declined', 'Approval declined')
      : t('approvalExecution.needs_reconciliation', 'Needs admin attention')
    : item.metadata?.runtime_action_from_result_integration === true && item.event_type === 'runtime_action_failed'
    ? t('approvalExecution.failed', 'Failed')
    : item.item_type === 'approval_request'
    ? approvalSentence(item, t)
    : item.user_summary || item.content;
  const detailBody = details.length > 0 && (
    <dl className="thread-item-details">
      {details.map((detail) => (
        <React.Fragment key={detail.label}>
          <dt>{t(`sessionWorkbench.threadItem.field.${detail.label}`, detail.label)}</dt>
          <dd>{detail.value}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
  const argumentsBody = item.item_type === 'approval_request' && approvalArguments != null && (
    <>
    <button type="button" className="btn btn-secondary" aria-expanded={showArguments} onClick={() => setShowArguments(!showArguments)}>
      <IconChevronRight size={13} aria-hidden="true" style={{ transform: showArguments ? 'rotate(90deg)' : undefined }} />
      {t('agent.chat.permission.fullArguments', 'Full parameters')}
    </button>
    {showArguments && <pre
      className="thread-item-arguments"
      aria-label={t('agent.chat.permission.actionDetails', 'Action details')}
      style={{ maxHeight: '240px', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
    >{JSON.stringify(approvalArguments, null, 2)}</pre>}
    </>
  );

  if (item.item_type === 'context_compaction' && item.audience !== 'operator') {
    return item.user_summary ? <p>{item.user_summary}</p> : null;
  }

  if (item.item_type === 'approval_decision' && item.event_type === 'permission_resolved') {
    // A settled permission decision collapses to one plain line (product
    // definition P3) instead of a full decision card.
    const action = item.item_data.action;
    return <p>
      <IconCheck size={14} aria-hidden="true" /> {action === 'allow_once'
        ? t('sessionWorkbench.threadItem.permissionDecision.allowOnce', 'Allowed · this time')
        : action === 'allow_session'
          ? t('sessionWorkbench.threadItem.permissionDecision.allowSession', 'Allowed · for this session')
          : t('sessionWorkbench.threadItem.permissionDecision.deny', 'Declined')}
    </p>;
  }

  return (
    <article
      className={`thread-item-card thread-item-${item.item_type}${selected ? ' is-selected' : ''}`}
      data-thread-item-type={item.item_type}
      data-thread-item-status={item.item_status}
      data-selected={selected || undefined}
      aria-label={`${title}: ${status}`}
      aria-current={selected ? 'true' : undefined}
    >
      <header className="thread-item-header">
        <span className="thread-item-status-icon" data-status={item.item_status}>{statusIcon(item.item_status, item.item_type)}</span>
        <strong>{title}</strong>
        <span className="thread-item-status-text">{status}</span>
        {item.audience === 'operator' ? <span className="thread-item-sequence">#{item.sequence}</span> : null}
        {onSelect && item.audience === 'operator' && item.operator_details ? (
          <button
            type="button"
            data-testid="thread-item-technical-details"
            className="thread-item-technical-details"
            aria-label={t('sessionWorkbench.threadItem.inspectTechnicalDetails', 'Inspect technical details')}
            aria-pressed={selected}
            onClick={() => onSelect(item)}
          >
            <IconBraces size={13} aria-hidden="true" />
          </button>
        ) : null}
      </header>
      {visibleContent && <div className="thread-item-content">{visibleContent}</div>}
      {(detailBody || argumentsBody) && (shouldCollapse(item) ? (
        <details className="thread-item-disclosure">
          <summary>
            <IconChevronRight size={13} aria-hidden="true" />
            {t('sessionWorkbench.threadItem.details', 'Details')}
          </summary>
          {detailBody}
          {argumentsBody}
        </details>
      ) : detailBody)}
      {item.item_type === 'approval_request' && approvalActions && (
        <div className="thread-item-actions" aria-label={t('sessionWorkbench.threadItem.approvalActions', 'Approval actions')}>
          {approvalActions}
        </div>
      )}
      {actions && <div className="thread-item-actions">{actions}</div>}
    </article>
  );
}
