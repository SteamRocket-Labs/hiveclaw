import {
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
  extractArtifactParts,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type SessionUiState,
} from './chatRuntime';
import { normalizeToolCallResult } from './toolResultEnvelope';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import { trimMessagesBeforeTranscriptEvent } from './agentDetailPolicy';
import type { SessionMessageStore } from './sessionMessageStore';
import { runtimeFailurePresentation } from '../session-workbench/runtimeFailurePresentation';
import {
  createSessionEventStore,
  reduceSessionCompatibilityEvent,
  reduceSessionEvent,
  sessionPayloadContent,
  type SessionCompatibilityEvent,
  type SessionEventStore,
  type SessionItemV2,
  type SessionEventV2,
} from '../session-workbench/sessionEventStore';

export type SessionTranscriptApplication = {
  /** Canonical events applied to the contiguous item projection in this
   * transition, in sequence order — the only events whose projector side
   * effects may run now. */
  canonicalEvents: SessionEventV2[];
  /** Compatibility envelopes applied to the contiguous delivery cursor in
   * this transition, in sequence order (drained buffered envelopes only —
   * an applied carrier delivers through its own projection path). Each one
   * owns exactly one legacy projection and one terminal-effect pass. */
  compatibilityEvents: SessionCompatibilityEvent[];
  /** True when a compatibility carrier itself advanced the contiguous cursor
   * in this transition (canonical events it drained appear in
   * canonicalEvents). */
  compatibilityApplied: boolean;
};

export type ConsumedSessionEnvelope = {
  store: SessionEventStore | undefined;
  projectionEvent: ChatTranscriptEventPayload;
  sessionEnvelope: boolean;
  canonical: boolean;
  /** This transition's application facts, or null when the carrier was only
   * buffered, conflicted, ignored, duplicated, or rejected. A store identity
   * change alone is NOT application. */
  application: SessionTranscriptApplication | null;
};

const ASSISTANT_ITEM_KINDS = new Set([
  'assistant_text',
  'assistant_commentary',
  'assistant_reasoning_summary',
  'assistant_reasoning_private',
  'assistant_final',
  'assistant_plan',
]);

function itemDisplayContent(item: SessionItemV2): string {
  return item.content || item.summary || item.display?.summary || '';
}

function finalDisplayContent(item: SessionItemV2, store: SessionEventStore): string {
  if (item.content) return item.content;
  return (item.source_blocks || [])
    .map((source) => store.items[source.item_id]?.content || '')
    .join('');
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function messageIdentities(message: AgentChatMessage): string[] {
  return [message.transcriptEventId, message.messageId, message.id]
    .filter((identity): identity is string => typeof identity === 'string' && Boolean(identity));
}

/**
 * Compatibility-plane message timeline bookkeeping — the single owner shared
 * by hydration and the live applier. Records, for every message a projected
 * compatibility event materialized, the sequence of the event that FIRST
 * materialized it and (for legacy assistant_message materializations) the
 * typed legacy run binding used by the canonical-final supersede rule.
 */
export type CompatibilityMessageTimeline = {
  sequenceByIdentity: Map<string, number>;
  legacyAssistantRunByIdentity: Map<string, string>;
  /** Identities first materialized by a terminal witness the run-identity
   * registry REJECTED (a stale old-run terminal while another run is
   * active). The evidence stays durable in transcript/replay/timeline, but
   * these identities never enter the active visible composition. Replaced
   * wholesale with the timeline on every authoritative hydration publish. */
  excludedIdentities: Set<string>;
};

export function createCompatibilityMessageTimeline(): CompatibilityMessageTimeline {
  return { sequenceByIdentity: new Map(), legacyAssistantRunByIdentity: new Map(), excludedIdentities: new Set() };
}

/** Returns the identities this event materialized for the FIRST time. */
export function recordCompatibilityTimelineMessages(
  timeline: CompatibilityMessageTimeline,
  projectionEvent: ChatTranscriptEventPayload,
  messages: AgentChatMessage[],
): string[] {
  const sequence = Number(projectionEvent.sequence ?? 0);
  const eventType = projectionEvent.event_type || projectionEvent.type;
  const runId = typeof projectionEvent.run_id === 'string' && projectionEvent.run_id.trim()
    ? projectionEvent.run_id.trim()
    : null;
  const legacyAssistantRunId = eventType === 'assistant_message' ? runId : null;
  const firstMaterialized: string[] = [];
  for (const message of messages) {
    for (const identity of messageIdentities(message)) {
      // A compatibility lifecycle event can replay the entire accumulated
      // message list. Only the event that first materialized a message owns
      // its timeline position; later phase/tool events must not drag old
      // messages to the newest turn.
      if (timeline.sequenceByIdentity.has(identity)) continue;
      timeline.sequenceByIdentity.set(identity, sequence);
      firstMaterialized.push(identity);
      if (legacyAssistantRunId) timeline.legacyAssistantRunByIdentity.set(identity, legacyAssistantRunId);
    }
  }
  return firstMaterialized;
}

/**
 * Deterministically seed timeline identities for a stored-message fallback
 * baseline (the empty-canonical-transcript path): every stored message gets
 * a pre-live sequence (…, -1, 0) so the first live canonical event always
 * composes AFTER the legacy history, in original stored order.
 */
export function seedCompatibilityTimelineIdentities(
  timeline: CompatibilityMessageTimeline,
  messages: AgentChatMessage[],
  lastSequence = 0,
): void {
  const firstSequence = lastSequence - messages.length + 1;
  messages.forEach((message, index) => {
    for (const identity of messageIdentities(message)) {
      if (!timeline.sequenceByIdentity.has(identity)) {
        timeline.sequenceByIdentity.set(identity, firstSequence + index);
      }
    }
  });
}

/**
 * Mechanical rewind visibility boundary — exact projection identity state.
 * `hiddenIdentities` is the identity set of the messages the rewind trim
 * removed at install time; later live events carry new identities and stay
 * visible. Replaced on every hydration publish and rewind command install.
 */
export type SessionVisibilityBoundary = {
  checkpointEventId: string;
  hiddenIdentities: ReadonlySet<string>;
} | null;

/** Build the boundary from a rewind trim (the trimmed list is always a prefix
 * of the full list — trimMessagesBeforeTranscriptEvent's contract). */
export function buildSessionVisibilityBoundary(
  checkpointEventId: string,
  fullMessages: AgentChatMessage[],
  trimmedMessages: AgentChatMessage[],
): SessionVisibilityBoundary {
  const hidden = trimmedMessages.length < fullMessages.length
    ? fullMessages.slice(trimmedMessages.length)
    : [];
  return { checkpointEventId, hiddenIdentities: new Set(hidden.flatMap(messageIdentities)) };
}

/**
 * Compose a live rewind command install with the prior boundary: every
 * identity hidden by an earlier accepted rewind stays hidden and the newest
 * checkpoint binds. Only full authoritative hydration may REPLACE a
 * boundary (it uses buildSessionVisibilityBoundary directly).
 */
export function composeSessionVisibilityBoundary(
  previous: SessionVisibilityBoundary,
  checkpointEventId: string,
  fullMessages: AgentChatMessage[],
  trimmedMessages: AgentChatMessage[],
): SessionVisibilityBoundary {
  const installed = buildSessionVisibilityBoundary(checkpointEventId, fullMessages, trimmedMessages);
  return {
    checkpointEventId,
    hiddenIdentities: new Set([
      ...(previous?.hiddenIdentities || []),
      ...(installed?.hiddenIdentities || []),
    ]),
  };
}

/**
 * The single live rewind command install path. The boundary is ALWAYS
 * derived from the current session surface visible list — never from the
 * legacy replay baseline, because canonical live messages never enter that
 * baseline. Returns the composed boundary and the trimmed visible list.
 */
export function installRewindVisibilityBoundary(options: {
  previous: SessionVisibilityBoundary;
  checkpointEventId: string;
  visibleMessages: AgentChatMessage[];
  checkpointCreatedAt?: string;
}): { boundary: SessionVisibilityBoundary; trimmedVisibleMessages: AgentChatMessage[] } {
  const trimmedVisibleMessages = trimMessagesBeforeTranscriptEvent(
    options.visibleMessages,
    options.checkpointEventId,
    options.checkpointCreatedAt,
  );
  return {
    boundary: composeSessionVisibilityBoundary(
      options.previous,
      options.checkpointEventId,
      options.visibleMessages,
      trimmedVisibleMessages,
    ),
    trimmedVisibleMessages,
  };
}

/**
 * The atomic chat-surface rewind install: `updateAfterQueued` synchronously
 * flushes queued live updates first, so the boundary AND the trim derive
 * from the same current list — a message queued while the command request
 * was in flight can never be trimmed from the UI while missing from the
 * hidden identity set.
 */
export function installRewindVisibilityBoundaryFromStore(options: {
  store: SessionMessageStore;
  sessionId: string;
  previous: SessionVisibilityBoundary;
  checkpointEventId: string;
  marker: AgentChatMessage;
  checkpointCreatedAt?: string;
}): SessionVisibilityBoundary {
  let boundary: SessionVisibilityBoundary = options.previous;
  options.store.updateAfterQueued(options.sessionId, (current) => {
    const install = installRewindVisibilityBoundary({
      previous: options.previous,
      checkpointEventId: options.checkpointEventId,
      visibleMessages: current,
      ...(options.checkpointCreatedAt ? { checkpointCreatedAt: options.checkpointCreatedAt } : {}),
    });
    boundary = install.boundary;
    return [...install.trimmedVisibleMessages, options.marker];
  });
  return boundary;
}

/** Apply a rewind visibility boundary to a freshly composed visible list. */
export function applySessionVisibilityBoundary(
  messages: AgentChatMessage[],
  boundary: SessionVisibilityBoundary,
): AgentChatMessage[] {
  if (!boundary || boundary.hiddenIdentities.size === 0) return messages;
  return messages.filter(
    (message) => !messageIdentities(message).some((identity) => boundary.hiddenIdentities.has(identity)),
  );
}

function persistedToolEnvelope(payload: Record<string, unknown>): Record<string, unknown> {
  const direct = recordValue(payload);
  let content: Record<string, unknown> | undefined;
  if (typeof payload.content === 'string' && payload.content.trim()) {
    try {
      content = recordValue(JSON.parse(payload.content));
    } catch {
      content = undefined;
    }
  }
  const parts = Array.isArray(payload.parts) ? payload.parts : [];
  const toolPart = parts
    .map(recordValue)
    .find((part) => part?.type === 'tool_call');
  const metadata = recordValue(payload.metadata);
  return {
    ...(metadata || {}),
    ...(toolPart || {}),
    ...(content || {}),
    ...(direct || {}),
  };
}

function projectCanonicalItem(
  item: SessionItemV2,
  store: SessionEventStore,
  toolResultByCall: ReadonlyMap<string, SessionItemV2>,
): AgentChatMessage | null {
  if (item.kind === 'assistant_reasoning_private' && item.visibility.audience === 'private_provider') {
    return {
      role: 'event',
      content: '',
      id: item.id,
      timestamp: item.occurredAt,
      eventType: item.kind,
      eventTitle: item.display?.title || 'Private reasoning',
      eventStatus: item.lifecycle,
      sessionItem: item,
    };
  }
  if (item.kind === 'human_input') {
    return {
      role: 'user',
      content: itemDisplayContent(item),
      id: item.id,
      // Item identity and the durable checkpoint anchor are distinct: the
      // branch/rewind API needs an actual ChatTranscriptEvent id, never the
      // input item id.
      transcriptEventId: item.checkpointEventId ?? null,
      timestamp: item.occurredAt,
      sessionItem: item,
    };
  }
  if (item.kind === 'assistant_final') {
    return {
      role: 'assistant',
      content: finalDisplayContent(item, store),
      id: item.renderOwnerId || item.id,
      // Branch/regenerate anchor on the actual completed event id (zero-copy
      // finals included); the item id is not a transcript event id.
      transcriptEventId: item.completedEventId ?? null,
      timestamp: item.occurredAt,
      artifacts: extractArtifactParts(item.payload),
      sessionItem: item,
    };
  }
  if (ASSISTANT_ITEM_KINDS.has(item.kind)) {
    return {
      role: 'assistant',
      content: itemDisplayContent(item),
      id: item.id,
      transcriptEventId: item.id,
      timestamp: item.occurredAt,
      eventType: item.kind,
      eventStatus: item.lifecycle,
      sessionItem: item,
    };
  }
  if (item.kind === 'tool_call' || item.kind === 'tool_result') {
    const toolEnvelope = persistedToolEnvelope(item.payload);
    const pairedResult = item.kind === 'tool_call'
      ? toolResultByCall.get(item.id) || (item.invocationId ? toolResultByCall.get(item.invocationId) : undefined)
      : item;
    const pairedEnvelope = pairedResult ? persistedToolEnvelope(pairedResult.payload) : undefined;
    const toolName = typeof toolEnvelope.tool_name === 'string'
      ? toolEnvelope.tool_name
      : typeof toolEnvelope.name === 'string' ? toolEnvelope.name : undefined;
    const toolResult = typeof pairedEnvelope?.result === 'string'
      ? pairedEnvelope.result
      : pairedResult ? itemDisplayContent(pairedResult) : undefined;
    const toolArgs = recordValue(toolEnvelope.args ?? toolEnvelope.arguments ?? toolEnvelope.input);
    // The canonical projection feeds live socket delivery, terminal reconcile
    // backfill, and initial hydration alike. Structured tool cards (hr_preview,
    // plan proposals, clarification, …) are render-ready only after the shared
    // envelope normalizer runs; a reload-only parseChatMsg pass left the
    // reconciled tail as a meta-less row the chat surface renders as nothing.
    const normalized = normalizeToolCallResult(toolName, toolResult);
    return {
      role: 'tool_call',
      content: '',
      id: item.invocationId || item.id,
      transcriptEventId: item.id,
      timestamp: item.occurredAt,
      toolName,
      toolArgs,
      toolStatus: pairedResult || item.terminal ? 'done' : 'running',
      toolResult: normalized.displayResult,
      toolRawResult: normalized.raw,
      toolMeta: normalized.toolMeta,
      eventType: item.kind,
      eventStatus: item.lifecycle,
      sessionItem: item,
    };
  }
  if (item.kind === 'runtime_failure') {
    // Canonical thread-item projection for the provider-failure terminal
    // witness: a user-visible error card built from typed failure fields
    // (failure_code / terminal_reason / typed replay-safety) and the safe
    // humanized payload message — never an assistant message and never
    // natural-language scanning.  AgentChatSection renders msg.threadItem
    // directly, so this survives the legacy-subset normalization seam.
    const failureCode = typeof item.payload.failure_code === 'string' ? item.payload.failure_code : null;
    const failureReason = typeof item.payload.terminal_reason === 'string' ? item.payload.terminal_reason : null;
    const failureRetryable = item.payload.retryable === true;
    const failureMessage = runtimeFailurePresentation(failureCode, failureRetryable).fallback;
    const scope = item.scope;
    const threadItem: ThreadItem = {
      schema: 'hive.thread_item.v1',
      schema_version: 1,
      id: item.id,
      sequence: item.first_sequence,
      session_id: scope.session_id,
      thread_id: scope.thread_id,
      turn_id: 'turn_id' in scope ? scope.turn_id : null,
      run_id: 'run_id' in scope ? scope.run_id : null,
      item_status: 'failed',
      actor_type: typeof item.actor?.type === 'string' ? item.actor.type : 'runtime',
      event_type: item.kind,
      type: item.kind,
      role: 'system',
      visibility_scope: item.visibility?.audience === 'operator' ? 'operator' : 'direct_user',
      listed_surface: 'chat',
      content: failureMessage,
      audience: 'user',
      user_summary: failureMessage,
      item_type: 'error',
      item_data: {
        code: failureCode,
        reason: failureReason,
        retryable: failureRetryable,
        retry_reason: null,
      },
    };
    return {
      role: 'event',
      content: failureMessage,
      id: item.id,
      transcriptEventId: item.id,
      timestamp: item.occurredAt,
      eventType: item.kind,
      eventTitle: item.display?.title || item.kind,
      eventStatus: item.lifecycle,
      eventReason: failureReason ?? undefined,
      eventRetryable: failureRetryable,
      threadItem,
      sessionItem: item,
    };
  }
  return {
    role: 'event',
    content: itemDisplayContent(item),
    id: item.id,
    transcriptEventId: item.id,
    timestamp: item.occurredAt,
    eventType: item.kind,
    eventTitle: item.display?.title || item.kind,
    eventStatus: item.lifecycle,
    eventReason: typeof item.payload.reason === 'string' ? item.payload.reason : undefined,
    eventRetryable: typeof item.payload.retryable === 'boolean' ? item.payload.retryable : undefined,
    sessionItem: item,
  };
}

/**
 * Compatibility messages are a render projection of the canonical item store.
 * They are never reduced from the raw event a second time.
 */
export function projectSessionEventStoreToMessages(store: SessionEventStore): AgentChatMessage[] {
  const terminalFinalSources = new Set(
    Object.values(store.items)
      .filter((item) => item.kind === 'assistant_final' && item.terminal)
      .flatMap((item) => (item.source_blocks || []).map((source) => source.item_id)),
  );
  const toolCallIds = new Set(
    Object.values(store.items).filter((item) => item.kind === 'tool_call').map((item) => item.id),
  );
  const toolCallInvocations = new Set(
    Object.values(store.items)
      .filter((item) => item.kind === 'tool_call' && item.invocationId)
      .map((item) => item.invocationId as string),
  );
  const toolResultByCall = new Map<string, SessionItemV2>();
  Object.values(store.items).filter((item) => item.kind === 'tool_result').forEach((item) => {
    if (item.parentItemId) toolResultByCall.set(item.parentItemId, item);
    if (item.invocationId) toolResultByCall.set(item.invocationId, item);
  });
  return Object.values(store.items)
    .sort((left, right) => left.first_sequence - right.first_sequence || left.id.localeCompare(right.id))
    .filter((item) => !(item.kind === 'assistant_text' && terminalFinalSources.has(item.id)))
    .filter((item) => item.kind !== 'tool_result'
      || !((item.parentItemId && toolCallIds.has(item.parentItemId))
        || (item.invocationId && toolCallInvocations.has(item.invocationId))))
    .map((item) => projectCanonicalItem(item, store, toolResultByCall))
    .filter((message): message is AgentChatMessage => message !== null);
}

/**
 * The single owner of the final visible mixed-plane composition: the union of
 * the compatibility replay messages and the canonical store projection,
 * identity-deduped (canonical projection wins a shared identity) and ordered
 * by authoritative event/item sequence with a stable arrival fallback. Both
 * hydration and the live transcript applier commit through this function —
 * neither plane may replace the whole visible list from its own projection.
 */
export function composeMixedPlaneSessionMessages(options: {
  store: SessionEventStore | undefined;
  compatibilityMessages: AgentChatMessage[];
  timeline: CompatibilityMessageTimeline;
}): AgentChatMessage[] {
  const { store, timeline } = options;
  const canonicalMessages = store ? projectSessionEventStoreToMessages(store) : [];
  const canonicalFinalRunIds = new Set(
    Object.values(store?.items || {})
      .filter((item) => item.kind === 'assistant_final' && item.terminal)
      .map((item) => ('run_id' in item.scope ? item.scope.run_id : null))
      .filter((runId): runId is string => Boolean(runId)),
  );
  const canonicalItemIds = new Set(
    canonicalMessages.map((message) => message.sessionItem?.id).filter((id): id is string => Boolean(id)),
  );
  const canonicalIdentities = new Set(canonicalMessages.flatMap(messageIdentities));
  const compatibilityMessages = options.compatibilityMessages.filter((message) => {
    const identities = messageIdentities(message);
    // Identities a rejected stale terminal first materialized never enter the
    // active composition (their durable evidence stays in the replay).
    if (identities.some((identity) => timeline.excludedIdentities.has(identity))) return false;
    const legacyRunId = identities
      .map((identity) => timeline.legacyAssistantRunByIdentity.get(identity))
      .find((runId): runId is string => Boolean(runId));
    // During rolling migration the legacy ChatMessage projection and the
    // canonical assistant_final can both exist. The typed run binding—not text
    // similarity—makes the canonical final the sole render owner.
    if (legacyRunId && canonicalFinalRunIds.has(legacyRunId)) return false;
    // Cross-plane identity dedupe: a message materialized in the compatibility
    // replay that the canonical projection also owns renders once, from the
    // canonical projection.
    if (message.sessionItem?.id && canonicalItemIds.has(message.sessionItem.id)) return false;
    return !messageIdentities(message).some((identity) => canonicalIdentities.has(identity));
  });
  return [...compatibilityMessages, ...canonicalMessages]
    .map((message, index) => ({
      message,
      index,
      sequence: message.sessionItem?.first_sequence
        ?? timeline.sequenceByIdentity.get(String(message.transcriptEventId || message.messageId || message.id || ''))
        ?? Number.MAX_SAFE_INTEGER,
    }))
    .sort((left, right) => left.sequence - right.sequence || left.index - right.index)
    .map(({ message }) => message);
}

export function hydrateSessionTranscriptEvents(
  events: ChatTranscriptEventPayload[],
  baselineSequence = 0,
): {
  store: SessionEventStore | undefined;
  messages: AgentChatMessage[];
  ui: SessionUiState;
  compatibilityTimeline: CompatibilityMessageTimeline;
} {
  let store: SessionEventStore | undefined;
  let compatibilityReplay = createEmptyTranscriptReplayState();
  let uiReplay = createEmptyTranscriptReplayState();
  const timeline = createCompatibilityMessageTimeline();

  const projectCompatibility = (projectionEvent: ChatTranscriptEventPayload) => {
    compatibilityReplay = applyTranscriptEvent(compatibilityReplay, projectionEvent);
    recordCompatibilityTimelineMessages(timeline, projectionEvent, compatibilityReplay.messages);
  };

  for (const event of events) {
    const consumed = consumeSessionEnvelope(event, store, baselineSequence);
    if (consumed.store) store = consumed.store;
    // Application ledger gate: a session envelope projects only when its
    // transition actually applied it (contiguous cursor advancement).
    // Gap-buffered, consistency-conflicted, duplicate, late, and
    // recovery-held envelopes all report null application and project zero
    // times. Raw non-envelope legacy frames keep their direct projection
    // contract (they carry no application ledger).
    if (consumed.sessionEnvelope && !consumed.application) continue;

    const uiEvents: ChatTranscriptEventPayload[] = [];
    if (!consumed.sessionEnvelope) {
      uiEvents.push(consumed.projectionEvent);
    } else {
      if (!consumed.canonical && consumed.application?.compatibilityApplied) {
        uiEvents.push(consumed.projectionEvent);
      }
      for (const canonical of consumed.application?.canonicalEvents || []) {
        const projection = canonicalRuntimeProjectionEvent(canonical);
        if (projection) uiEvents.push(projection);
      }
      for (const drained of consumed.application?.compatibilityEvents || []) {
        uiEvents.push(compatibilityProjectionEvent(drained));
      }
    }
    for (const uiEvent of uiEvents.sort(
      (left, right) => Number(left.sequence ?? 0) - Number(right.sequence ?? 0),
    )) {
      uiReplay = applyTranscriptEvent(uiReplay, uiEvent);
    }

    if (!consumed.canonical) {
      // Total ascending event sequence: the compatibility carrier (lowest
      // sequence of the transition) projects before its drained buffered
      // envelopes.
      if (!consumed.sessionEnvelope || consumed.application?.compatibilityApplied) {
        projectCompatibility(consumed.projectionEvent);
      }
    }
    // Compatibility envelopes drained by either carrier kind own exactly one
    // projection each, in sequence order; their own later appearances in the
    // input are duplicates and hit the gate above.
    for (const drained of consumed.application?.compatibilityEvents || []) {
      projectCompatibility(compatibilityProjectionEvent(drained));
    }
  }

  return {
    store,
    messages: composeMixedPlaneSessionMessages({
      store,
      compatibilityMessages: compatibilityReplay.messages,
      timeline,
    }),
    ui: uiReplay.ui,
    compatibilityTimeline: timeline,
  };
}

/**
 * Reduce only typed lifecycle facts that own the whole run's visible phase.
 * Native assistant items may complete in the middle of a run, so they never
 * settle the Session. Legacy-adapted assistant terminals remain the explicit
 * compatibility exception because that path has no later run terminal item.
 */
function canonicalRuntimeProjectionEvent(
  event: SessionEventV2,
): ChatTranscriptEventPayload | null {
  let eventType: string | null = null;
  if (event.item_kind === 'run') {
    eventType = event.kind;
  } else if (
    event.item_kind === 'runtime_failure'
    && event.lifecycle === 'recorded'
    && event.scope.level === 'run'
  ) {
    eventType = 'error';
  } else if (isLegacyAssistantTerminalItem(event.item_kind, event.lifecycle, event.payload)) {
    eventType = event.lifecycle === 'completed'
      ? 'assistant_message'
      : event.lifecycle === 'failed'
        ? 'error'
        : 'run_cancelled';
  }
  if (!eventType) return null;
  return {
    id: event.event_id,
    sequence: event.sequence,
    event_type: eventType,
    run_id: event.scope.level === 'run' || event.scope.level === 'round'
      ? event.scope.run_id
      : null,
  };
}

function canonicalMessageRunId(message: AgentChatMessage): string | null {
  const scope = message.sessionItem?.scope;
  if (!scope || scope.level === 'session' || scope.level === 'turn') return null;
  return scope.run_id || null;
}

function isRenderedAssistantAnswer(message: AgentChatMessage): boolean {
  if (message.role !== 'assistant' || !message.content?.trim()) return false;
  if (message.sessionItem) {
    return message.sessionItem.kind === 'assistant_final' && message.sessionItem.terminal;
  }
  if (message.eventType === 'assistant_message' || message.eventType === 'assistant_final') return true;
  if (message.eventType?.startsWith('assistant_')) return false;
  return true;
}

function messagesShareIdentity(left: AgentChatMessage, right: AgentChatMessage): boolean {
  if (left.sessionItem?.id && right.sessionItem?.id && left.sessionItem.id === right.sessionItem.id) return true;
  const rightIdentities = new Set(messageIdentities(right));
  return messageIdentities(left).some((identity) => rightIdentities.has(identity));
}

/**
 * Seal one live run without rebuilding the whole Session from a locally
 * retained transcript array. Live transport and automatic older-history
 * hydration are intentionally independent, so that array may contain a
 * truthful gap while the newest suffix is already current. The visible live
 * process is therefore retained and the canonical assistant_final becomes the
 * single terminal render owner.
 */
export function mergeCanonicalTerminalMessages(
  previous: AgentChatMessage[],
  canonical: AgentChatMessage[],
  terminalRunId: string | null,
): AgentChatMessage[] {
  let latestUserIndex = -1;
  for (let index = previous.length - 1; index >= 0; index -= 1) {
    if (previous[index]?.role === 'user') {
      latestUserIndex = index;
      break;
    }
  }

  const prefix = latestUserIndex >= 0 ? previous.slice(0, latestUserIndex + 1) : [];
  const liveTail = latestUserIndex >= 0 ? previous.slice(latestUserIndex + 1) : previous;
  const canonicalRun = terminalRunId
    ? canonical.filter((message) => (
      // Compatibility-plane messages carry no canonical run binding; a
      // terminal seal bound to one run must not drop them from the union.
      !message.sessionItem || canonicalMessageRunId(message) === terminalRunId
    ))
    : canonical;
  const canonicalFinal = [...canonicalRun].reverse().find(isRenderedAssistantAnswer);
  const liveFinal = [...liveTail].reverse().find(isRenderedAssistantAnswer);
  const process = liveTail.filter((message) => !isRenderedAssistantAnswer(message));

  for (const message of canonicalRun) {
    if (isRenderedAssistantAnswer(message) || message.role === 'user') continue;
    if (process.some((existing) => messagesShareIdentity(existing, message))) continue;
    // A historical compatibility item already sealed in the prefix must not
    // be re-appended to the live process by a later run's terminal merge —
    // identity dedupe across the whole visible list, never text similarity.
    if (prefix.some((existing) => messagesShareIdentity(existing, message))) continue;
    process.push(message);
  }

  const terminalAnswer = [canonicalFinal, liveFinal].find((candidate) => (
    candidate && !prefix.some((existing) => messagesShareIdentity(existing, candidate))
  ));
  return terminalAnswer ? [...prefix, ...process, terminalAnswer] : [...prefix, ...process];
}

function runtimeFailureRunIdOf(envelope: SessionEventV2): string | null {
  // Only a run-scoped runtime_failure with a nonempty authoritative run_id
  // seals the run.  Session/turn/round scopes carry no whole-run terminal
  // authority — no null-id fallback for this event.
  return envelope.scope.level === 'run'
    && typeof (envelope.scope as { run_id?: unknown }).run_id === 'string'
    && ((envelope.scope as { run_id: string }).run_id || '').trim()
    ? (envelope.scope as { run_id: string }).run_id
    : null;
}

const TERMINAL_RUN_LIFECYCLES = new Set(['completed', 'failed', 'cancelled', 'needs_reconciliation']);

/**
 * The web-chat assistant_message finalizer settles the RuntimeTask and its
 * transcript event is the turn's last canonical witness on the live tail —
 * no run.completed item event follows on that path. Legacy-adapted envelopes
 * carry the typed `payload.legacy` marker from the backend serializer, so the
 * terminal assistant item itself is the run-terminal witness. Native V2
 * turns are excluded: their assistant items complete per message mid-run and
 * run terminality is owned by the `run` item lifecycle.
 */
export function isLegacyAssistantTerminalItem(
  itemKind: string,
  lifecycle: string,
  payload: Record<string, unknown>,
): boolean {
  return itemKind.startsWith('assistant_')
    && TERMINAL_RUN_LIFECYCLES.has(lifecycle)
    && payload.legacy === true;
}

function isRunTerminalEvent(envelope: SessionEventV2): boolean {
  return (envelope.item_kind === 'run'
    && TERMINAL_RUN_LIFECYCLES.has(envelope.lifecycle))
    // The canonical runtime_failure terminal event is the run-scoped terminal
    // witness of the web-chat provider-failure path; it seals the run exactly
    // like a run.failed lifecycle item.
    || (envelope.item_kind === 'runtime_failure' && envelope.lifecycle === 'recorded' && runtimeFailureRunIdOf(envelope) !== null)
    // Legacy-adapted assistant terminals are the turn-terminal witness of the
    // web-chat finalizer path — same acceptance set as the projector effects
    // (a null scope run binding keeps the legacy clear-active-run behavior).
    || isLegacyAssistantTerminalItem(
      String(envelope.item_kind || ''),
      String(envelope.lifecycle || ''),
      (envelope.payload && typeof envelope.payload === 'object' ? envelope.payload : {}) as Record<string, unknown>,
    );
}

function isTerminalMetadataOnlyEvent(envelope: SessionEventV2): boolean {
  return (envelope.item_kind === 'turn'
    && ['completed', 'failed', 'cancelled'].includes(envelope.lifecycle))
    || (envelope.item_kind === 'run_outcome'
      && envelope.lifecycle === 'terminal_committed');
}

function runTerminalRunIdOf(envelope: SessionEventV2): string | null {
  return envelope.scope.level !== 'session' && envelope.scope.level !== 'turn'
    ? envelope.scope.run_id
    : null;
}

export function projectCanonicalSessionSnapshot(
  event: ChatTranscriptEventPayload,
  store: SessionEventStore,
): {
  messages: AgentChatMessage[];
  projectMessages: boolean;
  terminal: boolean;
  runTerminal: boolean;
  runId: string | null;
} {
  const envelope = event as unknown as SessionEventV2;
  const runTerminal = isRunTerminalEvent(envelope);
  const messages = projectSessionEventStoreToMessages(store);
  return {
    messages,
    projectMessages: !isTerminalMetadataOnlyEvent(envelope),
    terminal: runTerminal,
    runTerminal,
    runId: runTerminal ? runTerminalRunIdOf(envelope) : null,
  };
}

export function applyCanonicalSessionSnapshot(options: {
  events: SessionEventV2[];
  store: SessionEventStore;
  active: boolean;
  onTranscript: () => void;
  onActivity: () => void;
  onTerminal: (runId: string | null, event: SessionEventV2) => boolean | void;
  onMessages: (messages: AgentChatMessage[], terminal: boolean, runId: string | null) => void;
}): void {
  options.onTranscript();
  options.onActivity();
  // Terminal semantics belong to each applied event, never only the carrier:
  // a gap close drains buffered terminals whose side effects were held back.
  // The stale-run guard stays per event — onTerminal reports acceptance, and
  // a stale old-run terminal may enter the durable projection but never seals
  // or replaces the active newer run's tail. The message merge binds to the
  // LATEST ACCEPTED terminal only: a later rejected stale terminal must never
  // retarget a binding an earlier terminal already earned.
  let anyTerminalAccepted = false;
  let acceptedTerminalRunId: string | null = null;
  for (const event of options.events) {
    if (!isRunTerminalEvent(event)) continue;
    const runId = runTerminalRunIdOf(event);
    if (options.onTerminal(runId, event) === false) continue;
    anyTerminalAccepted = true;
    acceptedTerminalRunId = runId;
  }
  if (options.active && !options.events.every(isTerminalMetadataOnlyEvent)) {
    // A metadata-only terminal event (turn/run_outcome) changes no renderable
    // item; a transition made only of them keeps the message projection.
    options.onMessages(
      projectSessionEventStoreToMessages(options.store),
      anyTerminalAccepted,
      acceptedTerminalRunId,
    );
  }
}

function transitionApplicationOf(
  nextStore: SessionEventStore | undefined,
  previousStore: SessionEventStore | undefined,
): SessionTranscriptApplication | null {
  // A same-identity store return carries a stale lastTransition from an
  // earlier transition — only a changed store owns a fresh report. Empty
  // transitions (duplicate, late, recovery hold, buffered-only, ignored)
  // report no application.
  if (!nextStore || nextStore === previousStore) return null;
  const { appliedEvents, appliedCompatibilityEvents, compatibilityApplied } = nextStore.lastTransition;
  return appliedEvents.length > 0 || appliedCompatibilityEvents.length > 0 || compatibilityApplied
    ? { canonicalEvents: appliedEvents, compatibilityEvents: appliedCompatibilityEvents, compatibilityApplied }
    : null;
}

/** Legacy projection event of a compatibility envelope — the single owner of
 * that mapping for carriers and drained buffered envelopes alike. */
export function compatibilityProjectionEvent(event: SessionCompatibilityEvent): ChatTranscriptEventPayload {
  const envelope = event as unknown as Record<string, unknown>;
  const payload = recordValue(envelope.payload) || {};
  const occurredAt = typeof envelope.occurred_at === 'string' && envelope.occurred_at.trim()
    ? envelope.occurred_at.trim()
    : typeof envelope.created_at === 'string' && envelope.created_at.trim()
      ? envelope.created_at.trim()
      : undefined;
  return {
    ...(event as unknown as ChatTranscriptEventPayload),
    id: String(envelope.event_id || ''),
    event_type: String(envelope.legacy_event_type || ''),
    // Compatibility envelopes use the Session V2 `occurred_at` clock while
    // the legacy projector consumes `created_at`. Preserve that mechanical
    // event timestamp so a reload ends the visible run at the durable
    // assistant terminal instead of the earlier answer snapshot.
    created_at: occurredAt,
    ...(typeof payload.legacy_run_id === 'string' && payload.legacy_run_id.trim()
      ? { run_id: payload.legacy_run_id.trim() }
      : {}),
    content: sessionPayloadContent(payload),
    parts: Array.isArray(payload.parts) ? payload.parts as Array<Record<string, unknown>> : [],
    metadata: recordValue(payload.metadata) || {},
  };
}

export function consumeSessionEnvelope(
  event: ChatTranscriptEventPayload,
  previousStore: SessionEventStore | undefined,
  baselineSequence: number,
): ConsumedSessionEnvelope {
  const envelope = event as unknown as Record<string, unknown>;
  if (envelope.schema === 'hive.session_event' && envelope.schema_version === 2) {
    const store = reduceSessionEvent(
      previousStore || createSessionEventStore(baselineSequence),
      event as unknown as SessionEventV2,
    );
    return {
      store,
      projectionEvent: event,
      sessionEnvelope: true,
      canonical: true,
      application: transitionApplicationOf(store, previousStore),
    };
  }
  if (envelope.schema !== 'hive.session_event_compatibility' || envelope.schema_version !== 1) {
    return { store: previousStore, projectionEvent: event, sessionEnvelope: false, canonical: false, application: null };
  }

  const store = reduceSessionCompatibilityEvent(
    previousStore || createSessionEventStore(baselineSequence),
    event as unknown as SessionCompatibilityEvent,
  );
  return {
    store,
    projectionEvent: compatibilityProjectionEvent(event as unknown as SessionCompatibilityEvent),
    sessionEnvelope: true,
    canonical: false,
    application: transitionApplicationOf(store, previousStore),
  };
}
