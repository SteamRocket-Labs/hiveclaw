import type { SessionRuntimeSummary } from '../../api/domains/chat';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import {
  normalizeThreadItemPayload,
  threadItemToAgentChatMessage,
} from '../session-workbench/threadItemReducer';
import type { SessionItemV2 } from '../session-workbench/sessionEventStore';
import { normalizeToolCallResult, type ToolCallMeta } from './toolResultEnvelope';
import type { RuntimeBudgetState } from './runtimeBudgetState';

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
  | 'agent_task_notification'
  | 'subagent'
  | 'team_member'
  | 'schedule'
  | 'schedule_fire'
  | 'goal'
  | 'once'
  | 'memory_candidate'
  | 'artifact_update'
  | 'artifact_delivery'
  | 'file_changes'
  | 'runtime_action_started'
  | 'runtime_action_progress'
  | 'runtime_action_completed'
  | 'runtime_action_blocked'
  | 'runtime_action_failed'
  | 'memory_context_degraded'
  | 'memory_context_unavailable';

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
	  messageId?: string | null;
	  transcriptEventId?: string | null;
	  eventType?: string;
  /** Canonical UI protocol. Optional only while historical rows cross the compatibility reducer. */
  threadItem?: ThreadItem;
  /** Session V2 canonical item. Present only for the canonical event-store projection. */
  sessionItem?: SessionItemV2;
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
  eventNotificationSource?: string;
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

const COMPACT_UUID_RUN_ID_RE = /^[0-9a-fA-F]{32}$/;
const CANONICAL_UUID_RUN_ID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/**
 * RuntimeTask UUIDs cross two truthful wire surfaces: live run payloads use
 * ``UUID.hex`` while canonical SessionEvent scopes use the hyphenated UUID.
 * Normalize only those exact mechanical forms; arbitrary provider/runtime
 * identifiers remain byte-distinct after the existing whitespace trim.
 */
export function normalizeSessionRunId(value: string | null | undefined): string {
  const runId = String(value || '').trim();
  if (!COMPACT_UUID_RUN_ID_RE.test(runId) && !CANONICAL_UUID_RUN_ID_RE.test(runId)) return runId;
  const compact = runId.replaceAll('-', '').toLowerCase();
  return `${compact.slice(0, 8)}-${compact.slice(8, 12)}-${compact.slice(12, 16)}-${compact.slice(16, 20)}-${compact.slice(20)}`;
}

export function isSameSessionRunId(
  left: string | null | undefined,
  right: string | null | undefined,
): boolean {
  const normalizedLeft = normalizeSessionRunId(left);
  const normalizedRight = normalizeSessionRunId(right);
  return Boolean(normalizedLeft && normalizedRight && normalizedLeft === normalizedRight);
}

export function agentChatMessageRunId(message?: AgentChatMessage): string | null {
  const scope = message?.sessionItem?.scope;
  const scopedRunId = scope && 'run_id' in scope ? scope.run_id : null;
  const runId = typeof scopedRunId === 'string' && scopedRunId.trim()
    ? scopedRunId
    : message?.eventRuntimeTaskId || message?.threadItem?.run_id;
  const normalized = typeof runId === 'string' ? normalizeSessionRunId(runId) : '';
  return normalized || null;
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
  contentHash?: string;
  snapshotHash?: string;
  snapshotStoragePath?: string;
  previewSnapshotContent?: string;
  previewSnapshotTruncated?: boolean;
  ownerAgentId?: string;
  sourceAgentId?: string;
  downloadAgentId?: string;
  deliveryAgentId?: string;
  sourceArtifactId?: string;
  producerAgentId?: string;
  sourceSessionId?: string;
  rootSessionId?: string;
  ownerAgentName?: string;
  sourceAgentName?: string;
  downloadAgentName?: string;
  deliveryAgentName?: string;
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

export type ChatTranscriptEventPayload = Partial<ThreadItem> & {
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
  phase?: string;
  parts?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  created_at?: string;
  timestamp?: string;
  message_id?: string | null;
  payload?: Record<string, unknown>;
};

function messageIdentityFromTranscriptEvent(event: ChatTranscriptEventPayload): Pick<AgentChatMessage, 'id' | 'messageId' | 'transcriptEventId'> {
  const messageId = event.message_id ? String(event.message_id) : null;
  const transcriptEventId = event.id ? String(event.id) : null;
  return {
    id: messageId || transcriptEventId || undefined,
    messageId,
    transcriptEventId,
  };
}

function messageIdentityFromStoredPayload(payload: any): Pick<AgentChatMessage, 'messageId' | 'transcriptEventId'> {
  const metadata = payload?.metadata && typeof payload.metadata === 'object' ? payload.metadata : {};
  const messageId = payload?.message_id || payload?.id || null;
  const transcriptEventId = payload?.transcript_event_id || metadata?.transcript_event_id || null;
  return {
    messageId: messageId ? String(messageId) : null,
    transcriptEventId: transcriptEventId ? String(transcriptEventId) : null,
  };
}

export interface SessionRunState {
  runId: string;
  status: string;
  runtimeBudget?: RuntimeBudgetState;
}

/**
 * Terminal run-identity guard for the at-least-once transport contract.  A
 * terminal witness carrying a nonempty run id that differs from the
 * currently active run id is a stale old-run event: it must never clear the
 * active run, fail the active phase, or seal the active tail.  A null active
 * run (nothing live) or a null/blank terminal id (legacy frames without run
 * identity) keeps the historical behavior of accepting the terminal.
 */
export function isTerminalRunAcceptedForActiveRun(
  activeRunId: string | null | undefined,
  terminalRunId: string | null | undefined,
): boolean {
  const terminal = normalizeSessionRunId(terminalRunId);
  const active = normalizeSessionRunId(activeRunId);
  return !(terminal && active && terminal !== active);
}

/**
 * Active-run terminal bookkeeping for the at-least-once transport contract.
 * A terminal witness whose nonempty run id differs from the currently active
 * run id is a stale old-run event: it is recorded in ``terminalRunIds`` (so
 * the stale run is never re-observed as active) but it never marks the whole
 * session terminal and never clears the active run.  Returns whether the
 * terminal was accepted for the currently active run; ``clearActiveRun``
 * fires only on acceptance.
 */
export function markActiveRunTerminalInRegistry(
  terminalSessionKeys: Set<string>,
  terminalRunIds: Set<string>,
  key: string,
  activeRunId: string | null | undefined,
  terminalRunId: string | null | undefined,
  clearActiveRun: () => void,
): boolean {
  if (!isTerminalRunAcceptedForActiveRun(activeRunId, terminalRunId)) {
    const rejectedRunId = normalizeSessionRunId(terminalRunId);
    if (rejectedRunId) terminalRunIds.add(rejectedRunId);
    return false;
  }
  terminalSessionKeys.add(key);
  const runId = normalizeSessionRunId(terminalRunId || activeRunId);
  if (runId) terminalRunIds.add(runId);
  clearActiveRun();
  return true;
}

/**
 * RuntimePhase — frontend mirror of the backend first-class phase contract
 * (backend/app/runtime/runtime_phase.py, unified design 2026-07-09 §3.3).
 * `idle` is a frontend-only base state meaning "no run in this session".
 */
export type RuntimePhase =
  | 'idle'
  | 'queued'
  | 'resuming'
  | 'starting'
  | 'thinking'
  | 'responding'
  | 'tool_running'
  | 'hook_evaluating'
  | 'compacting'
  | 'awaiting_approval'
  | 'awaiting_budget'
  | 'summarizing'
  | 'continuation_gap'
  | 'done'
  | 'failed'
  | 'cancelled';

const RUNTIME_PHASE_VALUES: ReadonlySet<string> = new Set([
  'idle',
  'queued',
  'resuming',
  'starting',
  'thinking',
  'responding',
  'tool_running',
  'hook_evaluating',
  'compacting',
  'awaiting_approval',
  'awaiting_budget',
  'summarizing',
  'continuation_gap',
  'done',
  'failed',
  'cancelled',
]);

export const TERMINAL_RUNTIME_PHASES: ReadonlySet<RuntimePhase> = new Set([
  'done',
  'failed',
  'cancelled',
]);

export function terminalRuntimePhaseForSessionEvent(itemKind: string, lifecycle: string): RuntimePhase | null {
  if (itemKind === 'runtime_failure' && lifecycle === 'recorded') return 'failed';
  if (lifecycle === 'completed') return 'done';
  if (lifecycle === 'failed' || lifecycle === 'needs_reconciliation') return 'failed';
  if (lifecycle === 'cancelled') return 'cancelled';
  return null;
}

const STREAMING_RUNTIME_PHASES: ReadonlySet<RuntimePhase> = new Set([
  'thinking',
  'responding',
  'tool_running',
  'hook_evaluating',
  'compacting',
  'summarizing',
]);

const WAITING_RUNTIME_PHASES: ReadonlySet<RuntimePhase> = new Set([
  'queued',
  'resuming',
  'starting',
]);

export function isRuntimePhase(value: unknown): value is RuntimePhase {
  return typeof value === 'string' && RUNTIME_PHASE_VALUES.has(value);
}

export interface SessionUiState {
  phase: RuntimePhase;
  isWaiting: boolean;
  isStreaming: boolean;
}

/** Derive the legacy waiting/streaming booleans from the phase — the single source of truth. */
export function phaseUi(phase: RuntimePhase): { isWaiting: boolean; isStreaming: boolean } {
  return {
    isWaiting: WAITING_RUNTIME_PHASES.has(phase),
    isStreaming: STREAMING_RUNTIME_PHASES.has(phase),
  };
}

/** The only constructor for SessionUiState: booleans are always derived from the phase. */
export function uiForPhase(phase: RuntimePhase): SessionUiState {
  return { phase, ...phaseUi(phase) };
}

interface RuntimePhaseEventLike {
  type?: unknown;
  event_type?: unknown;
  phase?: unknown;
  status?: unknown;
  role?: unknown;
}

/**
 * Turn lifecycle state machine shared by the live WS hot path and transcript
 * replay. A backend `phase` event is authoritative; other events derive the
 * phase so replayed histories reach the same state without persisted phases.
 * Terminal phases are not sealed here: the session-level machine must reopen
 * when a new run starts in the same session.
 */
export function reduceRuntimePhase(current: RuntimePhase, event: RuntimePhaseEventLike): RuntimePhase {
  const eventType = String(event.event_type || event.type || '').replaceAll('.', '_');
  switch (eventType) {
    case 'phase':
      return isRuntimePhase(event.phase) ? event.phase : current;
    case 'run_queued':
      return 'queued';
    case 'run_started':
    case 'run_starting':
    case 'run_running':
      return 'starting';
    case 'thinking':
      return 'thinking';
    case 'chunk':
    case 'assistant_delta':
      return 'responding';
    case 'tool_call':
      return event.status === 'done' ? 'thinking' : 'tool_running';
    case 'tool_result':
      return 'thinking';
    case 'permission':
      return String(event.status || '') === 'session_permission_required' ? 'awaiting_approval' : current;
    case 'permission_resolved':
    case 'session_permission_decision':
      return 'starting';
    case 'done':
    case 'run_completed':
    case 'assistant_message':
      return 'done';
    case 'error':
    case 'quota_exceeded':
    case 'run_failed':
    case 'run_needs_reconciliation':
      return 'failed';
    case 'run_cancelled':
      return 'cancelled';
    default:
      return current;
  }
}

export type SessionTranscriptLoadSurface = 'chat' | 'history';

export interface SessionTranscriptLoadDescriptor {
  key: string;
  surface: SessionTranscriptLoadSurface;
  loadSeq?: number;
}

export type AgentOwnedSession = {
  id?: unknown;
  agent_id?: unknown;
  agentId?: unknown;
  user_id?: unknown;
  userId?: unknown;
  peer_agent_id?: unknown;
  peerAgentId?: unknown;
  source_channel?: unknown;
  thread_source?: unknown;
  participant_type?: unknown;
  session_kind?: unknown;
  read_only?: unknown;
  readOnly?: unknown;
  is_current_user_session?: unknown;
  isCurrentUserSession?: unknown;
  is_pending_session_lookup?: unknown;
  isPendingSessionLookup?: unknown;
  is_draft?: unknown;
  isDraft?: unknown;
  draft_client_id?: unknown;
  draftClientId?: unknown;
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

export function shouldReuseSessionTranscriptLoad(
  current: SessionTranscriptLoadDescriptor | null | undefined,
  next: SessionTranscriptLoadDescriptor,
): boolean {
  return Boolean(current && current.key === next.key && current.surface === next.surface);
}

export function buildSessionTranscriptLoadFailureMessage(content: string): AgentChatMessage {
  return {
    role: 'event',
    content,
    eventType: 'runtime_action_failed',
    eventStatus: 'session_load_failed',
    timestamp: new Date().toISOString(),
  };
}

export function isA2ASession(session: AgentOwnedSession | null | undefined): boolean {
  if (!session) return false;
  const source = String(session.source_channel ?? session.thread_source ?? '').toLowerCase();
  const participantType = String(session.participant_type ?? '').toLowerCase();
  const sessionKind = String(session.session_kind ?? '').toLowerCase();
  return (
    source === 'agent'
    || participantType === 'agent'
    || sessionKind === 'agent_chat'
    || sessionKind === 'delegation_run'
  );
}

export function isDraftHumanChatSession(session: AgentOwnedSession | null | undefined): boolean {
  if (!session) return false;
  if (session.is_draft === true || session.isDraft === true) return true;
  const id = session.id == null ? '' : String(session.id);
  return id.startsWith('draft:') || Boolean(session.draft_client_id || session.draftClientId);
}

export function isReadOnlySessionForCurrentUser(
  session: AgentOwnedSession | null | undefined,
  currentUserId: string | number | null | undefined,
): boolean {
  if (!session) return false;
  if (isDraftHumanChatSession(session)) return false;
  if (session.read_only === true || session.readOnly === true) return true;
  if (session.is_pending_session_lookup === true || session.isPendingSessionLookup === true) return true;
  if (isA2ASession(session)) return true;
  if (session.is_current_user_session === true || session.isCurrentUserSession === true) return false;
  const ownerUserId = session.user_id ?? session.userId;
  return ownerUserId != null && currentUserId != null && String(ownerUserId) !== String(currentUserId);
}

export function shouldUseWritableSessionSurface(
  session: AgentOwnedSession | null | undefined,
  currentUserId: string | number | null | undefined,
): boolean {
  return !isReadOnlySessionForCurrentUser(session, currentUserId);
}

export function shouldPreserveActiveSessionForRequestedId(
  session: AgentOwnedSession | null | undefined,
  requestedSessionId: string | number | null | undefined,
): boolean {
  if (!session || requestedSessionId == null) return false;
  if (session.id == null || String(session.id) !== String(requestedSessionId)) return false;
  if (isDraftHumanChatSession(session)) return false;
  return session.is_pending_session_lookup !== true && session.isPendingSessionLookup !== true;
}

export function resolvePendingSessionLookup<T extends AgentOwnedSession>(
  session: T | null,
  requestedSessionId: string,
): T | null {
  if (!session || String(session.id) !== requestedSessionId) return session;
  if (session.is_pending_session_lookup !== true && session.isPendingSessionLookup !== true) return session;
  return { ...session, is_pending_session_lookup: false, isPendingSessionLookup: false };
}

export function sessionBelongsToAgent(session: AgentOwnedSession | null | undefined, agentId: string | null | undefined): boolean {
  if (!session || !agentId) return false;
  const sessionAgentId = session.agent_id ?? session.agentId;
  const peerAgentId = session.peer_agent_id ?? session.peerAgentId;
  if (isA2ASession(session) && peerAgentId != null && String(peerAgentId) === String(agentId)) return true;
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
      uiStates: { ...uiStates, [key]: uiForPhase('starting') },
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
      [key]: currentUi?.isStreaming ? currentUi : uiForPhase('resuming'),
    },
  };
}

export function shouldIgnoreObservedActiveRun(input: {
  key: string;
  run?: Partial<SessionRunState> & { run_id?: string | null } | null;
  terminalRunIds: Set<string>;
  terminalSessionKeys: Set<string>;
}): boolean {
  if (input.terminalSessionKeys.has(input.key)) return true;
  const runId = normalizeSessionRunId(input.run?.runId ?? input.run?.run_id);
  return Boolean(runId && [...input.terminalRunIds].some((terminalRunId) => (
    normalizeSessionRunId(terminalRunId) === runId
  )));
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

export function hasLocalSessionRuntimeState(
  activeRuns: Record<string, SessionRunState>,
  uiStates: Record<string, SessionUiState>,
  key: string,
): boolean {
  const ui = uiStates[key];
  return Boolean(key && (activeRuns[key] || ui?.isWaiting || ui?.isStreaming));
}

export function activeRunPollInterval(
  observedActiveRun: { status?: string | null } | null | undefined,
  hasLocalActiveRuntime: boolean,
): number | false {
  const status = String(observedActiveRun?.status || '').toLowerCase();
  return hasLocalActiveRuntime || status === 'pending' || status === 'running' ? 3000 : false;
}

/**
 * The live projection must reconcile the authoritative HTTP transcript when the
 * server's active-run read says the run is gone while local state still shows
 * it active. A lost live terminal tail (at-least-once relay) is otherwise
 * undetectable: no later frame exists to reveal a sequence gap, so the durable
 * read is the only terminal witness.
 */
export function shouldReconcileTranscriptOnActiveRunAbsence(input: {
  observedActiveRun?: { status?: string | null } | null;
  hasLocalActiveRuntime: boolean;
}): boolean {
  if (input.observedActiveRun === undefined) return false;
  if (!input.hasLocalActiveRuntime) return false;
  const run = input.observedActiveRun;
  if (!run) return true;
  return !['pending', 'running'].includes(String(run.status || '').toLowerCase());
}

/**
 * Containment seam for fire-and-forget terminal transcript reconciliation.
 * Both reconcile triggers (the projector's live terminal frame and the
 * authoritative active-run-absence observation) run the transcript backfill
 * without awaiting it; a rejected REST page must be reported through
 * ``onFailure`` instead of escaping as an unhandled rejection. The seam
 * latches nothing: the in-flight cursor cleanup inside the backfill keeps a
 * later terminal signal or observation free to retry. Same contained-failure
 * shape as ``backfillVisibleSessionOnRefocus`` in useSessionTransportController.
 */
export function reconcileSessionTranscriptSafely(
  reconcile: () => unknown | Promise<unknown>,
  onFailure: (error: unknown) => void,
): void {
  void Promise.resolve(reconcile()).catch(onFailure);
}

export function isTerminalRealtimeChatEvent(payload: any): boolean {
  const eventType = String(payload?.event_type || payload?.type || '').trim();
  return [
    'assistant_message',
    'done',
    'error',
    'quota_exceeded',
    'run_cancelled',
    'run_completed',
    'run.needs_reconciliation',
  ].includes(eventType);
}

function normalizedUserContent(message: AgentChatMessage): string {
  return String(message.content || '').replace(/\s+/g, ' ').trim();
}

function hasMaterialUserDisplay(message: AgentChatMessage): boolean {
  return Boolean(
    normalizedUserContent(message)
      || String(message.fileName || '').trim()
      || String(message.imageUrl || '').trim(),
  );
}

function userMessageHasIdentity(message: AgentChatMessage, identity: string): boolean {
  return message.role === 'user'
    && [message.id, message.messageId, message.transcriptEventId]
      .some((candidate) => String(candidate || '') === identity);
}

function hasMatchingDurableUserMessage(messages: AgentChatMessage[], pending: PendingUserMessage): boolean {
  const pendingId = String(pending.message.id || '').trim();
  // Identity alone is not visible proof. Live replay may first expose a
  // lifecycle-only HumanInput placeholder whose payload has no display bytes;
  // dropping the optimistic accepted input at that point makes the user's
  // prompt disappear until full hydration. Only a material durable projection
  // may retire the optimistic display bytes.
  if (pendingId && messages.some((message) => (
    userMessageHasIdentity(message, pendingId) && hasMaterialUserDisplay(message)
  ))) return true;
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
    const pendingId = String(item.message.id || '').trim();
    const placeholderIndex = pendingId
      ? merged.findIndex((message) => (
        userMessageHasIdentity(message, pendingId) && !hasMaterialUserDisplay(message)
      ))
      : -1;
    if (placeholderIndex >= 0) {
      const durablePlaceholder = merged[placeholderIndex];
      merged[placeholderIndex] = {
        ...item.message,
        ...durablePlaceholder,
        content: item.message.content,
        fileName: item.message.fileName || durablePlaceholder.fileName,
        imageUrl: item.message.imageUrl || durablePlaceholder.imageUrl,
      };
      return;
    }
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
    ui: uiForPhase('idle'),
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

export function isStreamingAssistantMessage(message: AgentChatMessage | undefined): boolean {
  return Boolean(message && message.role === 'assistant' && (message as any)._streaming);
}

function joinProcessNotes(existing: string | undefined, next: string): string {
  const current = existing || '';
  if (!current.trim()) return next;
  if (!next.trim()) return current;
  const separator = current.endsWith('\n') || next.startsWith('\n') ? '' : '\n';
  return `${current}${separator}${next}`;
}

function sealStreamingAssistantStep(message: AgentChatMessage): AgentChatMessage {
  const next = { ...(message as any) };
  const visibleText = String(next.content || '');
  if (visibleText.trim()) {
    next.thinking = joinProcessNotes(next.thinking, visibleText);
    next.content = '';
  }
  delete next._streaming;
  return next as AgentChatMessage;
}

function sealStreamingAssistantBeforeProcessMessage(
  messages: AgentChatMessage[],
  options: { dropEmpty?: boolean } = {},
): AgentChatMessage[] {
  const dropEmpty = options.dropEmpty === true;
  const last = messages[messages.length - 1];
  if (!isStreamingAssistantMessage(last)) return messages;
  if (String(last.content || '').trim() || String(last.thinking || '').trim()) {
    return [...messages.slice(0, -1), sealStreamingAssistantStep(last)];
  }
  return dropEmpty ? messages.slice(0, -1) : messages;
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
  const base = sealStreamingAssistantBeforeProcessMessage(messages, { dropEmpty: terminalCard });

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
  const thinkingFromParts = extractThinkingFromParts(event?.parts);
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
    thinking: (last && last.role === 'assistant' && (last as any)._streaming)
      ? (last.thinking || thinkingFromParts)
      : thinkingFromParts,
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

function inlineContentReplacementFromEnvelope(envelope: Record<string, unknown> | null): string | undefined {
  const replacement = envelope?.content_replacement;
  if (!replacement || typeof replacement !== 'object' || Array.isArray(replacement)) return undefined;
  const inlineContent = (replacement as Record<string, unknown>).inline_content;
  return typeof inlineContent === 'string' && inlineContent.trim() ? inlineContent : undefined;
}

function toolResultValueFromEnvelope(envelope: Record<string, unknown> | null): unknown {
  if (!envelope) return '';
  if (Object.prototype.hasOwnProperty.call(envelope, 'result')) {
    const result = envelope.result;
    if (typeof result !== 'string' || result.trim()) {
      return result;
    }
  }
  return inlineContentReplacementFromEnvelope(envelope) ?? '';
}

const TERMINAL_TRANSCRIPT_EVENT_TYPES = new Set(['assistant_message', 'run_completed', 'done', 'error', 'quota_exceeded']);

export function getTerminalRunIdFromTranscriptEvent(event: ChatTranscriptEventPayload): string | null {
  const runId = typeof event.run_id === 'string' ? normalizeSessionRunId(event.run_id) : '';
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
      result: toolResultValueFromEnvelope(envelope),
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
  const threadItem = normalizeThreadItemPayload(event);
  const canonicalThreadItem = event.schema === 'hive.thread_item.v1' ? threadItem : null;
  const eventType = threadItem?.event_type || transcriptEventType(event);
  const timestamp = event.created_at || event.timestamp;
  const content = event.content || '';
  const pendingSessionPermissions = state.pendingSessionPermissions || [];
  const nextPhase = reduceRuntimePhase(state.ui.phase, { ...event, type: eventType });

  if (eventType === 'phase') {
    // Backend first-class phase signal: updates the state machine, adds no message.
    return {
      messages: canonicalThreadItem?.item_type === 'boundary'
        ? [...state.messages, threadItemToAgentChatMessage(canonicalThreadItem)]
        : state.messages,
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  if (eventType === 'run_started') {
    return {
      messages: canonicalThreadItem?.item_type === 'boundary'
        ? [...state.messages, threadItemToAgentChatMessage(canonicalThreadItem)]
        : state.messages,
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  if (eventType === 'run_completed' || eventType === 'done') {
    return {
      messages: canonicalThreadItem?.item_type === 'boundary'
        ? [...state.messages, threadItemToAgentChatMessage(canonicalThreadItem)]
        : state.messages,
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  if (eventType === 'session_permission_decision' || eventType === 'permission_resolved') {
    const requestId = permissionDecisionRequestId(event, content);
    const nextPendingSessionPermissions = requestId
      ? resolveSessionPermissionQueue(pendingSessionPermissions, requestId)
      : pendingSessionPermissions;
    const resolvedMessages = requestId
      ? renderSessionPermissionQueue(
          removeSessionPermissionMessage(state.messages, requestId),
          nextPendingSessionPermissions,
        )
      : state.messages;
    return {
      messages: canonicalThreadItem?.item_type === 'approval_decision'
        ? [...resolvedMessages, threadItemToAgentChatMessage(canonicalThreadItem)]
        : resolvedMessages,
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions: nextPendingSessionPermissions,
    };
  }

  if (eventType === 'thinking') {
    return {
      messages: applyStreamingChunkEvent(state.messages, { type: 'chunk', content: '' }).map((message, index, arr) => {
        if (index !== arr.length - 1 || message.role !== 'assistant') return message;
        return { ...message, thinking: `${message.thinking || ''}${content}`, timestamp, threadItem: threadItem || undefined } as AgentChatMessage;
      }),
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  if (eventType === 'assistant_delta' || eventType === 'chunk') {
    return {
      messages: applyStreamingChunkEvent(state.messages, { type: 'chunk', content }).map((message, index, arr) => {
        if (index !== arr.length - 1 || message.role !== 'assistant') return message;
        return { ...message, timestamp, threadItem: threadItem || undefined } as AgentChatMessage;
      }),
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  if (eventType !== 'artifact_delivery') {
    const eventDisplayContent =
      (typeof event.metadata?.message === 'string' && event.metadata.message) ||
      (typeof event.metadata?.display_content === 'string' && event.metadata.display_content) ||
      content;
    const runtimeEvent = getRuntimeEventMessage({
      ...(event.metadata || {}),
      ...event,
      content: eventDisplayContent,
      message: eventDisplayContent,
      type: eventType,
      timestamp,
    });
    if (runtimeEvent) {
      const pendingRequestId = sessionPermissionRequestId(runtimeEvent);
      if (runtimeEvent.eventStatus === 'session_permission_required' && pendingRequestId) {
        const nextPendingSessionPermissions = upsertSessionPermissionQueue(pendingSessionPermissions, runtimeEvent);
        return {
          messages: renderSessionPermissionQueue(
            sealStreamingAssistantBeforeProcessMessage(state.messages),
            nextPendingSessionPermissions,
          ),
          seenEventIds,
          ui: uiForPhase('awaiting_approval'),
          pendingSessionPermissions: nextPendingSessionPermissions,
        };
      }
      return {
        messages: [
          ...sealStreamingAssistantBeforeProcessMessage(state.messages),
          runtimeEvent,
        ],
        seenEventIds,
        ui: uiForPhase(nextPhase),
        pendingSessionPermissions,
      };
    }
  }

  if (eventType === 'user_message' || event.role === 'user') {
    const identity = messageIdentityFromTranscriptEvent(event);
    const displayContent = typeof event.metadata?.display_content === 'string' && event.metadata.display_content
      ? event.metadata.display_content
      : content;
    const previousMessages = identity.messageId && displayContent
      ? state.messages.filter((message) => !userMessageHasIdentity(message, identity.messageId!))
      : state.messages;
    return {
      messages: [
        ...previousMessages,
        {
          role: 'user',
          content: displayContent,
          timestamp,
          threadItem: threadItem || undefined,
          ...identity,
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
	          ? { ...message, timestamp, threadItem: threadItem || undefined, ...messageIdentityFromTranscriptEvent(event) }
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
	      threadItem: threadItem || undefined,
	      ...messageIdentityFromTranscriptEvent(event),
	    };
    return {
      messages: appendToolCallMessage(state.messages, toolMessage),
      seenEventIds,
      ui: isBlockingToolMessage(toolMessage)
        ? uiForPhase('awaiting_approval')
        : uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  if (eventType === 'response_repair') {
    const originalMessageId = String(event.metadata?.original_message_id || event.message_id || '').trim();
    let replaced = false;
    const messages = state.messages.map((message) => {
      if (!originalMessageId || (message.messageId !== originalMessageId && message.id !== originalMessageId)) {
        return message;
      }
      replaced = true;
      return {
        ...message,
        role: 'assistant' as const,
        content,
        timestamp,
        threadItem: threadItem || message.threadItem,
        ...messageIdentityFromTranscriptEvent(event),
      };
    });
    if (!replaced) {
      messages.push({
        role: 'assistant',
        content,
        timestamp,
        threadItem: threadItem || undefined,
        ...messageIdentityFromTranscriptEvent(event),
      });
    }
    return {
      messages,
      seenEventIds,
      ui: uiForPhase(nextPhase),
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
	          ? { ...message, timestamp, threadItem: threadItem || undefined, ...messageIdentityFromTranscriptEvent(event) }
	          : message
	      )),
      seenEventIds,
      ui: uiForPhase(nextPhase),
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
        messages: renderSessionPermissionQueue(
          sealStreamingAssistantBeforeProcessMessage(state.messages),
          nextPendingSessionPermissions,
        ),
        seenEventIds,
        ui: uiForPhase('awaiting_approval'),
        pendingSessionPermissions: nextPendingSessionPermissions,
      };
    }
    return {
      messages: [
        ...sealStreamingAssistantBeforeProcessMessage(state.messages),
        runtimeEvent,
      ],
      seenEventIds,
      ui: uiForPhase(nextPhase),
      pendingSessionPermissions,
    };
  }

  return { ...state, seenEventIds, ui: uiForPhase(nextPhase) };
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
  'agent_task_notification',
  'subagent',
  'team_member',
  'schedule',
  'schedule_fire',
  'goal',
  'once',
  'memory_candidate',
  'artifact_update',
  'artifact_delivery',
  'file_changes',
  'runtime_action_started',
  'runtime_action_progress',
  'runtime_action_completed',
  'runtime_action_blocked',
  'runtime_action_failed',
  'memory_context_degraded',
  'memory_context_unavailable',
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
    ownerAgentId: typeof part.ownerAgentId === 'string' ? part.ownerAgentId : (typeof part.owner_agent_id === 'string' ? part.owner_agent_id : undefined),
    sourceAgentId: typeof part.sourceAgentId === 'string' ? part.sourceAgentId : (typeof part.source_agent_id === 'string' ? part.source_agent_id : undefined),
    downloadAgentId: typeof part.downloadAgentId === 'string' ? part.downloadAgentId : (typeof part.download_agent_id === 'string' ? part.download_agent_id : undefined),
    deliveryAgentId: typeof part.deliveryAgentId === 'string' ? part.deliveryAgentId : (typeof part.delivery_agent_id === 'string' ? part.delivery_agent_id : undefined),
    sourceArtifactId: typeof part.sourceArtifactId === 'string' ? part.sourceArtifactId : (typeof part.source_artifact_id === 'string' ? part.source_artifact_id : undefined),
    producerAgentId: typeof part.producerAgentId === 'string' ? part.producerAgentId : (typeof part.producer_agent_id === 'string' ? part.producer_agent_id : undefined),
    sourceSessionId: typeof part.sourceSessionId === 'string' ? part.sourceSessionId : (typeof part.source_session_id === 'string' ? part.source_session_id : undefined),
    rootSessionId: typeof part.rootSessionId === 'string' ? part.rootSessionId : (typeof part.root_session_id === 'string' ? part.root_session_id : undefined),
    ownerAgentName: typeof part.ownerAgentName === 'string' ? part.ownerAgentName : (typeof part.owner_agent_name === 'string' ? part.owner_agent_name : undefined),
    sourceAgentName: typeof part.sourceAgentName === 'string' ? part.sourceAgentName : (typeof part.source_agent_name === 'string' ? part.source_agent_name : undefined),
    downloadAgentName: typeof part.downloadAgentName === 'string' ? part.downloadAgentName : (typeof part.download_agent_name === 'string' ? part.download_agent_name : undefined),
    deliveryAgentName: typeof part.deliveryAgentName === 'string' ? part.deliveryAgentName : (typeof part.delivery_agent_name === 'string' ? part.delivery_agent_name : undefined),
    revisionId: typeof part.revisionId === 'string' ? part.revisionId : (typeof part.revision_id === 'string' ? part.revision_id : undefined),
    action: typeof part.action === 'string' ? part.action : undefined,
    toolCallId: typeof part.toolCallId === 'string' ? part.toolCallId : (typeof part.tool_call_id === 'string' ? part.tool_call_id : undefined),
    diffSummary: typeof part.diffSummary === 'string' ? part.diffSummary : (typeof part.diff_summary === 'string' ? part.diff_summary : undefined),
    contentHash: typeof part.contentHash === 'string' ? part.contentHash : (typeof part.content_hash === 'string' ? part.content_hash : undefined),
    snapshotHash: typeof part.snapshotHash === 'string' ? part.snapshotHash : (typeof part.snapshot_hash === 'string' ? part.snapshot_hash : undefined),
    snapshotStoragePath: typeof part.snapshotStoragePath === 'string'
      ? part.snapshotStoragePath
      : (typeof part.snapshot_storage_path === 'string' ? part.snapshot_storage_path : undefined),
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

function extractThinkingFromParts(parts: unknown): string | undefined {
  if (!Array.isArray(parts)) return undefined;
  const chunks = parts
    .filter((part: any) => part?.type === 'reasoning' || part?.type === 'thinking')
    .map((part: any) => (
      typeof part?.text === 'string' ? part.text : typeof part?.content === 'string' ? part.content : ''
    ))
    .filter((text: string) => text.trim().length > 0);
  return chunks.length > 0 ? chunks.join('') : undefined;
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
      label: backendModel.label || activeModel?.label || activeModel?.model,
      provider: backendModel.provider || activeModel?.provider,
      name: backendModel.name || activeModel?.model,
      fallback_name: backendModel.fallback_name ?? null,
      route_reason: backendModel.route_reason ?? null,
      routing_config_source: backendModel.routing_config_source ?? null,
      routing_locked: backendModel.routing_locked ?? false,
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

const OPAQUE_MODEL_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function buildComposerRuntimePresentation(runtimeSummary: ChatRuntimeSummary | null): {
  modelLabel: string | null;
  contextUsedPercent: number | null;
} {
  const rawModelLabel = [runtimeSummary?.model?.label, runtimeSummary?.model?.name]
    .find((value): value is string => typeof value === 'string' && value.trim().length > 0)
    ?.trim();
  const modelLabel = rawModelLabel && !OPAQUE_MODEL_ID_RE.test(rawModelLabel) ? rawModelLabel : null;

  const used = runtimeSummary?.runtime?.estimated_input_tokens;
  const remaining = runtimeSummary?.runtime?.remaining_tokens_estimate;
  const contextWindow = runtimeSummary?.model?.context_window_tokens;
  const total =
    typeof contextWindow === 'number' && contextWindow > 0
      ? contextWindow
      : typeof used === 'number' && typeof remaining === 'number'
        ? used + remaining
        : null;
  const contextUsedPercent =
    typeof used === 'number' && total && total > 0
      ? Math.max(0, Math.min(100, Math.round((used / total) * 100)))
      : null;

  return { modelLabel, contextUsedPercent };
}

export function getTransportNotice(payload: any): string | null {
  if (payload?.type !== 'info') return null;
  const text = payload?.content || payload?.message;
  return typeof text === 'string' && text.trim() ? text : null;
}

export function getRuntimeEventMessage(payload: any): AgentChatMessage | null {
  const eventType = payload?.eventType || payload?.event_type || payload?.type;
  const item = normalizeThreadItemPayload(payload);
  if (!item) return null;
  if (payload?.schema === 'hive.thread_item.v1') {
    const specialized = new Set(['user_message', 'agent_message', 'reasoning', 'tool_call', 'tool_result']);
    return specialized.has(item.item_type) ? null : threadItemToAgentChatMessage(item);
  }
  return isRuntimeEventType(eventType) ? threadItemToAgentChatMessage(item) : null;
}

export function normalizeRuntimeEventMessage(payload: any): AgentChatMessage | null {
  if (normalizeThreadItemPayload(payload?.threadItem)) return payload as AgentChatMessage;
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
	      ...messageIdentityFromStoredPayload(payload),
	    };
  }

  return {
    role: payload?.role === 'assistant' ? 'assistant' : 'user',
    content: payload?.content || '',
    artifacts: artifacts.length > 0 ? artifacts : undefined,
    thinking: payload?.thinking || extractThinkingFromParts(payload?.parts),
	    timestamp: payload?.created_at || payload?.timestamp,
	    sender_name: payload?.sender_name,
	    participant_id: payload?.participant_id,
	    id: payload?.id,
	    ...messageIdentityFromStoredPayload(payload),
	  };
}
