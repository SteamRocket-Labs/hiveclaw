import {
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
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
    const pairedResult = item.kind === 'tool_call'
      ? toolResultByCall.get(item.id) || (item.invocationId ? toolResultByCall.get(item.invocationId) : undefined)
      : item;
    const toolName = typeof item.payload.tool_name === 'string'
      ? item.payload.tool_name
      : typeof item.payload.name === 'string' ? item.payload.name : undefined;
    const toolResult = typeof pairedResult?.payload.result === 'string'
      ? pairedResult.payload.result
      : pairedResult ? itemDisplayContent(pairedResult) : undefined;
    const rawToolArgs = item.payload.args ?? item.payload.arguments ?? item.payload.input;
    const toolArgs = rawToolArgs && typeof rawToolArgs === 'object' && !Array.isArray(rawToolArgs)
      ? rawToolArgs as Record<string, unknown>
      : undefined;
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
): { store: SessionEventStore | undefined; messages: AgentChatMessage[]; ui: SessionUiState } {
  let store: SessionEventStore | undefined;
  let compatibilityReplay = createEmptyTranscriptReplayState();
  const sequenceByIdentity = new Map<string, number>();

  for (const event of events) {
    const sequence = Number(event.sequence ?? 0);
    const consumed = consumeSessionEnvelope(event, store, 0);
    if (consumed.store) store = consumed.store;
    if (!consumed.canonical) {
      compatibilityReplay = applyTranscriptEvent(compatibilityReplay, consumed.projectionEvent);
      for (const message of compatibilityReplay.messages) {
        for (const identity of [message.transcriptEventId, message.messageId, message.id]) {
          if (identity) sequenceByIdentity.set(String(identity), sequence);
        }
      }
    }
  }

  const canonicalMessages = store ? projectSessionEventStoreToMessages(store) : [];
  const ordered = [...compatibilityReplay.messages, ...canonicalMessages]
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

export function projectCanonicalSessionSnapshot(
  event: ChatTranscriptEventPayload,
  store: SessionEventStore,
): { messages: AgentChatMessage[]; terminal: boolean; runId: string | null } {
  const envelope = event as unknown as SessionEventV2;
  const terminal = envelope.item_kind === 'run'
    && ['completed', 'failed', 'cancelled'].includes(envelope.lifecycle);
  return {
    messages: projectSessionEventStoreToMessages(store),
    terminal,
    runId: terminal && envelope.scope.level !== 'session' && envelope.scope.level !== 'turn'
      ? envelope.scope.run_id
      : null,
  };
}

export function applyCanonicalSessionSnapshot(options: {
  event: ChatTranscriptEventPayload;
  store: SessionEventStore;
  active: boolean;
  onTranscript: () => void;
  onActivity: () => void;
  onTerminal: (runId: string | null) => void;
  onMessages: (messages: AgentChatMessage[], terminal: boolean) => void;
}): void {
  const snapshot = projectCanonicalSessionSnapshot(options.event, options.store);
  options.onTranscript();
  options.onActivity();
  if (snapshot.terminal) options.onTerminal(snapshot.runId);
  if (options.active) options.onMessages(snapshot.messages, snapshot.terminal);
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
