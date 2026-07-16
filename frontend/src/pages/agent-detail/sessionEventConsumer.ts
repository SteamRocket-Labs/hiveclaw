import type { ChatTranscriptEventPayload } from './chatRuntime';
import {
  createSessionEventStore,
  reduceSessionCompatibilityEvent,
  reduceSessionEvent,
  type SessionCompatibilityEvent,
  type SessionEventStore,
  type SessionEventV2,
} from '../session-workbench/sessionEventStore';

export type ConsumedSessionEnvelope = {
  store: SessionEventStore | undefined;
  projectionEvent: ChatTranscriptEventPayload;
  sessionEnvelope: boolean;
};

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
    };
  }
  if (envelope.schema !== 'hive.session_event_compatibility' || envelope.schema_version !== 1) {
    return { store: previousStore, projectionEvent: event, sessionEnvelope: false };
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
      content: typeof payload.content === 'string' ? payload.content : '',
      parts: Array.isArray(payload.parts) ? payload.parts as Array<Record<string, unknown>> : [],
      metadata: payload.metadata && typeof payload.metadata === 'object'
        ? payload.metadata as Record<string, unknown>
        : {},
    },
    sessionEnvelope: true,
  };
}
