import {
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
  extractArtifactParts,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type SessionUiState,
} from './chatRuntime';
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

export type ConsumedSessionEnvelope = {
  store: SessionEventStore | undefined;
  projectionEvent: ChatTranscriptEventPayload;
  sessionEnvelope: boolean;
  canonical: boolean;
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

function compatibilityPayload(event: ChatTranscriptEventPayload): Record<string, unknown> {
  const envelope = event as unknown as Record<string, unknown>;
  return recordValue(envelope.payload) || {};
}

function compatibilityLegacyRunId(event: ChatTranscriptEventPayload): string | null {
  const payloadRunId = compatibilityPayload(event).legacy_run_id;
  const runId = typeof payloadRunId === 'string' ? payloadRunId : event.run_id;
  return typeof runId === 'string' && runId.trim() ? runId.trim() : null;
}

function compatibilityLegacyEventType(event: ChatTranscriptEventPayload): string {
  const envelope = event as unknown as Record<string, unknown>;
  const eventType = envelope.legacy_event_type ?? event.event_type ?? event.type;
  return typeof eventType === 'string' ? eventType : '';
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
      transcriptEventId: item.id,
      timestamp: item.occurredAt,
      sessionItem: item,
    };
  }
  if (item.kind === 'assistant_final') {
    return {
      role: 'assistant',
      content: finalDisplayContent(item, store),
      id: item.renderOwnerId || item.id,
      transcriptEventId: item.id,
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
    return {
      role: 'tool_call',
      content: '',
      id: item.invocationId || item.id,
      transcriptEventId: item.id,
      timestamp: item.occurredAt,
      toolName,
      toolArgs,
      toolStatus: pairedResult || item.terminal ? 'done' : 'running',
      toolResult,
      eventType: item.kind,
      eventStatus: item.lifecycle,
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

export function hydrateSessionTranscriptEvents(
  events: ChatTranscriptEventPayload[],
  baselineSequence = 0,
): { store: SessionEventStore | undefined; messages: AgentChatMessage[]; ui: SessionUiState } {
  let store: SessionEventStore | undefined;
  let compatibilityReplay = createEmptyTranscriptReplayState();
  const sequenceByIdentity = new Map<string, number>();
  const legacyAssistantRunByIdentity = new Map<string, string>();

  for (const event of events) {
    const sequence = Number(event.sequence ?? 0);
    const consumed = consumeSessionEnvelope(event, store, baselineSequence);
    if (consumed.store) store = consumed.store;
    if (!consumed.canonical) {
      compatibilityReplay = applyTranscriptEvent(compatibilityReplay, consumed.projectionEvent);
      const legacyAssistantRunId = compatibilityLegacyEventType(event) === 'assistant_message'
        ? compatibilityLegacyRunId(event)
        : null;
      for (const message of compatibilityReplay.messages) {
        for (const identity of messageIdentities(message)) {
          // A compatibility lifecycle event can replay the entire accumulated
          // message list. Only the event that first materialized a message owns
          // its timeline position; later phase/tool events must not drag old
          // messages to the newest turn.
          const firstMaterialization = !sequenceByIdentity.has(identity);
          if (firstMaterialization) sequenceByIdentity.set(identity, sequence);
          if (firstMaterialization && legacyAssistantRunId) {
            legacyAssistantRunByIdentity.set(identity, legacyAssistantRunId);
          }
        }
      }
    }
  }

  const canonicalMessages = store ? projectSessionEventStoreToMessages(store) : [];
  const canonicalFinalRunIds = new Set(
    Object.values(store?.items || {})
      .filter((item) => item.kind === 'assistant_final' && item.terminal)
      .map((item) => ('run_id' in item.scope ? item.scope.run_id : null))
      .filter((runId): runId is string => Boolean(runId)),
  );
  const compatibilityMessages = compatibilityReplay.messages.filter((message) => {
    const legacyRunId = messageIdentities(message)
      .map((identity) => legacyAssistantRunByIdentity.get(identity))
      .find((runId): runId is string => Boolean(runId));
    // During rolling migration the legacy ChatMessage projection and the
    // canonical assistant_final can both exist. The typed run binding—not text
    // similarity—makes the canonical final the sole render owner.
    return !legacyRunId || !canonicalFinalRunIds.has(legacyRunId);
  });
  const ordered = [...compatibilityMessages, ...canonicalMessages]
    .map((message, index) => ({
      message,
      index,
      sequence: message.sessionItem?.first_sequence
        ?? sequenceByIdentity.get(String(message.transcriptEventId || message.messageId || message.id || ''))
        ?? Number.MAX_SAFE_INTEGER,
    }))
    .sort((left, right) => left.sequence - right.sequence || left.index - right.index)
    .map(({ message }) => message);

  return { store, messages: ordered, ui: compatibilityReplay.ui };
}

function transcriptSuffixBaseline(events: ChatTranscriptEventPayload[]): number {
  const sequences = events
    .map((event) => Number(event.sequence ?? 0))
    .filter((sequence) => Number.isSafeInteger(sequence) && sequence > 0);
  return sequences.length > 0 ? Math.max(0, Math.min(...sequences) - 1) : 0;
}

export function projectCanonicalSessionSnapshot(
  event: ChatTranscriptEventPayload,
  store: SessionEventStore,
  transcriptEvents?: ChatTranscriptEventPayload[],
): {
  messages: AgentChatMessage[];
  projectMessages: boolean;
  terminal: boolean;
  runTerminal: boolean;
  runId: string | null;
} {
  const envelope = event as unknown as SessionEventV2;
  const runTerminal = envelope.item_kind === 'run'
    && ['completed', 'failed', 'cancelled'].includes(envelope.lifecycle);
  const terminalMetadataOnly = (
    envelope.item_kind === 'turn'
    && ['completed', 'failed', 'cancelled'].includes(envelope.lifecycle)
  ) || (
    envelope.item_kind === 'run_outcome'
    && envelope.lifecycle === 'terminal_committed'
  );
  // A live tail can start after older V1/compatibility events. At a terminal
  // boundary, use the same complete replay projection as reload so the final
  // answer seals (rather than erases) the process disclosure. The first
  // locally retained durable sequence is the truthful baseline for this
  // presentation suffix; it is not a second event authority.
  const messages = runTerminal && transcriptEvents?.length
    ? hydrateSessionTranscriptEvents(
      transcriptEvents,
      transcriptSuffixBaseline(transcriptEvents),
    ).messages
    : projectSessionEventStoreToMessages(store);
  return {
    messages,
    projectMessages: !terminalMetadataOnly,
    terminal: runTerminal,
    runTerminal,
    runId: runTerminal && envelope.scope.level !== 'session' && envelope.scope.level !== 'turn'
      ? envelope.scope.run_id
      : null,
  };
}

export function applyCanonicalSessionSnapshot(options: {
  event: ChatTranscriptEventPayload;
  store: SessionEventStore;
  transcriptEvents?: ChatTranscriptEventPayload[];
  active: boolean;
  onTranscript: () => void;
  onActivity: () => void;
  onTerminal: (runId: string | null) => void;
  onMessages: (messages: AgentChatMessage[], terminal: boolean) => void;
}): void {
  const snapshot = projectCanonicalSessionSnapshot(options.event, options.store, options.transcriptEvents);
  options.onTranscript();
  options.onActivity();
  if (snapshot.runTerminal) options.onTerminal(snapshot.runId);
  if (options.active && snapshot.projectMessages) {
    options.onMessages(snapshot.messages, snapshot.terminal);
  }
}

export function consumeSessionEnvelope(
  event: ChatTranscriptEventPayload,
  previousStore: SessionEventStore | undefined,
  baselineSequence: number,
): ConsumedSessionEnvelope {
  const envelope = event as unknown as Record<string, unknown>;
  if (envelope.schema === 'hive.session_event' && envelope.schema_version === 2) {
    return {
      store: reduceSessionEvent(
        previousStore || createSessionEventStore(baselineSequence),
        event as unknown as SessionEventV2,
      ),
      projectionEvent: event,
      sessionEnvelope: true,
      canonical: true,
    };
  }
  if (envelope.schema !== 'hive.session_event_compatibility' || envelope.schema_version !== 1) {
    return { store: previousStore, projectionEvent: event, sessionEnvelope: false, canonical: false };
  }

  const payload = envelope.payload && typeof envelope.payload === 'object'
    ? envelope.payload as Record<string, unknown>
    : {};
  return {
    store: reduceSessionCompatibilityEvent(
      previousStore || createSessionEventStore(baselineSequence),
      event as unknown as SessionCompatibilityEvent,
    ),
    projectionEvent: {
      ...event,
      id: String(envelope.event_id || ''),
      event_type: String(envelope.legacy_event_type || ''),
      ...(typeof payload.legacy_run_id === 'string' && payload.legacy_run_id.trim()
        ? { run_id: payload.legacy_run_id.trim() }
        : {}),
      content: sessionPayloadContent(payload),
      parts: Array.isArray(payload.parts) ? payload.parts as Array<Record<string, unknown>> : [],
      metadata: payload.metadata && typeof payload.metadata === 'object'
        ? payload.metadata as Record<string, unknown>
        : {},
    },
    sessionEnvelope: true,
    canonical: false,
  };
}
