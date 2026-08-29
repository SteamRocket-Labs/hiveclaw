import {
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
  getTerminalRunIdFromTranscriptEvent,
  terminalRuntimePhaseForSessionEvent,
  uiForPhase,
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
  applySessionVisibilityBoundary,
  compatibilityProjectionEvent,
  composeMixedPlaneSessionMessages,
  consumeSessionEnvelope,
  createCompatibilityMessageTimeline,
  mergeCanonicalTerminalMessages,
  recordCompatibilityTimelineMessages,
  type CompatibilityMessageTimeline,
  type SessionTranscriptApplication,
  type SessionVisibilityBoundary,
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
  /** Compatibility-plane sequence/run-identity bookkeeping per session —
   * seeded by hydration and extended by every live legacy projection so the
   * mixed-plane composition keeps total ascending event sequence. */
  compatibilityTimelines: Record<string, CompatibilityMessageTimeline | undefined>;
  /** Mechanical rewind visibility boundary per session — replaced on every
   * hydration publish and on every live rewind command install. */
  visibilityBoundaries: Record<string, SessionVisibilityBoundary | undefined>;
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

type LegacyProjectionResult = {
  /** Whether the event was newly applied to the legacy projection. */
  applied: boolean;
  /** Present only for a terminal witness accepted by the active-run registry;
   * null is the typed value of a genuinely unbound compatibility witness. */
  terminalRunId?: string | null;
};

/** Apply one legacy-plane transcript event: replay state, timeline
 * bookkeeping, transcript backfill, and UI state. The visible message commit
 * is NOT performed here — each transition commits the shared mixed-plane
 * composition exactly once at the end. */
function applyLegacyProjectionEvent(
  deps: SessionTranscriptApplierDeps,
  key: string,
  projectionEvent: ChatTranscriptEventPayload,
  isActiveRuntime: boolean,
): LegacyProjectionResult {
  const { refs } = deps;
  const currentEvents = refs.transcriptEvents[key] || [];
  const sequenceAlreadyApplied = typeof projectionEvent.sequence === 'number'
    && projectionEvent.sequence > 0
    && currentEvents.some((candidate) => candidate.sequence === projectionEvent.sequence);
  if (sequenceAlreadyApplied) {
    refs.transcriptEvents[key] = mergeTranscriptBackfill(currentEvents, [projectionEvent]);
    return { applied: false };
  }
  const previous = refs.replayStates[key] || createEmptyTranscriptReplayState();
  const next = applyTranscriptEvent(previous, projectionEvent);
  if (next === previous) return { applied: false };
  const eventType = projectionEvent.event_type || projectionEvent.type;
  const lastMessage = next.messages[next.messages.length - 1];
  const terminal = isLegacyTerminalEventType(eventType) || deps.isTerminalTranscriptToolMessage(lastMessage);
  const terminalRunId = terminal ? getTerminalRunIdFromTranscriptEvent(projectionEvent) : null;
  const terminalAccepted = terminal
    ? deps.markActiveRunTerminal(key, terminalRunId) !== false
    : false;

  if (terminal && !terminalAccepted) {
    // Rejected stale terminal: durable evidence WITHOUT letting the
    // terminal's replay reduction replace or move the active newer stream.
    // The terminal reduces against an empty baseline so its message form is
    // standalone; otherwise-anonymous messages get stamped with the typed
    // event identity; the evidence inserts BEFORE a trailing active
    // streaming assistant so a later delta of the active run continues the
    // same stream. The session UI state keeps its exact pre-event identity
    // and zero active phase/seal effects fire.
    const standalone = applyTranscriptEvent(createEmptyTranscriptReplayState(), projectionEvent);
    const eventIdentity = String(projectionEvent.id || '')
      || (typeof projectionEvent.sequence === 'number' && projectionEvent.sequence > 0 ? `seq:${projectionEvent.sequence}` : '');
    const evidenceMessages = standalone.messages.map((message, index) => (
      message.id || message.messageId || message.transcriptEventId || !eventIdentity
        ? message
        : { ...message, id: `${eventIdentity}:evidence:${index}` }
    ));
    const previousMessages = previous.messages;
    const tail = previousMessages[previousMessages.length - 1];
    const tailIsActiveStream = Boolean(
      tail && tail.role === 'assistant' && (tail as { _streaming?: boolean })._streaming,
    );
    const mergedMessages = tailIsActiveStream
      ? [...previousMessages.slice(0, -1), ...evidenceMessages, tail]
      : [...previousMessages, ...evidenceMessages];
    refs.replayStates[key] = { ...next, messages: mergedMessages, ui: previous.ui };
    const timeline = refs.compatibilityTimelines[key]
      ?? (refs.compatibilityTimelines[key] = createCompatibilityMessageTimeline());
    const firstMaterialized = recordCompatibilityTimelineMessages(timeline, projectionEvent, mergedMessages);
    refs.transcriptEvents[key] = mergeTranscriptBackfill(
      refs.transcriptEvents[key] || [],
      [projectionEvent],
    );
    refs.runtimeActivityAt[key] = Date.now();
    // The identities this rejected terminal first materialized never enter
    // the active composition; the durable evidence stays in the replay.
    for (const identity of firstMaterialized) timeline.excludedIdentities.add(identity);
    return { applied: true };
  }

  // Non-terminal or accepted terminal: the full reduction applies.
  refs.replayStates[key] = next;
  const timeline = refs.compatibilityTimelines[key]
    ?? (refs.compatibilityTimelines[key] = createCompatibilityMessageTimeline());
  recordCompatibilityTimelineMessages(timeline, projectionEvent, next.messages);
  refs.transcriptEvents[key] = mergeTranscriptBackfill(
    refs.transcriptEvents[key] || [],
    [projectionEvent],
  );
  refs.uiStates[key] = next.ui;
  refs.runtimeActivityAt[key] = Date.now();
  if (isActiveRuntime) {
    deps.setActivePhase(next.ui.phase);
    deps.setIsWaiting(next.ui.isWaiting);
    deps.setIsStreaming(next.ui.isStreaming);
  }
  return terminal && terminalAccepted ? { applied: true, terminalRunId } : { applied: true };
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
  const application = consumed.application;

  // This transition's single visible-commit plan. Both planes only mutate
  // their durable stores/replay during application; the visible list is
  // committed once, at the end, from the shared mixed-plane composition —
  // never a plane-specific whole-list last-writer-wins replacement.
  let commitRequested = false;
  let terminalCommit = false;
  // The terminal merge binds to the canonical accepted terminal run when one
  // exists; a legacy-only accepted terminal keeps the legacy unbound seal.
  let canonicalTerminalRunId: string | null = null;
  let canonicalTerminalPhase: RuntimePhase | null = null;
  const syncCanonicalTerminalPhase = () => {
    if (!canonicalTerminalPhase || !isActiveRuntime) return;
    const terminalUi = uiForPhase(canonicalTerminalPhase);
    refs.uiStates[key] = terminalUi;
    deps.setActivePhase(terminalUi.phase);
    deps.setIsWaiting(terminalUi.isWaiting);
    deps.setIsStreaming(terminalUi.isStreaming);
  };

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
      onTerminal: (runId, terminalEvent) => {
        const accepted = deps.markActiveRunTerminal(key, runId);
        if (accepted !== false) {
          canonicalTerminalPhase = terminalRuntimePhaseForSessionEvent(
            terminalEvent.item_kind,
            terminalEvent.lifecycle,
          );
        }
        return accepted;
      },
      onMessages: (_messages, terminal, runId) => {
        commitRequested = true;
        if (terminal) {
          terminalCommit = true;
          canonicalTerminalRunId = runId;
        }
      },
    });
    // A canonical carrier owns the lowest sequence in this transition; any
    // compatibility events it drains are later and may legitimately advance
    // the UI after this terminal phase.
    if (consumed.canonical) syncCanonicalTerminalPhase();
  }

  const legacyResults: LegacyProjectionResult[] = [];
  if (!consumed.canonical) {
    if (consumed.sessionEnvelope && !application) {
      // Buffered, conflicted, duplicate, late, and recovery-held
      // compatibility carriers mutate zero UI/transcript state — the content
      // renders when the missing sequence closes the gap.
      return false;
    }
    if (consumed.sessionEnvelope) {
      // Total ascending event sequence: the carrier (the lowest sequence of
      // the transition) projects before its drained buffered envelopes.
      legacyResults.push(applyLegacyProjectionEvent(deps, key, consumed.projectionEvent, isActiveRuntime));
      for (const drained of application?.compatibilityEvents || []) {
        legacyResults.push(applyLegacyProjectionEvent(deps, key, compatibilityProjectionEvent(drained), isActiveRuntime));
      }
    } else {
      // Raw non-envelope legacy frames keep the boolean contract.
      legacyResults.push(applyLegacyProjectionEvent(deps, key, consumed.projectionEvent, isActiveRuntime));
    }
  } else {
    // Compatibility envelopes the canonical carrier drained own exactly one
    // legacy projection each, in sequence order.
    for (const drained of application?.compatibilityEvents || []) {
      legacyResults.push(applyLegacyProjectionEvent(deps, key, compatibilityProjectionEvent(drained), isActiveRuntime));
    }
  }

  // A compatibility carrier that closes a gap owns the lowest sequence; its
  // drained canonical terminal is later and therefore settles the final UI
  // phase after the carrier's legacy projection.
  if (!consumed.canonical) syncCanonicalTerminalPhase();

  if (legacyResults.some((result) => result.applied)) commitRequested = true;
  if (!terminalCommit) {
    const acceptedLegacyTerminal = [...legacyResults].reverse()
      .find((result) => 'terminalRunId' in result);
    if (acceptedLegacyTerminal) {
      terminalCommit = true;
      canonicalTerminalRunId = acceptedLegacyTerminal.terminalRunId ?? null;
    }
  }

  if (commitRequested && isActiveRuntime) {
    const timeline = refs.compatibilityTimelines[key] ?? createCompatibilityMessageTimeline();
    const composedAll = composeMixedPlaneSessionMessages({
      store: refs.eventStores[key],
      compatibilityMessages: refs.replayStates[key]?.messages || [],
      timeline,
    });
    // The rewind visibility boundary survives the live composition:
    // identities hidden by a rewind stay hidden until the next hydration
    // publish replaces the boundary; newly arriving post-rewind events carry
    // new identities and stay visible.
    const composed = applySessionVisibilityBoundary(composedAll, refs.visibilityBoundaries[key] ?? null)
      .map(deps.parseChatMsg);
    deps.setChatMessagesSessionId(sessionId);
    (terminalCommit ? deps.setChatMessagesAfterQueued : deps.enqueueChatMessagesUpdate)(
      sessionId,
      (previous) => deps.mergePendingMessages(
        key,
        terminalCommit ? mergeCanonicalTerminalMessages(previous, composed, canonicalTerminalRunId) : composed,
      ),
    );
  }

  if (consumed.canonical) return application ?? false;
  if (consumed.sessionEnvelope) return application;
  return legacyResults[0]?.applied ?? false;
}
