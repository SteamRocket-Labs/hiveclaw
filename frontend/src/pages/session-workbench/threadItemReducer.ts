import {
  LEGACY_THREAD_ITEM_TYPE_MAP,
  type ThreadItem,
  type ThreadItemStatus,
  type ThreadItemType,
} from '../../api/domains/threadItems.generated';
import type { AgentChatMessage, SessionPermissionRequest } from '../agent-detail/chatRuntime';

export const THREAD_ITEM_TYPES: readonly ThreadItemType[] = [
  'user_message',
  'agent_message',
  'reasoning',
  'tool_call',
  'tool_result',
  'approval_request',
  'approval_decision',
  'plan',
  'workflow_activity',
  'subagent_activity',
  'context_compaction',
  'artifact',
  'boundary',
  'error',
  'event',
] as const;

const THREAD_ITEM_TYPE_SET = new Set<string>(THREAD_ITEM_TYPES);
const THREAD_ITEM_STATUS_SET = new Set<string>([
  'running',
  'waiting_user',
  'succeeded',
  'failed',
  'cancelled',
]);
const RUNNING_STATUSES = new Set(['pending', 'queued', 'running', 'started', 'executing', 'in_progress']);
const WAITING_STATUSES = new Set(['awaiting_confirmation', 'awaiting_approval', 'session_permission_required', 'waiting_user']);
const FAILED_STATUSES = new Set(['failed', 'error', 'blocked', 'denied', 'capability_denied']);
const CANCELLED_STATUSES = new Set(['killed', 'cancelled', 'canceled']);
const EVENT_TITLES: Readonly<Record<string, string>> = {
  session_compact: 'Context Compacted',
  file_changes: 'File Changes',
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value)
    ? value as Record<string, unknown>
    : {};
}

function asRecords(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function text(value: unknown): string | null {
  if (value == null) return null;
  const normalized = String(value).trim();
  return normalized || null;
}

function finiteNumber(value: unknown): number | null {
  const normalized = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(normalized) ? normalized : null;
}

function boolean(value: unknown): boolean {
  return value === true;
}

function eventPart(parts: Array<Record<string, unknown>>): Record<string, unknown> {
  return parts.find((part) => part.type === 'event') || {};
}

function isCanonicalThreadItem(value: unknown): value is ThreadItem {
  const item = asRecord(value);
  return item.schema === 'hive.thread_item.v1'
    && item.schema_version === 1
    && typeof item.id === 'string'
    && typeof item.sequence === 'number'
    && THREAD_ITEM_TYPE_SET.has(String(item.item_type || ''))
    && THREAD_ITEM_STATUS_SET.has(String(item.item_status || ''))
    && typeof item.event_type === 'string'
    && typeof item.content === 'string'
    && isRecord(item.item_data);
}

function legacyItemType(eventType: string, role: string): ThreadItemType {
  const mapped = LEGACY_THREAD_ITEM_TYPE_MAP[eventType];
  if (mapped) return mapped;
  if (role === 'user') return 'user_message';
  if (role === 'assistant') return 'agent_message';
  return 'event';
}

function legacyItemStatus(itemType: ThreadItemType, eventType: string, data: Record<string, unknown>): ThreadItemStatus {
  const status = String(data.status || data.eventStatus || data.phase || '').trim().toLowerCase();
  if (itemType === 'approval_request' && (!status || RUNNING_STATUSES.has(status) || WAITING_STATUSES.has(status))) {
    return 'waiting_user';
  }
  if (itemType === 'error' || FAILED_STATUSES.has(status) || [
    'workflow_failed',
    'plan_failed',
    'runtime_action_failed',
    'tool_failure',
  ].includes(eventType)) {
    return 'failed';
  }
  if (CANCELLED_STATUSES.has(status) || eventType === 'run_cancelled') return 'cancelled';
  if (WAITING_STATUSES.has(status)) return 'waiting_user';
  if (RUNNING_STATUSES.has(status) || [
    'run_queued',
    'run_started',
    'thinking',
    'workflow_started',
    'subagent_task_started',
    'member_run_started',
  ].includes(eventType)) {
    return 'running';
  }
  return 'succeeded';
}

function legacyItemData(itemType: ThreadItemType, eventType: string, data: Record<string, unknown>): Record<string, unknown> {
  switch (itemType) {
    case 'user_message':
    case 'agent_message':
      return { sender_name: text(data.sender_name), file_name: text(data.file_name) };
    case 'reasoning':
      return { signature: text(data.thinking_signature || data.reasoning_signature) };
    case 'tool_call':
      return {
        tool_name: text(data.tool_name || data.name),
        tool_call_id: text(data.tool_call_id),
        arguments: asRecord(data.arguments || data.args),
        risk_class: text(data.risk_class),
      };
    case 'tool_result':
      return {
        event_type: eventType,
        tool_name: text(data.tool_name || data.name),
        tool_call_id: text(data.tool_call_id),
        success: !FAILED_STATUSES.has(String(data.status || '').toLowerCase()) && eventType !== 'tool_failure',
      };
    case 'approval_request':
      return {
        permission_request_id: text(data.permission_request_id || data.request_id) || 'unknown',
        tool_name: text(data.tool_name || data.tool_display_name),
        tool_display_name: text(data.tool_display_name),
        arguments: asRecord(data.arguments),
        capability: text(data.capability),
        security_zone: text(data.security_zone),
        permission_mode: text(data.permission_mode),
        decision_reason: text(data.decision_reason || data.reason),
        risk_class: text(data.risk_class),
        confirmation_kind: text(data.confirmation_kind),
        expires_at: text(data.expires_at),
        allow_session_allowed: boolean(data.allow_session_allowed),
        destructive: boolean(data.destructive),
      };
    case 'approval_decision':
      return {
        permission_request_id: text(data.permission_request_id || data.approval_id),
        action: text(data.action || data.decision),
        decision_reason: text(data.decision_reason || data.reason),
        approver_id: text(data.approver_id || data.actor_id),
      };
    case 'plan':
      return {
        plan_id: text(data.plan_id),
        plan_version: finiteNumber(data.plan_version),
        plan_hash: text(data.plan_hash),
        phase: text(data.phase || data.status),
      };
    case 'workflow_activity':
      return {
        workflow_run_id: text(data.workflow_run_id || data.run_id),
        workflow_step_id: text(data.workflow_step_id || data.step_id),
        runtime_task_id: text(data.runtime_task_id || data.task_id),
        label: text(data.label || data.title || data.name),
      };
    case 'subagent_activity':
      return {
        runtime_task_id: text(data.runtime_task_id || data.task_id),
        child_session_id: text(data.child_session_id),
        parent_session_id: text(data.parent_session_id),
        target_agent_name: text(data.target_agent_name || data.child_agent_name),
      };
    case 'context_compaction':
      return {
        original_message_count: finiteNumber(data.original_message_count),
        kept_message_count: finiteNumber(data.kept_message_count),
        continuity_sections_injected: Array.isArray(data.continuity_sections_injected)
          ? data.continuity_sections_injected.map(String)
          : [],
      };
    case 'artifact':
      return {
        artifact_id: text(data.artifact_id),
        path: text(data.path),
        revision_id: text(data.revision_id),
        action: text(data.action),
      };
    case 'boundary':
      return { phase: text(data.phase || data.status), reason: text(data.reason) };
    case 'error':
      return {
        code: text(data.code || data.error_code),
        reason: text(data.reason || data.error),
        retryable: boolean(data.retryable),
        retry_reason: text(data.retry_reason),
      };
    case 'event':
      return {
        event_type: eventType,
        title: text(data.title),
        runtime_task_id: text(data.runtime_task_id || data.task_id),
        reason: text(data.reason),
      };
    default:
      return assertNever(itemType);
  }
}

function assertNever(value: never): never {
  throw new Error(`Unhandled ThreadItem variant: ${String(value)}`);
}

export function normalizeThreadItemPayload(value: unknown): ThreadItem | null {
  if (isCanonicalThreadItem(value)) return value;
  const payload = asRecord(value);
  if (payload.schema === 'hive.thread_item.v1') return null;

  const eventType = text(payload.event_type || payload.eventType || payload.type);
  if (!eventType) return null;
  const parts = asRecords(payload.parts);
  const metadata = asRecord(payload.metadata);
  const nestedPermission = asRecord(payload.permission_request || eventPart(parts).permission_request);
  const data = { ...metadata, ...eventPart(parts), ...payload, ...nestedPermission };
  const role = text(payload.role) || (eventType === 'assistant_message' || eventType === 'thinking' ? 'assistant' : 'system');
  const itemType = legacyItemType(eventType, role);
  const sequence = finiteNumber(payload.sequence) || 0;
  const id = text(payload.id || payload.transcript_event_id)
    || `legacy:${text(payload.session_id) || 'session'}:${sequence}:${eventType}`;

  return {
    schema: 'hive.thread_item.v1',
    schema_version: 1,
    id,
    sequence,
    thread_id: text(payload.thread_id || payload.session_id),
    session_id: text(payload.session_id),
    run_id: text(payload.run_id || payload.runtime_task_id),
    message_id: text(payload.message_id),
    parent_event_id: text(payload.parent_event_id),
    root_session_id: text(payload.root_session_id),
    parent_session_id: text(payload.parent_session_id),
    turn_id: text(payload.turn_id),
    causation_id: text(payload.causation_id),
    correlation_id: text(payload.correlation_id || payload.run_id),
    item_type: itemType,
    item_status: legacyItemStatus(itemType, eventType, data),
    actor_type: text(payload.actor_type) || (role === 'assistant' ? 'assistant' : 'system'),
    event_type: eventType,
    type: eventType,
    role,
    visibility_scope: text(payload.visibility_scope) || 'direct_user',
    listed_surface: text(payload.listed_surface) || 'chat',
    content: text(payload.content || payload.message || payload.summary || eventPart(parts).text) || '',
    parts,
    metadata: data,
    created_at: text(payload.created_at || payload.timestamp),
    completed_at: text(payload.completed_at),
    evidence_refs: asRecords(payload.evidence_refs || data.evidence_refs),
    item_data: legacyItemData(itemType, eventType, data),
  } as ThreadItem;
}

export function reduceThreadItems(items: readonly ThreadItem[], payload: unknown): ThreadItem[] {
  const item = normalizeThreadItemPayload(payload);
  if (!item) return [...items];
  const existing = items.findIndex((candidate) => candidate.id === item.id);
  const next = existing >= 0
    ? items.map((candidate, index) => (index === existing ? item : candidate))
    : [...items, item];
  return [...next].sort((left, right) => left.sequence - right.sequence || left.id.localeCompare(right.id));
}

function approvalRequest(item: Extract<ThreadItem, { item_type: 'approval_request' }>): SessionPermissionRequest {
  const data = item.item_data;
  return {
    permission_request_id: data.permission_request_id,
    session_id: item.session_id,
    runtime_task_id: item.run_id,
    turn_id: item.turn_id,
    tool_call_id: null,
    tool_name: data.tool_name,
    tool_display_name: data.tool_display_name,
    arguments: data.arguments,
    capability: data.capability,
    permission_mode: data.permission_mode,
    decision_reason: data.decision_reason,
    risk_class: data.risk_class,
    confirmation_kind: data.confirmation_kind,
    allow_session_allowed: data.allow_session_allowed,
    destructive: data.destructive,
    created_at: item.created_at,
    expires_at: data.expires_at,
  };
}

export function threadItemToAgentChatMessage(item: ThreadItem): AgentChatMessage {
  const compatibility = { ...asRecord(item.metadata), ...eventPart(asRecords(item.parts)) };
  const toolGroups = compatibility.tool_groups ?? compatibility.packs;
  const activatedToolGroupCount = Array.isArray(toolGroups)
    ? toolGroups.filter((group) => typeof group === 'string' || text(asRecord(group).name)).length || undefined
    : undefined;
  const base: AgentChatMessage = {
    role: 'event',
    content: item.content,
    eventType: item.event_type,
    eventStatus: text(compatibility.eventStatus || compatibility.status) || item.item_status,
    timestamp: item.created_at || undefined,
    id: item.message_id || item.id,
    messageId: item.message_id || null,
    transcriptEventId: item.id,
    eventTurnId: item.turn_id || undefined,
    eventRootSessionId: item.root_session_id || undefined,
    eventParentSessionId: item.parent_session_id || undefined,
    eventTitle: text(compatibility.eventTitle || compatibility.title) || EVENT_TITLES[item.event_type],
    eventToolName: text(compatibility.tool_name) || undefined,
    eventApprovalId: text(compatibility.approval_id) || undefined,
    eventSecurityZone: text(compatibility.security_zone) || undefined,
    eventCapability: text(compatibility.capability) || undefined,
    eventApprovalRequired: compatibility.approval_required === true,
    eventReason: text(compatibility.reason) || undefined,
    eventNextStep: text(compatibility.next_step) || undefined,
    eventRetryable: compatibility.retryable === true,
    eventRetryReason: text(compatibility.retry_reason) || undefined,
    eventNotificationSource: text(compatibility.notification_source) || undefined,
    eventRuntimeTaskId: text(compatibility.runtime_task_id) || undefined,
    eventToolCallId: text(compatibility.tool_call_id) || undefined,
    eventHookEvent: text(compatibility.hook_event) || undefined,
    eventHookKey: text(compatibility.hook_key) || undefined,
    eventHookType: text(compatibility.hook_type) || undefined,
    eventChildSessionId: text(compatibility.child_session_id) || undefined,
    eventWorkflowRunId: text(compatibility.workflow_run_id) || undefined,
    eventWorkflowStepId: text(compatibility.workflow_step_id) || undefined,
    eventScheduleId: text(compatibility.schedule_id) || undefined,
    eventScheduleFireId: text(compatibility.schedule_fire_id) || undefined,
    eventGoalId: text(compatibility.goal_id) || undefined,
    eventOnceId: text(compatibility.once_id) || undefined,
    eventMemoryCandidateId: text(compatibility.memory_candidate_id) || undefined,
    eventArtifactId: text(compatibility.artifact_id) || undefined,
    eventPath: text(compatibility.path) || undefined,
    eventRevisionId: text(compatibility.revision_id) || undefined,
    eventAction: text(compatibility.action) || undefined,
    eventDiffSummary: text(compatibility.diff_summary) || undefined,
    activatedToolGroupCount,
    skillName: text(compatibility.skill_name) || undefined,
    triggerTool: text(compatibility.trigger_tool) || undefined,
    threadItem: item,
  };

  switch (item.item_type) {
    case 'user_message':
      return {
        ...base,
        role: 'user',
        sender_name: item.item_data.sender_name || undefined,
        fileName: item.item_data.file_name || undefined,
      };
    case 'agent_message':
      return {
        ...base,
        role: 'assistant',
        sender_name: item.item_data.sender_name || undefined,
        fileName: item.item_data.file_name || undefined,
      };
    case 'reasoning':
      return { ...base, thinking: item.content };
    case 'tool_call':
      return {
        ...base,
        role: 'tool_call',
        toolName: item.item_data.tool_name || undefined,
        toolArgs: item.item_data.arguments,
        toolStatus: item.item_status === 'running' ? 'running' : 'done',
        eventToolCallId: item.item_data.tool_call_id || undefined,
      };
    case 'tool_result':
      return {
        ...base,
        role: 'tool_call',
        toolName: item.item_data.tool_name || undefined,
        toolStatus: 'done',
        toolResult: item.content,
        eventToolCallId: item.item_data.tool_call_id || undefined,
      };
    case 'approval_request':
      if (item.item_data.permission_request_id === 'unknown') {
        return {
          ...base,
          eventToolName: item.item_data.tool_name || base.eventToolName,
          eventReason: item.item_data.decision_reason || base.eventReason,
        };
      }
      return {
        ...base,
        eventStatus: 'session_permission_required',
        eventToolName: item.item_data.tool_name || undefined,
        eventReason: item.item_data.decision_reason || item.item_data.risk_class || undefined,
        sessionPermissionRequest: approvalRequest(item),
      };
    case 'approval_decision':
      return {
        ...base,
        eventApprovalId: item.item_data.permission_request_id || undefined,
        eventAction: item.item_data.action || undefined,
        eventReason: item.item_data.decision_reason || undefined,
      };
    case 'plan':
      return { ...base, eventStatus: item.item_data.phase || item.item_status };
    case 'workflow_activity':
      return {
        ...base,
        eventRuntimeTaskId: item.item_data.runtime_task_id || undefined,
        eventWorkflowRunId: item.item_data.workflow_run_id || undefined,
        eventWorkflowStepId: item.item_data.workflow_step_id || undefined,
        eventTitle: item.item_data.label || base.eventTitle,
      };
    case 'subagent_activity':
      return {
        ...base,
        eventRuntimeTaskId: item.item_data.runtime_task_id || undefined,
        eventChildSessionId: item.item_data.child_session_id || undefined,
        eventParentSessionId: item.item_data.parent_session_id || base.eventParentSessionId,
        eventTitle: item.item_data.target_agent_name || base.eventTitle,
      };
    case 'context_compaction':
      return {
        ...base,
        originalMessageCount: item.item_data.original_message_count ?? undefined,
        keptMessageCount: item.item_data.kept_message_count ?? undefined,
        continuitySectionsInjected: item.item_data.continuity_sections_injected,
      };
    case 'artifact':
      return {
        ...base,
        eventArtifactId: item.item_data.artifact_id || undefined,
        eventPath: item.item_data.path || undefined,
        eventRevisionId: item.item_data.revision_id || undefined,
        eventAction: item.item_data.action || undefined,
      };
    case 'boundary':
      return { ...base, eventStatus: item.item_data.phase || item.item_status, eventReason: item.item_data.reason || undefined };
    case 'error':
      return {
        ...base,
        eventStatus: 'failed',
        eventReason: item.item_data.reason || undefined,
        eventRetryable: item.item_data.retryable,
        eventRetryReason: item.item_data.retry_reason || undefined,
      };
    case 'event':
      return {
        ...base,
        eventTitle: item.item_data.title || base.eventTitle,
        eventRuntimeTaskId: item.item_data.runtime_task_id || undefined,
        eventReason: item.item_data.reason || undefined,
      };
    default:
      return assertNever(item);
  }
}
