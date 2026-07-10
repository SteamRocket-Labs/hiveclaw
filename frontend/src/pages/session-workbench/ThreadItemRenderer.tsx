import React from 'react';
import { useTranslation } from 'react-i18next';
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
  context_compaction: 'Context compaction',
  artifact: 'Artifact',
  boundary: 'Run boundary',
  error: 'Runtime error',
  event: 'Runtime event',
};

function assertNever(value: never): never {
  throw new Error(`Unhandled ThreadItem renderer variant: ${String(value)}`);
}

function json(value: unknown): React.ReactNode {
  if (!value || (typeof value === 'object' && Object.keys(value as object).length === 0)) return null;
  return <pre className="thread-item-json">{JSON.stringify(value, null, 2)}</pre>;
}

function statusIcon(status: ThreadItemStatus): React.ReactNode {
  if (status === 'running') return <IconLoader2 className="thread-item-spinner" size={14} aria-hidden="true" />;
  if (status === 'waiting_user') return <IconClock size={14} aria-hidden="true" />;
  if (status === 'failed') return <IconAlertTriangle size={14} aria-hidden="true" />;
  if (status === 'cancelled') return <IconBan size={14} aria-hidden="true" />;
  return <IconCheck size={14} aria-hidden="true" />;
}

function detailsFor(item: ThreadItem, t: (key: string, fallback: string) => string): Detail[] {
  switch (item.item_type) {
    case 'user_message':
    case 'agent_message':
      return [
        { label: 'sender', value: item.item_data.sender_name },
        { label: 'file', value: item.item_data.file_name },
      ];
    case 'reasoning':
      return [{ label: 'signature', value: item.item_data.signature }];
    case 'tool_call':
      return [
        { label: 'tool', value: item.item_data.tool_name },
        { label: 'call', value: item.item_data.tool_call_id },
        { label: 'risk', value: item.item_data.risk_class },
        { label: 'arguments', value: json(item.item_data.arguments) },
      ];
    case 'tool_result':
      return [
        { label: 'tool', value: item.item_data.tool_name },
        { label: 'call', value: item.item_data.tool_call_id },
        { label: 'result', value: item.item_data.success ? t('sessionWorkbench.threadItem.value.success', 'success') : t('sessionWorkbench.threadItem.value.failure', 'failure') },
      ];
    case 'approval_request':
      return [
        { label: 'request', value: item.item_data.permission_request_id },
        { label: 'tool', value: item.item_data.tool_name },
        { label: 'permission', value: item.item_data.permission_mode },
        { label: 'risk', value: item.item_data.risk_class },
        { label: 'expires', value: item.item_data.expires_at },
        { label: 'scope', value: item.item_data.allow_session_allowed ? t('sessionWorkbench.threadItem.value.sessionAllowed', 'session allowed') : t('sessionWorkbench.threadItem.value.oneAction', 'one action') },
        { label: 'impact', value: item.item_data.destructive ? t('sessionWorkbench.threadItem.value.destructive', 'destructive') : t('sessionWorkbench.threadItem.value.reversible', 'reversible or read-only') },
        { label: 'arguments', value: json(item.item_data.arguments) },
      ];
    case 'approval_decision':
      return [
        { label: 'request', value: item.item_data.permission_request_id },
        { label: 'action', value: item.item_data.action },
        { label: 'reason', value: item.item_data.decision_reason },
        { label: 'approver', value: item.item_data.approver_id },
      ];
    case 'plan':
      return [
        { label: 'plan', value: item.item_data.plan_id },
        { label: 'version', value: item.item_data.plan_version },
        { label: 'hash', value: item.item_data.plan_hash },
        { label: 'phase', value: item.item_data.phase },
      ];
    case 'workflow_activity':
      return [
        { label: 'workflow', value: item.item_data.workflow_run_id },
        { label: 'step', value: item.item_data.workflow_step_id },
        { label: 'task', value: item.item_data.runtime_task_id },
        { label: 'label', value: item.item_data.label },
      ];
    case 'subagent_activity':
      return [
        { label: 'agent', value: item.item_data.target_agent_name },
        { label: 'task', value: item.item_data.runtime_task_id },
        { label: 'child session', value: item.item_data.child_session_id },
        { label: 'parent session', value: item.item_data.parent_session_id },
      ];
    case 'context_compaction':
      return [
        { label: 'original messages', value: item.item_data.original_message_count },
        { label: 'kept messages', value: item.item_data.kept_message_count },
        { label: 'continuity', value: item.item_data.continuity_sections_injected?.join(', ') },
      ];
    case 'artifact':
      return [
        { label: 'artifact', value: item.item_data.artifact_id },
        { label: 'path', value: item.item_data.path },
        { label: 'revision', value: item.item_data.revision_id },
        { label: 'action', value: item.item_data.action },
      ];
    case 'boundary':
      return [
        { label: 'phase', value: item.item_data.phase },
        { label: 'reason', value: item.item_data.reason },
      ];
    case 'error':
      return [
        { label: 'code', value: item.item_data.code },
        { label: 'reason', value: item.item_data.reason },
        { label: 'retry', value: item.item_data.retryable ? t('sessionWorkbench.threadItem.value.retryable', 'retryable') : t('sessionWorkbench.threadItem.value.notRetryable', 'not retryable') },
        { label: 'retry reason', value: item.item_data.retry_reason },
      ];
    case 'event':
      return [
        { label: 'event', value: item.item_data.event_type },
        { label: 'task', value: item.item_data.runtime_task_id },
        { label: 'reason', value: item.item_data.reason },
      ];
    default:
      return assertNever(item);
  }
}

function shouldCollapse(item: ThreadItem): boolean {
  return ['tool_call', 'tool_result', 'workflow_activity', 'subagent_activity', 'reasoning'].includes(item.item_type);
}

export interface ThreadItemRendererProps {
  item: ThreadItem;
  selected?: boolean;
  onSelect?: (item: ThreadItem) => void;
  approvalActions?: React.ReactNode;
  actions?: React.ReactNode;
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
  const visibleContent = item.item_type === 'approval_request'
    ? t(
        'sessionWorkbench.threadItem.permissionNeeded',
        'The agent needs permission to use {{tool}}.',
        { tool: item.item_data.tool_display_name || item.item_data.tool_name || t('sessionWorkbench.threadItem.thisTool', 'this tool') },
      )
    : item.content;
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
        <span className="thread-item-status-icon" data-status={item.item_status}>{statusIcon(item.item_status)}</span>
        <strong>{title}</strong>
        <span className="thread-item-status-text">{status}</span>
        <span className="thread-item-sequence">#{item.sequence}</span>
        {onSelect ? (
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
