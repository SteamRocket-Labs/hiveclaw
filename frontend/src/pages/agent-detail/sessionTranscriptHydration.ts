import type { ChatSession } from '../../api/domains/chat';
import {
  createEmptyTranscriptReplayState,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
} from './chatRuntime';
import { mergeTranscriptBackfill } from './chatTransportRecovery';
import { applySessionActiveProjection } from './agentDetailPolicy';
import { hydrateSessionTranscriptEvents } from './sessionEventConsumer';
import type { SessionEventStore } from '../session-workbench/sessionEventStore';

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

function projectionBaselineSequence(events: ChatTranscriptEventPayload[]): number {
  const firstSequence = events.reduce<number | undefined>((lowest, event) => {
    const sequence = Number(event.sequence ?? 0);
    if (!Number.isSafeInteger(sequence) || sequence <= 0) return lowest;
    return lowest == null || sequence < lowest ? sequence : lowest;
  }, undefined);
  return firstSequence == null ? 0 : firstSequence - 1;
}

/**
 * Return the durable watermark that is safe for a live Session subscription.
 *
 * A newest-first transcript page is already a truthful, contiguous tail even
 * while older pages continue loading. It must not block realtime delivery.
 * Actual gaps, stale projections, and forced full recovery remain hard stops.
 */
export function liveSubscriptionWatermark(store: SessionEventStore | undefined): number | null {
  if (!store || store.recoveryRequired === 'full_hydration') return null;
  if (store.projection.phase === 'gap_detected' || store.projection.phase === 'stale') return null;
  const sequence = Number(store.highestContiguousSequence);
  return Number.isSafeInteger(sequence) && sequence > 0 ? sequence : null;
}

export function projectCanonicalTranscriptSnapshot(options: {
  existing: ChatTranscriptEventPayload[];
  snapshot: ChatTranscriptEventPayload[];
  session: ChatSession;
  parseMessage: (message: AgentChatMessage) => AgentChatMessage;
}) {
  const events = mergeTranscriptBackfill(options.existing, options.snapshot);
  // Transcript hydration intentionally publishes the newest durable page
  // before older history has finished loading. Treat the omitted prefix as a
  // projection baseline so the latest page and subsequent live events can be
  // reduced immediately. A real gap inside the fetched suffix remains a gap.
  const hydration = hydrateSessionTranscriptEvents(events, projectionBaselineSequence(events));
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
