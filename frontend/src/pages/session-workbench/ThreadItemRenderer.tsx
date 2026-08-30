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

function detailsFor(item: ThreadItem, t: (key: string, fallback: string) => string): Detail[] {
  const userAction = item.user_action;
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
    case 'approval_request':
      return [
        { label: 'tool', value: item.item_data.tool_display_name || item.item_data.tool_name },
        { label: 'expires', value: item.item_data.expires_at },
        { label: 'impact', value: item.item_data.destructive ? t('sessionWorkbench.threadItem.value.destructive', 'destructive') : t('sessionWorkbench.threadItem.value.reversible', 'reversible or read-only') },
      ];
    case 'approval_decision':
      return [{ label: 'action', value: item.item_data.action }];
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
      return [
        { label: 'retry', value: item.item_data.retryable ? t('sessionWorkbench.threadItem.value.retryable', 'retryable') : t('sessionWorkbench.threadItem.value.notRetryable', 'not retryable') },
      ];
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
  ].includes(item.item_type);
}

function translateRuntimeFailure(t: TFunction, presentation: RuntimeFailurePresentation): string {
  switch (presentation.kind) {
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
  actions?: React.ReactNode;
}

const USER_CONVERSATION_ITEM_TYPES = new Set<ThreadItemType>(['approval_request', 'warning', 'error', 'plan']);
const NON_BLOCKING_STATUS_EVENTS = new Set(['memory_context_degraded']);

export function shouldRenderThreadItemInConversation(item: ThreadItem, operatorView: boolean): boolean {
  if (operatorView && item.audience === 'operator') return true;
  if (item.audience === 'operator') return false;
  if (NON_BLOCKING_STATUS_EVENTS.has(item.event_type)) return false;
  if (USER_CONVERSATION_ITEM_TYPES.has(item.item_type)) return true;
  return Boolean(item.user_action && item.user_action.kind !== 'open_artifact');
}

export function ThreadItemRenderer({
  item,
  selected = false,
  onSelect,
  approvalActions,
  actions,
}: ThreadItemRendererProps) {
  const { t } = useTranslation();
  const details = detailsFor(item, t).filter((detail) => detail.value !== null && detail.value !== undefined && detail.value !== '');
  const title = t(`sessionWorkbench.threadItem.type.${item.item_type}`, TITLES[item.item_type]);
  const status = t(`sessionWorkbench.threadItem.status.${item.item_status}`, item.item_status);
  const runtimeFailure = item.item_type === 'error' && item.event_type === 'runtime_failure'
    ? runtimeFailurePresentation(item.item_data.code, item.item_data.retryable === true)
    : null;
  const visibleContent = runtimeFailure
    ? translateRuntimeFailure(t, runtimeFailure)
    : item.user_summary || (item.item_type === 'approval_request'
      ? t(
          'sessionWorkbench.threadItem.permissionNeeded',
          'The agent needs permission to use {{tool}}.',
          { tool: item.item_data.tool_display_name || item.item_data.tool_name || t('sessionWorkbench.threadItem.thisTool', 'this tool') },
        )
      : item.content);
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
      {detailBody && (shouldCollapse(item) ? (
        <details className="thread-item-disclosure">
          <summary>
            <IconChevronRight size={13} aria-hidden="true" />
            {t('sessionWorkbench.threadItem.details', 'Details')}
          </summary>
          {detailBody}
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
