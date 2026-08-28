import {
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
  getTerminalRunIdFromTranscriptEvent,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type PendingUserMessage,
  type RuntimePhase,
  type SessionUiState,
  type TranscriptReplayState,
} from './chatRuntime';
import { latestTranscriptSequence, mergeTranscriptBackfill } from './chatTransportRecovery';
import {
  applyCanonicalSessionSnapshot,
  compatibilityProjectionEvent,
  consumeSessionEnvelope,
  mergeCanonicalTerminalMessages,
  type SessionTranscriptApplication,
} from './sessionEventConsumer';
import type { SessionCompatibilityEvent, SessionEventStore } from '../session-workbench/sessionEventStore';

/**
 * The AgentDetail per-session transcript applier — the single production owner
 * of live transcript consumption for canonical, compatibility, and raw legacy
 * frames. Every session-scoped ref it mutates and every message commit it
 * performs is declared here so the real path stays behaviorally testable
 * without a test-only mirror.
 */
export type SessionTranscriptApplierRefs = {
  transcriptEvents: Record<string, ChatTranscriptEventPayload[] | undefined>;
  eventStores: Record<string, SessionEventStore | undefined>;
  fullHydrationKeys: Set<string>;
  replayStates: Record<string, TranscriptReplayState | undefined>;
  uiStates: Record<string, SessionUiState | undefined>;
  runtimeActivityAt: Record<string, number | undefined>;
  pendingUserMessages: Record<string, PendingUserMessage[] | undefined>;
};

export type SessionTranscriptApplierDeps = {
  refs: SessionTranscriptApplierRefs;
  markActiveRunTerminal: (key: string, runId?: string | null) => boolean;
  isTerminalTranscriptToolMessage: (message: AgentChatMessage | undefined) => boolean;
  mergePendingMessages: (key: string, messages: AgentChatMessage[]) => AgentChatMessage[];
  setChatMessagesSessionId: (sessionId: string) => void;
  enqueueChatMessagesUpdate: (sessionId: string, updater: (messages: AgentChatMessage[]) => AgentChatMessage[]) => void;
  setChatMessagesAfterQueued: (sessionId: string, updater: (messages: AgentChatMessage[]) => AgentChatMessage[]) => void;
  setActivePhase: (phase: RuntimePhase) => void;
  setIsWaiting: (value: boolean) => void;
  setIsStreaming: (value: boolean) => void;
  parseChatMsg: (message: AgentChatMessage) => AgentChatMessage;
};

function isLegacyTerminalEventType(eventType: string | undefined): boolean {
  return eventType === 'assistant_message'
    || eventType === 'run_completed'
    || eventType === 'done'
    || eventType === 'error'
    || eventType === 'quota_exceeded';
}

/** Apply one legacy-plane transcript event: replay state, transcript backfill,
 * UI state, and (for the active runtime) the message commit. Returns whether
 * the event was newly applied to the legacy projection. */
function applyLegacyProjectionEvent(
  deps: SessionTranscriptApplierDeps,
  key: string,
  sessionId: string,
  projectionEvent: ChatTranscriptEventPayload,
  isActiveRuntime: boolean,
): boolean {
  const { refs } = deps;
  const currentEvents = refs.transcriptEvents[key] || [];
  const sequenceAlreadyApplied = typeof projectionEvent.sequence === 'number'
    && projectionEvent.sequence > 0
    && currentEvents.some((candidate) => candidate.sequence === projectionEvent.sequence);
  if (sequenceAlreadyApplied) {
    refs.transcriptEvents[key] = mergeTranscriptBackfill(currentEvents, [projectionEvent]);
    return false;
  }
  const previous = refs.replayStates[key] || createEmptyTranscriptReplayState();
  const next = applyTranscriptEvent(previous, projectionEvent);
  if (next === previous) return false;
  refs.replayStates[key] = next;
  refs.transcriptEvents[key] = mergeTranscriptBackfill(
    refs.transcriptEvents[key] || [],
    [projectionEvent],
  );
  refs.uiStates[key] = next.ui;
  refs.runtimeActivityAt[key] = Date.now();

  const eventType = projectionEvent.event_type || projectionEvent.type;
  const lastMessage = next.messages[next.messages.length - 1];
  const terminal = isLegacyTerminalEventType(eventType) || deps.isTerminalTranscriptToolMessage(lastMessage);
  if (terminal) {
    deps.markActiveRunTerminal(key, getTerminalRunIdFromTranscriptEvent(projectionEvent));
  }

  if (isActiveRuntime) {
    deps.setChatMessagesSessionId(sessionId);
    const commitChatMessages = terminal
      ? deps.setChatMessagesAfterQueued
      : deps.enqueueChatMessagesUpdate;
    commitChatMessages(sessionId, () => deps.mergePendingMessages(key, next.messages.map(deps.parseChatMsg)));
    deps.setActivePhase(next.ui.phase);
    deps.setIsWaiting(next.ui.isWaiting);
    deps.setIsStreaming(next.ui.isStreaming);
  }
  return true;
}

function applyDrainedCompatibilityEvents(
  deps: SessionTranscriptApplierDeps,
  key: string,
  sessionId: string,
  drained: SessionCompatibilityEvent[],
  isActiveRuntime: boolean,
): void {
  // Drained compatibility envelopes were buffered behind a gap; now that the
  // cursor reached them they own exactly one legacy projection each, in
  // sequence order.
  for (const drainedEvent of drained) {
    applyLegacyProjectionEvent(
      deps,
      key,
      sessionId,
      compatibilityProjectionEvent(drainedEvent),
      isActiveRuntime,
    );
  }
}

/**
 * Consume one live transcript event for a session. Returns this transition's
 * application facts for canonical/compatibility envelopes (null/false when
 * nothing was newly applied), or the legacy boolean for raw legacy frames.
 */
export function applyTranscriptToSessionRuntime(
  deps: SessionTranscriptApplierDeps,
  agentId: string,
  sessionId: string,
  event: ChatTranscriptEventPayload,
  isActiveRuntime: boolean,
): SessionTranscriptApplication | boolean | null {
  const key = `${agentId}:${sessionId}`;
  const { refs } = deps;
  const existingEvents = refs.transcriptEvents[key] || [];
  let consumed: ReturnType<typeof consumeSessionEnvelope>;
  try {
    consumed = consumeSessionEnvelope(
      event,
      refs.eventStores[key],
      refs.fullHydrationKeys.has(key) ? 0 : latestTranscriptSequence(existingEvents),
    );
  } catch (error) {
    console.warn(`[SessionEventV2] Rejected invalid envelope for ${key}:`, error);
    return false;
  }
  if (consumed.store) refs.eventStores[key] = consumed.store;
  const envelopeApplication = consumed.application ?? false;
  const application = consumed.application;

  // Canonical snapshots apply for every applied canonical event, whatever the
  // carrier: a compatibility envelope can fill a sequence gap and drain
  // buffered canonical events — terminal semantics and the transcript merge
  // then use the drained applied events, never only the carrier. Buffered-only,
  // conflicted, ignored, and duplicate arrivals apply nothing (zero early side
  // effects).
  const appliedCanonicalEvents = application?.canonicalEvents || [];
  if (consumed.store && appliedCanonicalEvents.length > 0) {
    applyCanonicalSessionSnapshot({
      events: appliedCanonicalEvents,
      store: consumed.store,
      active: isActiveRuntime,
      onTranscript: () => {
        refs.transcriptEvents[key] = mergeTranscriptBackfill(
          refs.transcriptEvents[key] || [],
          appliedCanonicalEvents as unknown as ChatTranscriptEventPayload[],
        );
      },
      onActivity: () => { refs.runtimeActivityAt[key] = Date.now(); },
      onTerminal: (runId) => deps.markActiveRunTerminal(key, runId),
      onMessages: (messages, terminal, runId) => {
        deps.setChatMessagesSessionId(sessionId);
        (terminal ? deps.setChatMessagesAfterQueued : deps.enqueueChatMessagesUpdate)(
          sessionId,
          (previous) => deps.mergePendingMessages(
            key,
            terminal ? mergeCanonicalTerminalMessages(previous, messages, runId) : messages,
          ),
        );
      },
    });
  }

  if (consumed.canonical) {
    applyDrainedCompatibilityEvents(
      deps, key, sessionId, application?.compatibilityEvents || [], isActiveRuntime,
    );
    return envelopeApplication;
  }

  if (consumed.sessionEnvelope) {
    // Compatibility carrier: the legacy projection runs ONLY when the carrier
    // itself was applied contiguously. Buffered, conflicted, duplicate, and
    // recovery-held arrivals mutate zero UI/transcript state — the content
    // renders when the missing sequence closes the gap.
    if (!application) return false;
    applyDrainedCompatibilityEvents(deps, key, sessionId, application.compatibilityEvents, isActiveRuntime);
    applyLegacyProjectionEvent(deps, key, sessionId, consumed.projectionEvent, isActiveRuntime);
    return application;
  }

  // Raw non-envelope legacy frames keep the boolean contract.
  return applyLegacyProjectionEvent(deps, key, sessionId, consumed.projectionEvent, isActiveRuntime);
}
