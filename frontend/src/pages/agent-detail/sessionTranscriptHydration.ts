import type { ChatSession } from '../../api/domains/chat';
import {
  createEmptyTranscriptReplayState,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
} from './chatRuntime';
import { mergeTranscriptBackfill } from './chatTransportRecovery';
import { applySessionActiveProjection } from './agentDetailPolicy';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';

export const CANONICAL_TRANSCRIPT_PAGE_SIZE = 1000;

export type CanonicalTranscriptPageRequest = {
  beforeSequence?: number;
  direction: 'backward';
  limit: number;
  schemaVersion: 2;
};

export type CanonicalTranscriptHydrationState = {
  complete: boolean;
  pageCount: number;
};

type TranscriptPageFetcher = (
  request: CanonicalTranscriptPageRequest,
) => Promise<ChatTranscriptEventPayload[]>;

type TranscriptSnapshotConsumer = (
  events: ChatTranscriptEventPayload[],
  state: CanonicalTranscriptHydrationState,
) => void | Promise<void>;

function eventSequence(event: ChatTranscriptEventPayload): number {
  const sequence = Number(event.sequence ?? 0);
  if (!Number.isSafeInteger(sequence) || sequence <= 0) {
    throw new Error('session_transcript_event_missing_sequence');
  }
  return sequence;
}

export function projectCanonicalTranscriptSnapshot(options: {
  existing: ChatTranscriptEventPayload[];
  snapshot: ChatTranscriptEventPayload[];
  session: ChatSession;
  parseMessage: (message: AgentChatMessage) => AgentChatMessage;
}) {
  const events = mergeTranscriptBackfill(options.existing, options.snapshot);
  const hydration = hydrateSessionTranscriptEvents(events);
  const activeProjection = applySessionActiveProjection(
    options.session,
    hydration.messages.map(options.parseMessage),
  );
  return {
    events,
    store: hydration.store,
    ui: hydration.ui,
    messages: activeProjection.messages,
    replay: {
      ...createEmptyTranscriptReplayState(),
      messages: activeProjection.messages,
      ui: hydration.ui,
    },
    activeProjection,
  };
}

/**
 * Load complete authorized Session V2 evidence newest-first. The first page is
 * published immediately, then older pages are merged and published
 * automatically. Pagination remains an API transport detail and never becomes
 * a user-operated visibility boundary or a blank-screen prerequisite.
 */
export async function loadCanonicalSessionTranscript(
  fetchPage: TranscriptPageFetcher,
  onSnapshot?: TranscriptSnapshotConsumer,
): Promise<ChatTranscriptEventPayload[]> {
  const eventsById = new Map<string, ChatTranscriptEventPayload>();
  let beforeSequence: number | undefined;
  let previousOldestSequence: number | undefined;
  let pageCount = 0;

  while (true) {
    const page = await fetchPage({
      ...(beforeSequence == null ? {} : { beforeSequence }),
      direction: 'backward',
      limit: CANONICAL_TRANSCRIPT_PAGE_SIZE,
      schemaVersion: 2,
    });
    pageCount += 1;
    for (const event of page) {
      const sequence = eventSequence(event);
      const eventId = String((event as unknown as Record<string, unknown>).event_id || event.id || `${sequence}`);
      eventsById.set(eventId, event);
    }

    const snapshot = [...eventsById.values()]
      .sort((left, right) => eventSequence(left) - eventSequence(right));
    const complete = page.length < CANONICAL_TRANSCRIPT_PAGE_SIZE;
    if (pageCount === 1 || complete) {
      await onSnapshot?.(snapshot, { complete, pageCount });
    }
    if (complete) return snapshot;

    const oldestSequence = Math.min(...page.map(eventSequence));
    if (previousOldestSequence != null && oldestSequence >= previousOldestSequence) {
      throw new Error('session_transcript_pagination_stalled');
    }
    previousOldestSequence = oldestSequence;
    beforeSequence = oldestSequence;
  }
}
