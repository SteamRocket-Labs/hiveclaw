import type { SessionRuntimeSummary } from '../../api/domains/chat';
import { normalizeToolCallResult, type ToolCallMeta } from './toolResultEnvelope';

export const MIN_COMPOSER_HEIGHT = 44;
export const MAX_COMPOSER_HEIGHT = 160;
export const CHAT_SOCKET_KEEPALIVE_INTERVAL_MS = 30_000;

export type RuntimeEventType =
  | 'permission'
  | 'permission_request'
  | 'permission_resolved'
  | 'session_compact'
  | 'tool_group_activation'
  | 'deferred_tools_delta'
  | 'pack_activation'
  | 'team_memory'
  | 'hook_progress'
  | 'hook_summary'
  | 'hook_attachment'
  | 'hook_blocked'
  | 'workflow_run'
  | 'workflow_step'
  | 'dynamic_workflow'
  | 'child_session'
  | 'subagent'
  | 'team_member'
  | 'schedule'
  | 'schedule_fire'
  | 'goal'
  | 'once'
  | 'memory_candidate'
  | 'artifact_update'
  | 'artifact_delivery';

export interface AgentChatMessage {
  role: 'user' | 'assistant' | 'tool_call' | 'event';
  content: string;
  fileName?: string;
  imageUrl?: string;
  thinking?: string;
  sender_name?: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolStatus?: 'running' | 'done';
  toolResult?: string;
  toolRawResult?: string;
  toolMeta?: ToolCallMeta | null;
  timestamp?: string;
  participant_id?: string | null;
  id?: string;
  eventType?: RuntimeEventType;
  eventTitle?: string;
  eventStatus?: string;
  eventToolName?: string;
  eventApprovalId?: string;
  eventSecurityZone?: string;
  eventCapability?: string;
  eventApprovalRequired?: boolean;
  eventReason?: string;
  eventNextStep?: string;
  eventRetryable?: boolean;
  eventRetryReason?: string;
  sessionPermissionRequest?: SessionPermissionRequest;
  eventRuntimeTaskId?: string;
  eventTurnId?: string;
  eventToolCallId?: string;
  eventHookEvent?: string;
  eventHookKey?: string;
  eventHookType?: string;
  eventChildSessionId?: string;
  eventParentSessionId?: string;
  eventRootSessionId?: string;
  eventWorkflowRunId?: string;
  eventWorkflowStepId?: string;
  eventScheduleId?: string;
  eventScheduleFireId?: string;
  eventGoalId?: string;
  eventOnceId?: string;
  eventMemoryCandidateId?: string;
  eventArtifactId?: string;
  eventPath?: string;
  eventRevisionId?: string;
  eventAction?: string;
  eventDiffSummary?: string;
  originalMessageCount?: number;
  keptMessageCount?: number;
  continuitySectionsInjected?: string[];
  // Count of runtime tool groups activated in this event. Names are internal and
  // intentionally not surfaced to users (§8.4) — only the fact/scale of activation.
  activatedToolGroupCount?: number;
  skillName?: string;
  triggerTool?: string;
  artifacts?: ChatArtifactPart[];
}

export interface SessionPermissionRequest {
  permission_request_id: string;
  session_id?: string | null;
  runtime_task_id?: string | null;
  turn_id?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  tool_display_name?: string | null;
  arguments?: Record<string, unknown>;
  capability?: string | null;
  permission_mode?: string | null;
  decision_reason?: string | null;
  risk_class?: string | null;
  confirmation_kind?: string | null;
  allow_session_allowed?: boolean | null;
  destructive?: boolean | null;
  created_at?: string | null;
  expires_at?: string | null;
}

export interface ChatArtifactPart {
  id?: string;
  name: string;
  path: string;
  previewKind?: string;
  mimeType?: string;
  size?: number;
  source?: string;
  runtimeTaskId?: string;
  revisionId?: string;
  action?: string;
  toolCallId?: string;
  diffSummary?: string;
  previewSnapshotContent?: string;
  previewSnapshotTruncated?: boolean;
}

function mergeClarificationAnswerMetadata(
  toolMeta: ToolCallMeta | null,
  metadata: Record<string, unknown> | undefined,
): ToolCallMeta | null {
  if (!toolMeta || toolMeta.kind !== 'user_clarification' || !metadata) return toolMeta;
  if (metadata.answered !== true && !metadata.answered_by_event_id) return toolMeta;
  return {
    ...toolMeta,
    answered: metadata.answered === true || Boolean(metadata.answered_by_event_id),
    answeredByEventId: typeof metadata.answered_by_event_id === 'string' ? metadata.answered_by_event_id : null,
    answerText: typeof metadata.answer_text === 'string' ? metadata.answer_text : null,
    answeredAt: typeof metadata.answered_at === 'string' ? metadata.answered_at : null,
  };
}

export type ChatRuntimeSummary = SessionRuntimeSummary;

export type StreamingChunkEvent = {
  type: 'chunk';
  content?: string;
  reset?: boolean;
};

export type ChatTranscriptEventPayload = {
  id?: string;
  sequence?: number;
  run_id?: string | null;
  type?: string;
  event_type?: string;
  actor_type?: string;
  role?: AgentChatMessage['role'] | string;
  content?: string;
  permission_request_id?: string;
  status?: string;
  parts?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  created_at?: string;
  timestamp?: string;
  message_id?: string | null;
};

export interface SessionRunState {
  runId: string;
  status: string;
}

export interface SessionUiState {
  isWaiting: boolean;
  isStreaming: boolean;
}

export type AgentOwnedSession = {
  id?: unknown;
  agent_id?: unknown;
  agentId?: unknown;
};

export interface TranscriptReplayState {
  messages: AgentChatMessage[];
  ui: SessionUiState;
  seenEventIds: Set<string>;
  pendingSessionPermissions: AgentChatMessage[];
}

export interface PendingUserMessage {
  message: AgentChatMessage;
  anchorMessageCount: number;
}

export const ACTIVE_RUN_ABSENCE_GRACE_MS = 8_000;

export function sessionBelongsToAgent(session: AgentOwnedSession | null | undefined, agentId: string | null | undefined): boolean {
  if (!session || !agentId) return false;
  const sessionAgentId = session.agent_id ?? session.agentId;
  return sessionAgentId == null || String(sessionAgentId) === String(agentId);
}

export function filterSessionsForAgent<T extends AgentOwnedSession>(sessions: T[], agentId: string | null | undefined): T[] {
  if (!agentId) return [];
  return sessions.filter((session) => sessionBelongsToAgent(session, agentId));
}

export function applySessionActiveRunState(
  activeRuns: Record<string, SessionRunState>,
  uiStates: Record<string, SessionUiState>,
  key: string,
  run: SessionRunState | null,
): { activeRuns: Record<string, SessionRunState>; uiStates: Record<string, SessionUiState> } {
  if (run) {
    return {
      activeRuns: { ...activeRuns, [key]: run },
      uiStates: { ...uiStates, [key]: { isWaiting: true, isStreaming: false } },
    };
  }
  const nextActiveRuns = { ...activeRuns };
  const nextUiStates = { ...uiStates };
  delete nextActiveRuns[key];
  delete nextUiStates[key];
  return { activeRuns: nextActiveRuns, uiStates: nextUiStates };
}

export function applySessionActiveRunObservedState(
  activeRuns: Record<string, SessionRunState>,
  uiStates: Record<string, SessionUiState>,
  key: string,
  run: SessionRunState,
): { activeRuns: Record<string, SessionRunState>; uiStates: Record<string, SessionUiState> } {
  const currentUi = uiStates[key];
  return {
    activeRuns: { ...activeRuns, [key]: run },
    uiStates: {
      ...uiStates,
      [key]: currentUi?.isStreaming
        ? currentUi
        : { isWaiting: true, isStreaming: false },
    },
  };
}

export function shouldClearStaleRuntimeState({
  hasStaleRuntimeState,
  lastRuntimeActivityAt,
  now,
  graceMs = ACTIVE_RUN_ABSENCE_GRACE_MS,
}: {
  hasStaleRuntimeState: boolean;
  lastRuntimeActivityAt?: number | null;
  now: number;
  graceMs?: number;
}): boolean {
  if (!hasStaleRuntimeState) return false;
  if (!lastRuntimeActivityAt) return true;
  return now - lastRuntimeActivityAt >= graceMs;
}

export function isTerminalRealtimeChatEvent(payload: any): boolean {
  const eventType = String(payload?.event_type || payload?.type || '').trim();
  return ['assistant_message', 'done', 'error', 'quota_exceeded', 'run_cancelled', 'run_completed'].includes(eventType);
}

function normalizedUserContent(message: AgentChatMessage): string {
  return String(message.content || '').replace(/\s+/g, ' ').trim();
}

function hasMatchingDurableUserMessage(messages: AgentChatMessage[], pending: PendingUserMessage): boolean {
  const pendingContent = normalizedUserContent(pending.message);
  if (!pendingContent) return false;
  return messages.some((message) => (
    message.role === 'user'
    && normalizedUserContent(message) === pendingContent
    && (!pending.message.fileName || pending.message.fileName === message.fileName)
  ));
}

export function mergePendingUserMessages(
  messages: AgentChatMessage[],
  pending: PendingUserMessage[],
): { messages: AgentChatMessage[]; pending: PendingUserMessage[] } {
  if (pending.length === 0) return { messages, pending };

  const remaining = pending.filter((item) => !hasMatchingDurableUserMessage(messages, item));
  if (remaining.length === 0) return { messages, pending: [] };

  const merged = [...messages];
  const sorted = [...remaining].sort((a, b) => a.anchorMessageCount - b.anchorMessageCount);
  sorted.forEach((item, offset) => {
    const index = Math.max(0, Math.min(item.anchorMessageCount + offset, merged.length));
    merged.splice(index, 0, item.message);
  });
  return { messages: merged, pending: remaining };
}

export function buildChatSocketKeepaliveMessage(): { type: 'ping' } {
  return { type: 'ping' };
}

export function createEmptyTranscriptReplayState(): TranscriptReplayState {
  return {
    messages: [],
    ui: { isWaiting: false, isStreaming: false },
    seenEventIds: new Set<string>(),
    pendingSessionPermissions: [],
  };
}

export function applyStreamingChunkEvent(
  messages: AgentChatMessage[],
  event: StreamingChunkEvent,
): AgentChatMessage[] {
  const last = messages[messages.length - 1];
  if (event.reset) {
    if (last && last.role === 'assistant' && (last as any)._streaming) {
      return [...messages.slice(0, -1), { ...last, content: '' } as any];
    }
    return messages;
  }
  const content = event.content ?? '';
  if (last && last.role === 'assistant' && (last as any)._streaming) {
    return [...messages.slice(0, -1), { ...last, content: last.content + content } as any];
  }
  return [...messages, { role: 'assistant', content, _streaming: true } as any];
}

function isStreamingAssistantPlaceholder(message: AgentChatMessage | undefined): boolean {
  return Boolean(
    message
      && message.role === 'assistant'
      && (message as any)._streaming
      && !String(message.content || '').trim(),
  );
}

function isTerminalToolCard(message: AgentChatMessage): boolean {
  if (message.role !== 'tool_call' || message.toolStatus !== 'done') return false;
  const kind = message.toolMeta?.kind;
  return kind === 'user_clarification'
    || kind === 'plan_proposal'
    || kind === 'dynamic_workflow_proposal'
    || kind === 'plan_mode_request'
    || kind === 'create_employee_success';
}

export function appendToolCallMessage(
  messages: AgentChatMessage[],
  toolMessage: AgentChatMessage,
): AgentChatMessage[] {
  const terminalCard = isTerminalToolCard(toolMessage);
  let base = messages;
  const last = base[base.length - 1];
  if (terminalCard && isStreamingAssistantPlaceholder(last)) {
    base = base.slice(0, -1);
  }

  const lastIdx = base.length - 1;
  const currentLast = base[lastIdx];
  const currentToolCallId = currentLast?.role === 'tool_call' && currentLast.toolMeta?.kind === 'runtime_step'
    ? currentLast.toolMeta.toolCallId
    : null;
  const nextToolCallId = toolMessage.toolMeta?.kind === 'runtime_step' ? toolMessage.toolMeta.toolCallId : null;
  if (
    toolMessage.toolStatus === 'done'
    && currentLast
    && currentLast.role === 'tool_call'
    && (currentLast.toolName === toolMessage.toolName || (currentToolCallId && currentToolCallId === nextToolCallId))
    && currentLast.toolStatus === 'running'
  ) {
    return [...base.slice(0, lastIdx), toolMessage];
  }
  if (
    currentLast
    && currentLast.role === 'tool_call'
    && currentLast.toolName === toolMessage.toolName
    && currentLast.toolStatus === toolMessage.toolStatus
    && currentLast.toolResult === toolMessage.toolResult
  ) {
    return base;
  }
  return [...base, toolMessage];
}

export function applyRuntimeDoneEvent(
  messages: AgentChatMessage[],
  event: any,
): AgentChatMessage[] {
  const content = typeof event?.content === 'string' ? event.content : '';
  const artifacts = extractArtifactParts(event);
  const last = messages[messages.length - 1];

  if (!content.trim() && artifacts.length === 0) {
    if (isStreamingAssistantPlaceholder(last)) {
      return messages.slice(0, -1);
    }
    return messages;
  }

  const assistantMessage: AgentChatMessage = {
    role: 'assistant',
    content,
    thinking: (last && last.role === 'assistant' && (last as any)._streaming) ? last.thinking : undefined,
    artifacts: artifacts.length > 0 ? artifacts : undefined,
    timestamp: new Date().toISOString(),
  };
  if (last && last.role === 'assistant' && (last as any)._streaming) {
    return [...messages.slice(0, -1), assistantMessage];
  }
  if (
    last
    && last.role === 'assistant'
    && !((last as any)._streaming)
    && last.content === assistantMessage.content
    && JSON.stringify(last.artifacts || []) === JSON.stringify(assistantMessage.artifacts || [])
  ) {
    return messages;
  }
  return [...messages, assistantMessage];
}

function transcriptEventKey(event: ChatTranscriptEventPayload): string | null {
  if (typeof event.id === 'string' && event.id.trim()) return `id:${event.id}`;
  if (typeof event.sequence === 'number') return `seq:${event.sequence}`;
  return null;
}

function transcriptEventType(event: ChatTranscriptEventPayload): string {
  return String(event.event_type || event.type || '');
}

function parseTranscriptObject(content: string): Record<string, unknown> | null {
  if (!content.trim()) return null;
  try {
    const parsed = JSON.parse(content);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

const TERMINAL_TRANSCRIPT_EVENT_TYPES = new Set(['assistant_message', 'run_completed', 'done', 'error', 'quota_exceeded']);

export function getTerminalRunIdFromTranscriptEvent(event: ChatTranscriptEventPayload): string | null {
  const runId = typeof event.run_id === 'string' && event.run_id.trim() ? event.run_id.trim() : null;
  if (!runId) return null;
  return TERMINAL_TRANSCRIPT_EVENT_TYPES.has(transcriptEventType(event)) ? runId : null;
}

function isBlockingToolMessage(message: AgentChatMessage): boolean {
  const toolMeta = message.toolMeta as (ToolCallMeta & { blocking?: boolean }) | null | undefined;
  return message.role === 'tool_call'
    && Boolean(toolMeta?.blocking || isTerminalToolCard(message));
}

function toolNameFromTranscriptEvent(event: ChatTranscriptEventPayload): string | undefined {
  const metadataName = event.metadata?.tool_name;
  return typeof metadataName === 'string' && metadataName.trim()
    ? metadataName.trim()
    : undefined;
}

function numberOrNull(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function sessionPermissionRequestId(message: AgentChatMessage): string | null {
  const requestId = message.sessionPermissionRequest?.permission_request_id;
  return typeof requestId === 'string' && requestId.trim() ? requestId : null;
}

function isPendingSessionPermissionMessage(message: AgentChatMessage): boolean {
  return (
    message.role === 'event' &&
    message.eventStatus === 'session_permission_required' &&
    Boolean(sessionPermissionRequestId(message))
  );
}

function permissionDecisionRequestId(event: ChatTranscriptEventPayload, content: string): string | null {
  const eventId = event.permission_request_id;
  if (typeof eventId === 'string' && eventId.trim()) return eventId;
  const metadataId = event.metadata?.permission_request_id;
  if (typeof metadataId === 'string' && metadataId.trim()) return metadataId;
  const parsed = parseTranscriptObject(content);
  const parsedId = parsed?.permission_request_id;
  return typeof parsedId === 'string' && parsedId.trim() ? parsedId : null;
}

function removeSessionPermissionMessage(
  messages: AgentChatMessage[],
  permissionRequestId: string,
): AgentChatMessage[] {
  return messages.filter((message) => sessionPermissionRequestId(message) !== permissionRequestId);
}

function renderSessionPermissionQueue(
  messages: AgentChatMessage[],
  pendingSessionPermissions: AgentChatMessage[],
): AgentChatMessage[] {
  const messagesWithoutPending = messages.filter((message) => !isPendingSessionPermissionMessage(message));
  const visiblePermission = pendingSessionPermissions[0];
  return visiblePermission ? [...messagesWithoutPending, visiblePermission] : messagesWithoutPending;
}

function upsertSessionPermissionQueue(
  pendingSessionPermissions: AgentChatMessage[],
  permissionMessage: AgentChatMessage,
): AgentChatMessage[] {
  const requestId = sessionPermissionRequestId(permissionMessage);
  if (!requestId) return pendingSessionPermissions;
  const existingIndex = pendingSessionPermissions.findIndex(
    (message) => sessionPermissionRequestId(message) === requestId,
  );
  if (existingIndex < 0) return [...pendingSessionPermissions, permissionMessage];
  return pendingSessionPermissions.map((message, index) => (index === existingIndex ? permissionMessage : message));
}

function resolveSessionPermissionQueue(
  pendingSessionPermissions: AgentChatMessage[],
  permissionRequestId: string,
): AgentChatMessage[] {
  return pendingSessionPermissions.filter((message) => sessionPermissionRequestId(message) !== permissionRequestId);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function visibilityFromValue(value: unknown): 'visible' | 'collapsed' | 'debug' | 'redacted' {
  return value === 'visible' || value === 'debug' || value === 'redacted' ? value : 'collapsed';
}

function toolResultFromTranscriptEvent(event: ChatTranscriptEventPayload): {
  toolName?: string;
  result: unknown;
  args?: Record<string, unknown>;
  status?: string;
  runtimeStepMeta?: ToolCallMeta | null;
} {
  const content = event.content || '';
  const metadataToolName = toolNameFromTranscriptEvent(event);
  const envelope = parseTranscriptObject(content);
  const envelopeToolName = typeof envelope?.name === 'string' && envelope.name.trim()
    ? envelope.name.trim()
    : undefined;
  const envelopeStatus = typeof envelope?.status === 'string' ? envelope.status : undefined;
  const looksLikePersistedToolEnvelope = Boolean(
    envelope
      && (
        Object.prototype.hasOwnProperty.call(envelope, 'result')
        || Object.prototype.hasOwnProperty.call(envelope, 'args')
        || Object.prototype.hasOwnProperty.call(envelope, 'tool_call_id')
        || Object.prototype.hasOwnProperty.call(envelope, 'step_id')
        || envelopeStatus === 'running'
        || envelopeStatus === 'done'
        || envelopeStatus === 'completed'
        || envelopeStatus === 'failed'
      )
      && (envelopeToolName || envelopeStatus || Object.prototype.hasOwnProperty.call(envelope, 'args')),
  );
  if (looksLikePersistedToolEnvelope) {
    const meta = event.metadata || {};
    const toolCallId = stringOrNull(envelope?.tool_call_id ?? meta.tool_call_id ?? meta.toolCallId);
    const stepId = stringOrNull(envelope?.step_id ?? meta.step_id ?? meta.stepId);
    const durationMs = numberOrNull(envelope?.duration_ms ?? meta.duration_ms ?? meta.durationMs);
    const status = typeof envelope?.status === 'string' ? envelope.status : (typeof meta.status === 'string' ? meta.status : undefined);
    return {
      toolName: metadataToolName || envelopeToolName,
      result: envelope?.result ?? '',
      args: envelope?.args && typeof envelope.args === 'object' && !Array.isArray(envelope.args)
        ? envelope.args as Record<string, unknown>
        : undefined,
      status,
      runtimeStepMeta: (toolCallId || stepId || durationMs != null || meta.visibility || envelope?.visibility)
        ? {
            kind: 'runtime_step',
            toolCallId,
            stepId,
            durationMs,
            visibility: visibilityFromValue(envelope?.visibility ?? meta.visibility),
            status: status || null,
          }
        : null,
    };
  }
  return { toolName: metadataToolName, result: content };
}

export function applyTranscriptEvent(
  state: TranscriptReplayState,
  event: ChatTranscriptEventPayload,
): TranscriptReplayState {
  const key = transcriptEventKey(event);
  if (key && state.seenEventIds.has(key)) return state;

  const seenEventIds = new Set(state.seenEventIds);
  if (key) seenEventIds.add(key);
  const eventType = transcriptEventType(event);
  const timestamp = event.created_at || event.timestamp;
  const content = event.content || '';
  const pendingSessionPermissions = state.pendingSessionPermissions || [];

  if (eventType === 'run_started') {
    return {
      messages: state.messages,
      seenEventIds,
      ui: { isWaiting: true, isStreaming: false },
      pendingSessionPermissions,
    };
  }

  if (eventType === 'run_completed' || eventType === 'done') {
    return {
      messages: state.messages,
      seenEventIds,
      ui: { isWaiting: false, isStreaming: false },
      pendingSessionPermissions,
    };
  }

  if (eventType === 'session_permission_decision' || eventType === 'permission_resolved') {
    const requestId = permissionDecisionRequestId(event, content);
    const nextPendingSessionPermissions = requestId
      ? resolveSessionPermissionQueue(pendingSessionPermissions, requestId)
      : pendingSessionPermissions;
    return {
      messages: requestId
        ? renderSessionPermissionQueue(
            removeSessionPermissionMessage(state.messages, requestId),
            nextPendingSessionPermissions,
          )
        : state.messages,
      seenEventIds,
      ui: { isWaiting: false, isStreaming: false },
      pendingSessionPermissions: nextPendingSessionPermissions,
    };
  }

  if (eventType === 'thinking') {
    return {
      messages: applyStreamingChunkEvent(state.messages, { type: 'chunk', content: '' }).map((message, index, arr) => {
        if (index !== arr.length - 1 || message.role !== 'assistant') return message;
        return { ...message, thinking: `${message.thinking || ''}${content}`, timestamp } as AgentChatMessage;
      }),
      seenEventIds,
      ui: { isWaiting: false, isStreaming: true },
      pendingSessionPermissions,
    };
  }

  if (eventType === 'assistant_delta' || eventType === 'chunk') {
    return {
      messages: applyStreamingChunkEvent(state.messages, { type: 'chunk', content }),
      seenEventIds,
      ui: { isWaiting: false, isStreaming: true },
      pendingSessionPermissions,
    };
  }

  if (eventType === 'user_message' || event.role === 'user') {
    return {
      messages: [
        ...state.messages,
        {
          role: 'user',
          content,
          timestamp,
          id: event.message_id || event.id,
        },
      ],
      seenEventIds,
      ui: state.ui,
      pendingSessionPermissions,
    };
  }

  if (eventType === 'artifact_delivery') {
    const artifacts = extractArtifactParts({
      artifacts: event.metadata?.artifacts,
      parts: event.parts,
    });
    if (messageAlreadyContainsArtifacts(state.messages[state.messages.length - 1], artifacts)) {
      return { messages: state.messages, seenEventIds, ui: state.ui, pendingSessionPermissions };
    }
    const messages = applyRuntimeDoneEvent(state.messages, {
      type: 'done',
      content: '',
      artifacts,
      created_at: timestamp,
    });
    return {
      messages: messages.map((message, index, arr) => (
        index === arr.length - 1 && message.role === 'assistant'
          ? { ...message, timestamp, id: event.message_id || event.id }
          : message
      )),
      seenEventIds,
      ui: state.ui,
      pendingSessionPermissions,
    };
  }

  if (eventType === 'tool_result' || eventType === 'tool_call' || event.role === 'tool_call') {
    const toolPayload = toolResultFromTranscriptEvent(event);
    const normalized = normalizeToolCallResult(toolPayload.toolName, toolPayload.result);
    const toolStatus = toolPayload.status === 'running' ? 'running' : 'done';
    const toolMeta = mergeClarificationAnswerMetadata(
      normalized.toolMeta || toolPayload.runtimeStepMeta || null,
      event.metadata,
    );
    const artifacts = extractArtifactParts(event);
    const toolMessage: AgentChatMessage = {
      role: 'tool_call',
      content: '',
      toolName: toolPayload.toolName,
      toolArgs: toolPayload.args,
      toolStatus,
      toolResult: normalized.displayResult,
      toolRawResult: normalized.raw,
      toolMeta,
      artifacts: artifacts.length > 0 ? artifacts : undefined,
      timestamp,
      id: event.message_id || event.id,
    };
    return {
      messages: appendToolCallMessage(state.messages, toolMessage),
      seenEventIds,
      ui: isBlockingToolMessage(toolMessage)
        ? { isWaiting: false, isStreaming: false }
        : { isWaiting: false, isStreaming: state.ui.isStreaming },
      pendingSessionPermissions,
    };
  }

  if (eventType === 'assistant_message' || event.role === 'assistant') {
    const messages = applyRuntimeDoneEvent(state.messages, {
      type: 'done',
      content,
      parts: event.parts,
      created_at: timestamp,
    });
    return {
      messages: messages.map((message, index, arr) => (
        index === arr.length - 1 && message.role === 'assistant'
          ? { ...message, timestamp, id: event.message_id || event.id }
          : message
      )),
      seenEventIds,
      ui: { isWaiting: false, isStreaming: false },
      pendingSessionPermissions,
    };
  }

  const runtimeEvent = getRuntimeEventMessage({
    ...(event.metadata || {}),
    ...event,
    content: content || (typeof event.metadata?.message === 'string' ? event.metadata.message : ''),
    type: eventType,
    timestamp,
  });
  if (runtimeEvent) {
    const pendingRequestId = sessionPermissionRequestId(runtimeEvent);
    if (runtimeEvent.eventStatus === 'session_permission_required' && pendingRequestId) {
      const nextPendingSessionPermissions = upsertSessionPermissionQueue(pendingSessionPermissions, runtimeEvent);
      return {
        messages: renderSessionPermissionQueue(state.messages, nextPendingSessionPermissions),
        seenEventIds,
        ui: { isWaiting: false, isStreaming: false },
        pendingSessionPermissions: nextPendingSessionPermissions,
      };
    }
    return {
      messages: [...state.messages, runtimeEvent],
      seenEventIds,
      ui: { isWaiting: false, isStreaming: false },
      pendingSessionPermissions,
    };
  }

  return { ...state, seenEventIds };
}

export function replayTranscriptEvents(events: ChatTranscriptEventPayload[]): TranscriptReplayState {
  return events.reduce(
    (state, event) => applyTranscriptEvent(state, event),
    createEmptyTranscriptReplayState(),
  );
}

type ActiveModelSummary = {
  label?: string;
  provider?: string;
  model?: string;
  supports_vision?: boolean;
  max_input_tokens?: number | null;
};

type BuildRuntimeSummaryInput = {
  persistedSummary?: Partial<ChatRuntimeSummary> | null;
  activeModel?: ActiveModelSummary | null;
  agentPrimaryModelId?: string | null;
  agentContextWindowSize?: number | null;
  messages: AgentChatMessage[];
  connected: boolean;
};

type EventPart = {
  type?: string;
  event_type?: RuntimeEventType;
  title?: string;
  text?: string;
  status?: string;
  tool_name?: string;
  approval_id?: string;
  security_zone?: string;
  capability?: string;
  approval_required?: boolean;
  reason?: string;
  next_step?: string;
  retryable?: boolean;
  retry_reason?: string;
  permission_request_id?: string;
  permission_request?: SessionPermissionRequest;
  hook_event?: string;
  hook_key?: string;
  hook_type?: string;
  runtime_task_id?: string;
  turn_id?: string;
  tool_call_id?: string;
  child_session_id?: string;
  parent_session_id?: string;
  root_session_id?: string;
  workflow_run_id?: string;
  workflow_step_id?: string;
  schedule_id?: string;
  schedule_fire_id?: string;
  goal_id?: string;
  once_id?: string;
  memory_candidate_id?: string;
  artifact_id?: string;
  path?: string;
  revision_id?: string;
  action?: string;
  diff_summary?: string;
  original_message_count?: number;
  kept_message_count?: number;
  continuity_sections_injected?: string[];
  tool_groups?: Array<string | { name?: string }>;
  packs?: Array<string | { name?: string }>;
  skill_name?: string;
  trigger_tool?: string;
};

const RUNTIME_EVENT_TYPES = new Set<RuntimeEventType>([
  'permission',
  'session_compact',
  'tool_group_activation',
  'deferred_tools_delta',
  'pack_activation',
  'team_memory',
  'permission_request',
  'permission_resolved',
  'hook_progress',
  'hook_summary',
  'hook_attachment',
  'hook_blocked',
  'workflow_run',
  'workflow_step',
  'dynamic_workflow',
  'child_session',
  'subagent',
  'team_member',
  'schedule',
  'schedule_fire',
  'goal',
  'once',
  'memory_candidate',
  'artifact_update',
  'artifact_delivery',
]);
const RAW_COMPACTION_SECTION_LABELS = [
  'Task Ledger',
  'Decision Ledger',
  'Artifact Ledger',
  'Tool Ledger',
  'Preference Ledger',
  'Pending Ledger',
  'Primary Request and Intent',
  'Key Technical Decisions',
  'Files and Code Sections',
  'Problem Solving',
  'Errors and Fixes',
  'All User Messages',
  'User Preferences',
  'Tool Outcomes',
  'Pending Tasks',
  'Current Work',
  'Recovery Context',
];
const RAW_COMPACTION_SECTION_PATTERN = RAW_COMPACTION_SECTION_LABELS
  .map((label) => label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  .join('|');
const RAW_COMPACTION_SECTION_RE = new RegExp(`^\\*\\*(${RAW_COMPACTION_SECTION_PATTERN}):\\*\\*`, 'gim');

function isRuntimeEventType(value: unknown): value is RuntimeEventType {
  return typeof value === 'string' && RUNTIME_EVENT_TYPES.has(value as RuntimeEventType);
}

function getEventPart(payload: any): EventPart | undefined {
  if (payload?.part && typeof payload.part === 'object') return payload.part as EventPart;
  if (Array.isArray(payload?.parts)) {
    return payload.parts.find((part: EventPart) => part?.type === 'event');
  }
  return undefined;
}

function normalizeArtifactPart(part: any): ChatArtifactPart | null {
  if (!part || typeof part !== 'object') return null;
  if (part.type && part.type !== 'artifact') return null;
  const path = typeof part.path === 'string' ? part.path.trim() : '';
  if (!path) return null;
  const fallbackName = path.split('/').filter(Boolean).pop() || path;
  const size = typeof part.size === 'number' && Number.isFinite(part.size) ? part.size : undefined;
  return {
    id: typeof part.id === 'string' ? part.id : (typeof part.artifact_id === 'string' ? part.artifact_id : undefined),
    name: typeof part.name === 'string' && part.name.trim() ? part.name.trim() : fallbackName,
    path,
    previewKind: typeof part.previewKind === 'string' ? part.previewKind : (typeof part.preview_kind === 'string' ? part.preview_kind : undefined),
    mimeType: typeof part.mimeType === 'string' ? part.mimeType : (typeof part.mime_type === 'string' ? part.mime_type : undefined),
    size,
    source: typeof part.source === 'string' ? part.source : undefined,
    runtimeTaskId: typeof part.runtimeTaskId === 'string' ? part.runtimeTaskId : (typeof part.runtime_task_id === 'string' ? part.runtime_task_id : undefined),
    revisionId: typeof part.revisionId === 'string' ? part.revisionId : (typeof part.revision_id === 'string' ? part.revision_id : undefined),
    action: typeof part.action === 'string' ? part.action : undefined,
    toolCallId: typeof part.toolCallId === 'string' ? part.toolCallId : (typeof part.tool_call_id === 'string' ? part.tool_call_id : undefined),
    diffSummary: typeof part.diffSummary === 'string' ? part.diffSummary : (typeof part.diff_summary === 'string' ? part.diff_summary : undefined),
    previewSnapshotContent: typeof part.previewSnapshotContent === 'string'
      ? part.previewSnapshotContent
      : (typeof part.preview_snapshot_content === 'string' ? part.preview_snapshot_content : undefined),
    previewSnapshotTruncated: typeof part.previewSnapshotTruncated === 'boolean'
      ? part.previewSnapshotTruncated
      : (typeof part.preview_snapshot_truncated === 'boolean' ? part.preview_snapshot_truncated : undefined),
  };
}

export function extractArtifactParts(payload: any): ChatArtifactPart[] {
  const candidates = [
    ...(Array.isArray(payload?.artifacts) ? payload.artifacts : []),
    ...(Array.isArray(payload?.parts) ? payload.parts.filter((part: any) => part?.type === 'artifact') : []),
  ];
  const artifacts: ChatArtifactPart[] = [];
  const seen = new Set<string>();
  for (const candidate of candidates) {
    const artifact = normalizeArtifactPart(candidate);
    if (!artifact) continue;
    const key = `${artifact.id || ''}:${artifact.path}`;
    if (seen.has(key)) continue;
    seen.add(key);
    artifacts.push(artifact);
  }
  return artifacts;
}

function messageAlreadyContainsArtifacts(message: AgentChatMessage | undefined, artifacts: ChatArtifactPart[]): boolean {
  if (!message || message.role !== 'assistant' || artifacts.length === 0) return false;
  const existingArtifacts = message.artifacts || [];
  if (existingArtifacts.length === 0) return false;
  const existingIds = new Set(existingArtifacts.map((artifact) => artifact.id).filter(Boolean));
  const existingPaths = new Set(existingArtifacts.map((artifact) => artifact.path).filter(Boolean));
  return artifacts.every((artifact) => (
    (artifact.id && existingIds.has(artifact.id)) || existingPaths.has(artifact.path)
  ));
}

function countActivatedToolGroups(
  groups: EventPart['tool_groups'] | undefined,
): number | undefined {
  if (!Array.isArray(groups)) return undefined;
  const valid = groups.filter((group) => (typeof group === 'string' ? group : group?.name));
  return valid.length > 0 ? valid.length : undefined;
}

function parseJsonObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string' || !value.trim().startsWith('{')) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function isRawCompactionSummaryContent(content: unknown): content is string {
  if (typeof content !== 'string') return false;
  const text = content.trim();
  if (!text) return false;
  RAW_COMPACTION_SECTION_RE.lastIndex = 0;
  const sectionCount = Array.from(text.matchAll(RAW_COMPACTION_SECTION_RE)).length;
  return sectionCount >= 2 && /\*\*Recovery Context:\*\*/i.test(text);
}

function getEmbeddedRuntimeEventPayload(payload: any): Record<string, unknown> | null {
  const jsonEvent = payload?.role === 'system' ? parseJsonObject(payload?.content) : null;
  const jsonEventType = jsonEvent?.event_type || jsonEvent?.type;
  if (isRuntimeEventType(jsonEventType)) {
    return {
      ...jsonEvent,
      type: jsonEventType,
      timestamp: payload?.timestamp,
      created_at: payload?.created_at,
      id: payload?.id,
      sender_name: payload?.sender_name,
      participant_id: payload?.participant_id,
    };
  }

  if ((payload?.role === 'assistant' || payload?.role === 'event') && isRawCompactionSummaryContent(payload?.content)) {
    return {
      type: 'session_compact',
      title: 'Context Compacted',
      summary: payload.content,
      timestamp: payload?.timestamp,
      created_at: payload?.created_at,
      id: payload?.id,
      sender_name: payload?.sender_name,
      participant_id: payload?.participant_id,
    };
  }

  return null;
}

export function getCompactionDisplayContent(content: string): {
  compacted: boolean;
  visible: string;
  details: string | null;
} {
  const text = typeof content === 'string' ? content.trim() : '';
  if (!isRawCompactionSummaryContent(text)) {
    return { compacted: false, visible: text, details: null };
  }

  return {
    compacted: true,
    visible: '',
    details: text,
  };
}

export function computeComposerHeight(scrollHeight: number): number {
  return Math.min(MAX_COMPOSER_HEIGHT, Math.max(MIN_COMPOSER_HEIGHT, scrollHeight));
}

export function estimateRuntimeInputTokens(messages: AgentChatMessage[]): number {
  const totalChars = messages.reduce((total, message) => {
    const payload = [
      message.content,
      message.thinking,
      message.toolResult,
      message.toolName,
      message.fileName,
    ]
      .filter((part): part is string => typeof part === 'string' && part.length > 0)
      .join('\n');
    return total + payload.length;
  }, 0);
  return totalChars > 0 ? Math.max(1, Math.ceil(totalChars / 4)) : 0;
}

export function buildRuntimeSummary({
  persistedSummary,
  activeModel,
  agentPrimaryModelId,
  agentContextWindowSize,
  messages,
  connected,
}: BuildRuntimeSummaryInput): ChatRuntimeSummary {
  const fallbackContextWindow =
    activeModel?.max_input_tokens ??
    agentContextWindowSize ??
    null;
  const fallbackEstimatedTokens = estimateRuntimeInputTokens(messages);
  const backendModel = persistedSummary?.model || {};
  const backendRuntime = persistedSummary?.runtime || {};
  const contextWindowTokens =
    backendModel.context_window_tokens ??
    fallbackContextWindow;
  const estimatedInputTokens =
    backendRuntime.estimated_input_tokens ??
    fallbackEstimatedTokens;

  return {
    model: {
      label: backendModel.label || activeModel?.label || agentPrimaryModelId || 'Unknown model',
      provider: backendModel.provider || activeModel?.provider,
      name: backendModel.name || activeModel?.model,
      supports_vision: backendModel.supports_vision ?? activeModel?.supports_vision,
      context_window_tokens: contextWindowTokens,
    },
    runtime: {
      connected: backendRuntime.connected ?? connected,
      estimated_input_tokens: estimatedInputTokens,
      remaining_tokens_estimate:
        backendRuntime.remaining_tokens_estimate ??
        (typeof contextWindowTokens === 'number'
          ? Math.max(contextWindowTokens - estimatedInputTokens, 0)
          : null),
    },
    activated_tool_groups: persistedSummary?.activated_tool_groups || [],
    used_tools: persistedSummary?.used_tools || [],
    blocked_capabilities: persistedSummary?.blocked_capabilities || [],
    compaction_count: persistedSummary?.compaction_count || 0,
    permission_event_count: persistedSummary?.permission_event_count || 0,
    team_memory_hit_count: persistedSummary?.team_memory_hit_count || 0,
    last_compaction: persistedSummary?.last_compaction || null,
    last_team_memory_hit: persistedSummary?.last_team_memory_hit || null,
    last_tool_budget_event: persistedSummary?.last_tool_budget_event || null,
    last_retry_reason: persistedSummary?.last_retry_reason || null,
  };
}

export function getTransportNotice(payload: any): string | null {
  if (payload?.type !== 'info') return null;
  const text = payload?.content || payload?.message;
  return typeof text === 'string' && text.trim() ? text : null;
}

export function getRuntimeEventMessage(payload: any): AgentChatMessage | null {
  const eventType = payload?.eventType || payload?.event_type || payload?.type;
  if (!isRuntimeEventType(eventType)) return null;

  const part = getEventPart(payload);
  // New events carry `tool_groups`; historical persisted events carry `packs`.
  const activatedToolGroupCount = countActivatedToolGroups(
    payload?.tool_groups ?? part?.tool_groups ?? payload?.packs ?? part?.packs,
  );
  const content =
    payload?.content ||
    payload?.message ||
    payload?.summary ||
    part?.text ||
    '';

  return {
    role: 'event',
    content,
    eventType,
    eventTitle:
      payload?.eventTitle ||
      payload?.title ||
      part?.title ||
      (eventType === 'session_compact' ? 'Context Compacted' : undefined),
    eventStatus: payload?.eventStatus || payload?.status || part?.status || 'info',
    eventToolName: payload?.eventToolName || payload?.tool_name || part?.tool_name,
    eventApprovalId: payload?.eventApprovalId || payload?.approval_id || part?.approval_id,
    eventSecurityZone: payload?.eventSecurityZone || payload?.security_zone || part?.security_zone,
    eventCapability: payload?.eventCapability || payload?.capability || part?.capability,
    eventApprovalRequired:
      payload?.eventApprovalRequired ?? payload?.approval_required ?? part?.approval_required,
    eventReason: payload?.eventReason || payload?.reason || part?.reason,
    eventNextStep: payload?.eventNextStep || payload?.next_step || part?.next_step,
    eventRetryable: payload?.eventRetryable ?? payload?.retryable ?? part?.retryable,
    eventRetryReason: payload?.eventRetryReason || payload?.retry_reason || part?.retry_reason,
    eventRuntimeTaskId: payload?.eventRuntimeTaskId || payload?.runtime_task_id || part?.runtime_task_id,
    eventTurnId: payload?.eventTurnId || payload?.turn_id || part?.turn_id,
    eventToolCallId: payload?.eventToolCallId || payload?.tool_call_id || part?.tool_call_id,
    eventHookEvent: payload?.eventHookEvent || payload?.hook_event || part?.hook_event,
    eventHookKey: payload?.eventHookKey || payload?.hook_key || part?.hook_key,
    eventHookType: payload?.eventHookType || payload?.hook_type || part?.hook_type,
    eventChildSessionId: payload?.eventChildSessionId || payload?.child_session_id || part?.child_session_id,
    eventParentSessionId: payload?.eventParentSessionId || payload?.parent_session_id || part?.parent_session_id,
    eventRootSessionId: payload?.eventRootSessionId || payload?.root_session_id || part?.root_session_id,
    eventWorkflowRunId: payload?.eventWorkflowRunId || payload?.workflow_run_id || part?.workflow_run_id,
    eventWorkflowStepId: payload?.eventWorkflowStepId || payload?.workflow_step_id || part?.workflow_step_id,
    eventScheduleId: payload?.eventScheduleId || payload?.schedule_id || part?.schedule_id,
    eventScheduleFireId: payload?.eventScheduleFireId || payload?.schedule_fire_id || part?.schedule_fire_id,
    eventGoalId: payload?.eventGoalId || payload?.goal_id || part?.goal_id,
    eventOnceId: payload?.eventOnceId || payload?.once_id || part?.once_id,
    eventMemoryCandidateId: payload?.eventMemoryCandidateId || payload?.memory_candidate_id || part?.memory_candidate_id,
    eventArtifactId: payload?.eventArtifactId || payload?.artifact_id || part?.artifact_id,
    eventPath: payload?.eventPath || payload?.path || part?.path,
    eventRevisionId: payload?.eventRevisionId || payload?.revision_id || part?.revision_id,
    eventAction: payload?.eventAction || payload?.action || part?.action,
    eventDiffSummary: payload?.eventDiffSummary || payload?.diff_summary || part?.diff_summary,
    sessionPermissionRequest:
      payload?.sessionPermissionRequest ||
      payload?.permission_request ||
      part?.permission_request ||
      (payload?.permission_request_id || part?.permission_request_id
        ? { permission_request_id: payload?.permission_request_id || part?.permission_request_id }
        : undefined),
    originalMessageCount:
      payload?.originalMessageCount ??
      payload?.original_message_count ??
      part?.original_message_count,
    keptMessageCount:
      payload?.keptMessageCount ??
      payload?.kept_message_count ??
      part?.kept_message_count,
    continuitySectionsInjected:
      payload?.continuitySectionsInjected ??
      payload?.continuity_sections_injected ??
      part?.continuity_sections_injected,
    activatedToolGroupCount,
    skillName: payload?.skillName || payload?.skill_name || part?.skill_name,
    triggerTool: payload?.triggerTool || payload?.trigger_tool || part?.trigger_tool,
    timestamp: payload?.timestamp || payload?.created_at,
    sender_name: payload?.sender_name,
    participant_id: payload?.participant_id,
    id: payload?.id,
  };
}

export function normalizeRuntimeEventMessage(payload: any): AgentChatMessage | null {
  const embeddedPayload = getEmbeddedRuntimeEventPayload(payload);
  if (embeddedPayload) return getRuntimeEventMessage(embeddedPayload);
  return getRuntimeEventMessage(payload);
}

export function normalizeStoredChatMessage(payload: any): AgentChatMessage {
  const eventMessage = normalizeRuntimeEventMessage(payload);
  if (eventMessage) return eventMessage;
  const artifacts = extractArtifactParts(payload);

  if (payload?.role === 'tool_call') {
    const normalized = normalizeToolCallResult(payload?.toolName, payload?.toolRawResult ?? payload?.toolResult);
    const toolMeta = payload?.toolMeta ?? normalized.toolMeta;
    return {
      role: 'tool_call',
      content: payload?.content || '',
      toolName: payload?.toolName,
      toolArgs: payload?.toolArgs,
      toolStatus: payload?.toolStatus,
      toolResult: normalized.toolMeta ? normalized.displayResult : payload?.toolResult,
      toolRawResult: payload?.toolRawResult ?? payload?.toolResult ?? normalized.raw,
      toolMeta,
      artifacts: artifacts.length > 0 ? artifacts : undefined,
      thinking: payload?.thinking,
      timestamp: payload?.created_at || payload?.timestamp,
      sender_name: payload?.sender_name,
      participant_id: payload?.participant_id,
      id: payload?.id,
    };
  }

  return {
    role: payload?.role === 'assistant' ? 'assistant' : 'user',
    content: payload?.content || '',
    artifacts: artifacts.length > 0 ? artifacts : undefined,
    thinking: payload?.thinking,
    timestamp: payload?.created_at || payload?.timestamp,
    sender_name: payload?.sender_name,
    participant_id: payload?.participant_id,
    id: payload?.id,
  };
}
