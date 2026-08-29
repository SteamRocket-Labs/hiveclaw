import { describe, expect, it, vi } from 'vitest';

import {
  createEmptyTranscriptReplayState,
  mergePendingUserMessages,
  uiForPhase,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type PendingUserMessage,
  type RuntimePhase,
  type SessionUiState,
  type TranscriptReplayState,
} from './chatRuntime';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import {
  createCompatibilityMessageTimeline,
  seedCompatibilityTimelineIdentities,
  type CompatibilityMessageTimeline,
  type SessionVisibilityBoundary,
} from './sessionEventConsumer';
import { projectCanonicalTranscriptSnapshot } from './sessionTranscriptHydration';
import { createSessionMessageStore, type SessionMessageStore } from './sessionMessageStore';
import { buildThreadTimeline } from '../session-workbench/timelineModel';
import type { ChatSession } from '../../api/domains/chat';
import type { SessionEventStore } from '../session-workbench/sessionEventStore';

const KEY = 'agent-1:session-1';

function canonicalTextEvent(sequence: number, content: string, runId = 'run-1'): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-text-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: {
      level: 'round',
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: runId,
      round_id: 'round-1',
    },
    item_id: `text-item-${sequence}`,
    item_kind: 'assistant_text',
    kind: 'assistant_text.delta',
    lifecycle: 'delta',
    payload_schema: 'hive.session.payload.assistant_text.delta.v2',
    actor: { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: { content, phase: 'unknown' },
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  } as unknown as ChatTranscriptEventPayload;
}

function runScopedFailureEvent(sequence: number, runId = 'run-1'): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-runtime-failure-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: runId },
    item_id: `failure-item-${sequence}`,
    item_kind: 'runtime_failure',
    kind: 'runtime_failure.recorded',
    lifecycle: 'recorded',
    payload_schema: 'hive.session.payload.runtime_failure.recorded.v2',
    actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' },
    payload: {
      status: 'failed',
      terminal_reason: 'provider_error',
      failure_code: 'quota_exhausted',
      delivery_state: 'rejected',
      requires_user_decision: true,
      retryable: true,
      content: 'quota message',
      message: 'quota message',
    },
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  } as unknown as ChatTranscriptEventPayload;
}

function canonicalRunLifecycleEvent(
  sequence: number,
  lifecycle: 'queued' | 'completed' | 'failed' | 'cancelled' | 'needs_reconciliation' = 'completed',
  runId = 'run-1',
  turnId = 'turn-1',
): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-run-${lifecycle}-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: turnId, run_id: runId },
    item_id: `run-item-${runId}`,
    item_kind: 'run',
    kind: `run.${lifecycle}`,
    lifecycle,
    payload_schema: `hive.session.payload.run.${lifecycle}.v2`,
    actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' },
    payload: { status: lifecycle },
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  } as unknown as ChatTranscriptEventPayload;
}

function compatibilityCarrier(
  sequence: number,
  legacyEventType: string,
  payload: Record<string, unknown>,
): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event_compatibility',
    schema_version: 1,
    compatibility_status: 'needs_reconciliation',
    reason: 'legacy_generation',
    event_id: `legacy-${sequence}`,
    sequence,
    legacy_event_type: legacyEventType,
    payload,
  } as unknown as ChatTranscriptEventPayload;
}

function canonicalInputEvent(sequence: number, content: string): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-input-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
    item_id: `input-item-${sequence}`,
    item_kind: 'human_input',
    kind: 'human_input.accepted',
    lifecycle: 'accepted',
    payload_schema: 'hive.session.payload.human_input.accepted.v2',
    actor: { type: 'user', id: 'user-1' },
    visibility: { audience: 'direct_user' },
    payload: { content },
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  } as unknown as ChatTranscriptEventPayload;
}

function canonicalAssistantEvent(
  sequence: number,
  itemKind: 'assistant_reasoning_private' | 'assistant_text' | 'assistant_final',
  lifecycle: 'delta' | 'snapshot' | 'completed',
  turnId: string,
  runId: string,
  itemId: string,
  payload: Record<string, unknown>,
): ChatTranscriptEventPayload {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-${itemKind}-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: {
      level: 'round',
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: turnId,
      run_id: runId,
      round_id: `round-${turnId}`,
    },
    item_id: itemId,
    item_kind: itemKind,
    kind: `${itemKind}.${lifecycle}`,
    lifecycle,
    payload_schema: `hive.session.payload.${itemKind}.${lifecycle}.v2`,
    actor: { type: 'assistant' },
    visibility: { audience: itemKind === 'assistant_reasoning_private' ? 'private_provider' : 'direct_user' },
    payload,
    occurred_at: '2026-08-29T13:01:01Z',
    persisted_at: '2026-08-29T13:01:01Z',
  } as unknown as ChatTranscriptEventPayload;
}

function makeApplierHarness(messageStore?: SessionMessageStore) {
  const refs = {
    transcriptEvents: {} as Record<string, ChatTranscriptEventPayload[] | undefined>,
    eventStores: {} as Record<string, SessionEventStore | undefined>,
    fullHydrationKeys: new Set<string>(),
    replayStates: {} as Record<string, TranscriptReplayState | undefined>,
    compatibilityTimelines: {} as Record<string, CompatibilityMessageTimeline | undefined>,
    visibilityBoundaries: {} as Record<string, SessionVisibilityBoundary | undefined>,
    uiStates: {} as Record<string, SessionUiState | undefined>,
    runtimeActivityAt: {} as Record<string, number | undefined>,
    pendingUserMessages: {} as Record<string, PendingUserMessage[] | undefined>,
  };
  const messagesBySession = new Map<string, AgentChatMessage[]>();
  const commits: Array<{ sessionId: string; kind: 'enqueue' | 'afterQueued'; result: AgentChatMessage[] }> = [];
  const deps: SessionTranscriptApplierDeps = {
    refs,
    markActiveRunTerminal: vi.fn(() => true),
    isTerminalTranscriptToolMessage: () => false,
    mergePendingMessages: (key, messages) => {
      const merged = mergePendingUserMessages(messages, refs.pendingUserMessages[key] || []);
      if (merged.pending.length > 0) refs.pendingUserMessages[key] = merged.pending;
      else delete refs.pendingUserMessages[key];
      return merged.messages;
    },
    setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: vi.fn((sessionId: string, updater: (messages: AgentChatMessage[]) => AgentChatMessage[]) => {
      if (messageStore) {
        messageStore.enqueueUpdate(sessionId, (previous) => {
          const result = updater(previous);
          commits.push({ sessionId, kind: 'enqueue', result });
          return result;
        });
        return;
      }
      const result = updater(messagesBySession.get(sessionId) || []);
      commits.push({ sessionId, kind: 'enqueue', result });
      messagesBySession.set(sessionId, result);
    }),
    setChatMessagesAfterQueued: vi.fn((sessionId: string, updater: (messages: AgentChatMessage[]) => AgentChatMessage[]) => {
      if (messageStore) {
        messageStore.updateAfterQueued(sessionId, (previous) => {
          const result = updater(previous);
          commits.push({ sessionId, kind: 'afterQueued', result });
          return result;
        });
        return;
      }
      const result = updater(messagesBySession.get(sessionId) || []);
      commits.push({ sessionId, kind: 'afterQueued', result });
      messagesBySession.set(sessionId, result);
    }),
    setActivePhase: vi.fn((_phase: RuntimePhase) => undefined),
    setIsWaiting: vi.fn(() => undefined),
    setIsStreaming: vi.fn(() => undefined),
    parseChatMsg: (message) => message,
  };
  // Production always seeds the legacy replay baseline before live delivery
  // (hydration projection or the empty-transcript session-message fallback);
  // mirror that seeded user message here.
  refs.replayStates[KEY] = {
    ...createEmptyTranscriptReplayState(),
    messages: [{ role: 'user', content: 'SEEDED USER', id: 'seeded-user' }],
  };
  // …and the compatibility timeline records the seeded baseline's sequence
  // exactly like a hydration publish would.
  refs.compatibilityTimelines[KEY] = {
    sequenceByIdentity: new Map([['seeded-user', 0]]),
    legacyAssistantRunByIdentity: new Map(),
    excludedIdentities: new Set(),
  };
  if (messageStore) {
    messageStore.updateAfterQueued('session-1', () => [{ role: 'user', content: 'SEEDED USER', id: 'seeded-user' }]);
  } else {
    messagesBySession.set('session-1', [{ role: 'user', content: 'SEEDED USER', id: 'seeded-user' }]);
  }
  const applyEvent = (event: ChatTranscriptEventPayload, isActiveRuntime = true) =>
    applyTranscriptToSessionRuntime(deps, 'agent-1', 'session-1', event, isActiveRuntime);
  return {
    refs,
    deps,
    commits,
    applyEvent,
    messages: () => messageStore?.getSnapshot('session-1') || messagesBySession.get('session-1') || [],
    flushMessages: () => messageStore?.flushSession('session-1'),
    transcriptEvents: () => refs.transcriptEvents[KEY] || [],
  };
}

describe('session transcript applier real consumption path (Codex REQUEST_CHANGES #3)', () => {
  it('settles the active UI phase when direct transcript backfill applies a canonical run terminal', () => {
    const harness = makeApplierHarness();
    harness.refs.uiStates[KEY] = uiForPhase('responding');

    expect(harness.applyEvent(canonicalRunLifecycleEvent(1))).toBeTruthy();

    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    expect(harness.deps.setActivePhase).toHaveBeenCalledWith('done');
    expect(harness.deps.setIsWaiting).toHaveBeenCalledWith(false);
    expect(harness.deps.setIsStreaming).toHaveBeenCalledWith(false);
    expect(harness.refs.uiStates[KEY]).toEqual(uiForPhase('done'));
  });

  it('settles an ambiguous provider send as review-required instead of reviving a running disclosure', () => {
    const harness = makeApplierHarness();
    harness.refs.uiStates[KEY] = uiForPhase('responding');

    expect(harness.applyEvent(canonicalRunLifecycleEvent(1, 'needs_reconciliation'))).toBeTruthy();

    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    expect(harness.deps.setActivePhase).toHaveBeenCalledWith('failed');
    expect(harness.deps.setIsWaiting).toHaveBeenCalledWith(false);
    expect(harness.deps.setIsStreaming).toHaveBeenCalledWith(false);
    expect(harness.refs.uiStates[KEY]).toEqual(uiForPhase('failed'));

    const timeline = buildThreadTimeline({
      messages: harness.messages(),
      activeSession: { id: 'session-1', title: 'Review required' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
      activeRunId: null,
      runtimePhase: harness.refs.uiStates[KEY]?.phase,
    });
    expect(timeline.header.status).toBe('failed');
    expect(timeline.cells.filter((cell) => (
      cell.kind === 'active_run'
      && (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
    ))).toHaveLength(0);
    expect(timeline.cells.filter((cell) => (
      cell.kind === 'active_run' && cell.timeline.status === 'failed'
    ))).toHaveLength(1);

    const reloaded = projectCanonicalTranscriptSnapshot({
      existing: [],
      snapshot: [
        canonicalInputEvent(1, 'RETURN THE MARKER'),
        canonicalRunLifecycleEvent(2, 'needs_reconciliation'),
      ],
      session: { id: 'session-1' } as unknown as ChatSession,
      parseMessage: (message) => message,
    });
    expect(reloaded.ui).toEqual(uiForPhase('failed'));
    const reloadedTimeline = buildThreadTimeline({
      messages: reloaded.messages,
      activeSession: { id: 'session-1', title: 'Review required' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
      activeRunId: null,
      runtimePhase: reloaded.ui.phase,
    });
    expect(reloadedTimeline.header.status).toBe('failed');
    expect(reloadedTimeline.cells.filter((cell) => (
      cell.kind === 'active_run' && cell.timeline.status === 'failed'
    ))).toHaveLength(1);
  });

  it('keeps a newer active UI phase byte-identical when canonical terminal identity is rejected', () => {
    const harness = makeApplierHarness();
    const activeUi = uiForPhase('responding');
    harness.refs.uiStates[KEY] = activeUi;
    harness.deps.markActiveRunTerminal = vi.fn(() => false);

    expect(harness.applyEvent(canonicalRunLifecycleEvent(1))).toBeTruthy();

    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    expect(harness.refs.uiStates[KEY]).toBe(activeUi);
    expect(harness.deps.setActivePhase).not.toHaveBeenCalled();
    expect(harness.deps.setIsWaiting).not.toHaveBeenCalled();
    expect(harness.deps.setIsStreaming).not.toHaveBeenCalled();
  });

  it('keeps an accepted prompt visible through a lifecycle-only canonical input placeholder', () => {
    const harness = makeApplierHarness();
    const pending = {
      message: {
        id: 'input-item-1',
        role: 'user' as const,
        content: 'ACCEPTED PROMPT',
      },
      anchorMessageCount: 1,
    };
    harness.refs.pendingUserMessages[KEY] = [pending];

    expect(harness.applyEvent(canonicalInputEvent(1, ''))).toBeTruthy();
    expect(harness.messages().filter((message) => message.id === 'input-item-1')).toEqual([
      expect.objectContaining({
        role: 'user',
        content: 'ACCEPTED PROMPT',
        transcriptEventId: 'event-input-1',
      }),
    ]);
    expect(harness.refs.pendingUserMessages[KEY]).toEqual([pending]);
  });

  it('upgrades an empty-transcript stored user carrier when its durable compatibility event arrives', () => {
    const harness = makeApplierHarness();
    const stored = {
      id: 'message-user-1',
      messageId: 'message-user-1',
      role: 'user' as const,
      content: 'Exact branch retry prompt.',
    };
    harness.refs.replayStates[KEY] = {
      ...createEmptyTranscriptReplayState(),
      messages: [stored],
    };
    harness.refs.compatibilityTimelines[KEY] = createCompatibilityMessageTimeline();
    seedCompatibilityTimelineIdentities(harness.refs.compatibilityTimelines[KEY]!, [stored]);
    harness.deps.setChatMessagesAfterQueued('session-1', () => [stored]);

    expect(harness.applyEvent(compatibilityCarrier(1, 'user_message', {
      message_id: 'message-user-1',
      content: 'Exact branch retry prompt.',
      metadata: { role: 'user' },
    }))).toBeTruthy();

    expect(harness.messages().filter((message) => message.role === 'user')).toEqual([
      expect.objectContaining({
        id: 'message-user-1',
        messageId: 'message-user-1',
        transcriptEventId: 'legacy-1',
        content: 'Exact branch retry prompt.',
      }),
    ]);
  });

  it('proves a compatibility carrier that drains a canonical runtime_failure updates canonical transcript, messages, and terminal acceptance exactly once (finding A)', () => {
    const harness = makeApplierHarness();
    const failure = runScopedFailureEvent(2);

    // Arrival with sequence 1 missing: buffered only, zero application.
    expect(harness.applyEvent(failure)).toBe(false);
    expect(harness.refs.eventStores[KEY]?.projection.phase).toBe('gap_detected');
    expect(harness.transcriptEvents()).toEqual([]);
    expect(harness.commits).toHaveLength(0);
    expect(harness.deps.markActiveRunTerminal).not.toHaveBeenCalled();

    // The compatibility carrier fills sequence 1 and drains the failure.
    const carrier = compatibilityCarrier(1, 'phase', { content: '', metadata: { phase: 'done' } });
    const application = harness.applyEvent(carrier) as {
      canonicalEvents: Array<{ event_id: string }>;
      compatibilityApplied: boolean;
    };
    expect(application).toEqual(expect.objectContaining({ compatibilityApplied: true }));
    expect(application.canonicalEvents.map((applied) => applied.event_id)).toEqual(['event-runtime-failure-2']);

    // Canonical transcript updated exactly once with the drained failure bytes.
    expect(harness.transcriptEvents()
      .filter((existing) => (existing as unknown as { event_id?: string }).event_id === 'event-runtime-failure-2')).toHaveLength(1);

    // Terminal acceptance fired exactly once for the failure run.
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    expect(harness.deps.setActivePhase).toHaveBeenCalledWith('failed');
    expect(harness.deps.setIsWaiting).toHaveBeenCalledWith(false);
    expect(harness.deps.setIsStreaming).toHaveBeenCalledWith(false);
    expect(harness.refs.uiStates[KEY]).toEqual(uiForPhase('failed'));

    // Final visible state (not flattened commit history): the typed
    // runtime_failure error card survives the message-neutral compatibility
    // carrier's commit — no cross-plane whole-list last-writer-wins.
    const finalMessages = harness.messages();
    const finalFailureCards = finalMessages
      .filter((message) => message.eventType === 'runtime_failure' && message.threadItem?.item_type === 'error');
    expect(finalFailureCards).toHaveLength(1);
    expect(finalFailureCards[0]).toMatchObject({ content: expect.stringContaining('quota or balance') });
    expect(finalFailureCards[0]?.content).not.toContain('quota message');
    const seededIndex = finalMessages.findIndex((message) => message.id === 'seeded-user');
    const failureIndex = finalMessages.findIndex((message) => message.eventType === 'runtime_failure');
    expect(seededIndex).toBeGreaterThanOrEqual(0);
    expect(seededIndex).toBeLessThan(failureIndex);

    // At-least-once redelivery of the drained failure: zero re-application.
    expect(harness.applyEvent(failure)).toBe(false);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.transcriptEvents()
      .filter((existing) => (existing as unknown as { event_id?: string }).event_id === 'event-runtime-failure-2')).toHaveLength(1);
    expect(harness.messages()
      .filter((message) => message.eventType === 'runtime_failure')).toHaveLength(1);
  });

  it('performs zero UI/transcript mutation for a buffered compatibility carrier until contiguous application (finding B)', () => {
    const harness = makeApplierHarness();
    const carrier = compatibilityCarrier(2, 'user_message', { content: 'CARRIER CONTENT', metadata: {} });

    // Sequence 1 missing: the carrier is buffered only.
    expect(harness.applyEvent(carrier)).toBe(false);
    expect(harness.refs.eventStores[KEY]?.projection.phase).toBe('gap_detected');
    expect(harness.transcriptEvents()).toEqual([]);
    expect(harness.commits).toHaveLength(0);
    expect(harness.refs.replayStates[KEY]?.messages.map((message) => message.content)).toEqual(['SEEDED USER']);
    expect(harness.deps.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.deps.setActivePhase).not.toHaveBeenCalled();

    // Contiguous application projects the carrier content exactly once.
    const application = harness.applyEvent(canonicalTextEvent(1, 'gap filler'));
    expect(application).toEqual(expect.objectContaining({ compatibilityApplied: false }));
    const carrierRenderings = harness.commits
      .flatMap((commit) => commit.result)
      .filter((message) => message.content === 'CARRIER CONTENT');
    expect(carrierRenderings).toHaveLength(1);
    expect(harness.transcriptEvents().some((existing) => existing.sequence === 2)).toBe(true);

    // Duplicate redelivery of the drained carrier: zero further projection.
    expect(harness.applyEvent(carrier)).toBe(false);
    expect(harness.commits
      .flatMap((commit) => commit.result)
      .filter((message) => message.content === 'CARRIER CONTENT')).toHaveLength(1);
  });

  it('legacy-projects compatibility envelopes drained by a canonical carrier exactly once, in sequence order (finding C)', () => {
    const harness = makeApplierHarness();
    const firstDrained = compatibilityCarrier(2, 'user_message', { content: 'FIRST DRAINED', metadata: {} });
    const secondDrained = compatibilityCarrier(3, 'user_message', { content: 'SECOND DRAINED', metadata: {} });

    expect(harness.applyEvent(firstDrained)).toBe(false);
    expect(harness.applyEvent(secondDrained)).toBe(false);
    expect(harness.commits).toHaveLength(0);

    const application = harness.applyEvent(canonicalTextEvent(1, 'gap filler')) as {
      compatibilityEvents: Array<{ event_id: string }>;
      compatibilityApplied: boolean;
    };
    expect(application).toEqual(expect.objectContaining({ compatibilityApplied: false }));
    expect(application.compatibilityEvents.map((applied) => applied.event_id)).toEqual(['legacy-2', 'legacy-3']);

    // Both drained envelopes rendered through the legacy projection, in
    // sequence order, exactly once each.
    const drainedContents = harness.refs.replayStates[KEY]?.messages
      .map((message) => message.content)
      .filter((content) => content === 'FIRST DRAINED' || content === 'SECOND DRAINED');
    expect(drainedContents).toEqual(['FIRST DRAINED', 'SECOND DRAINED']);
    expect(harness.transcriptEvents().filter((existing) => existing.sequence === 2 || existing.sequence === 3))
      .toHaveLength(2);

    // Redelivery of a drained envelope: zero re-projection.
    expect(harness.applyEvent(firstDrained)).toBe(false);
    expect(harness.refs.replayStates[KEY]?.messages
      .filter((message) => message.content === 'FIRST DRAINED')).toHaveLength(1);
  });

  it('keeps the final visible list as a deduped ascending union when a canonical carrier drains compatibility events (reverse mixed direction)', () => {
    const harness = makeApplierHarness();
    const firstDrained = compatibilityCarrier(2, 'user_message', { content: 'FIRST DRAINED', metadata: {} });
    const secondDrained = compatibilityCarrier(3, 'user_message', { content: 'SECOND DRAINED', metadata: {} });

    expect(harness.applyEvent(firstDrained)).toBe(false);
    expect(harness.applyEvent(secondDrained)).toBe(false);
    expect(harness.messages()).toEqual([{ role: 'user', content: 'SEEDED USER', id: 'seeded-user' }]);

    const application = harness.applyEvent(canonicalTextEvent(1, 'gap filler')) as {
      compatibilityEvents: Array<{ event_id: string }>;
    };
    expect(application.compatibilityEvents.map((applied) => applied.event_id)).toEqual(['legacy-2', 'legacy-3']);

    // Final visible state is the deterministic union of both planes: the
    // drained compatibility whole-list commit must not drop the canonical
    // plane, every message appears exactly once, and the surviving order is
    // total ascending event sequence.
    const finalMessages = harness.messages();
    expect(finalMessages.filter((message) => message.content === 'gap filler')).toHaveLength(1);
    expect(finalMessages.filter((message) => message.content === 'FIRST DRAINED')).toHaveLength(1);
    expect(finalMessages.filter((message) => message.content === 'SECOND DRAINED')).toHaveLength(1);
    const orderedContents = finalMessages.map((message) => message.content);
    const gapIndex = orderedContents.indexOf('gap filler');
    const firstIndex = orderedContents.indexOf('FIRST DRAINED');
    const secondIndex = orderedContents.indexOf('SECOND DRAINED');
    expect(gapIndex).toBeGreaterThanOrEqual(0);
    expect(gapIndex).toBeLessThan(firstIndex);
    expect(firstIndex).toBeLessThan(secondIndex);

    // Redelivery of the canonical carrier: the union stays exactly-once.
    expect(harness.applyEvent(canonicalTextEvent(1, 'gap filler'))).toBe(false);
    const afterRedelivery = harness.messages();
    expect(afterRedelivery.filter((message) => message.content === 'gap filler')).toHaveLength(1);
    expect(afterRedelivery.filter((message) => message.content === 'FIRST DRAINED')).toHaveLength(1);
    expect(afterRedelivery.filter((message) => message.content === 'SECOND DRAINED')).toHaveLength(1);
  });

  it('honors a rejected stale compatibility terminal: durable evidence kept, zero active terminal seal or phase mutation (finding D)', () => {
    const harness = makeApplierHarness();
    // Registry semantics with run-2 active: a run-1 terminal is recorded but
    // rejected for the active run.
    harness.deps.markActiveRunTerminal = vi.fn((_key: string, runId?: string | null) => runId !== 'run-1');

    // Active run-2 visible tail.
    expect(harness.applyEvent(canonicalTextEvent(1, 'RUN-2 LIVE TAIL', 'run-2'))).toBeTruthy();
    expect(harness.messages().some((message) => message.content === 'RUN-2 LIVE TAIL')).toBe(true);

    const staleTerminal = compatibilityCarrier(2, 'assistant_message', {
      content: 'STALE RUN-1 FINAL',
      legacy_run_id: 'run-1',
      metadata: {},
    });
    harness.applyEvent(staleTerminal);

    // The stale terminal identity is still checked with the registry exactly
    // once (recorded as terminal, never re-observed as active)…
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    // …durable historical evidence is preserved…
    expect(harness.transcriptEvents().some((existing) => existing.sequence === 2)).toBe(true);
    // …but a rejected terminal must not seal the active UI…
    expect(harness.deps.setChatMessagesAfterQueued).not.toHaveBeenCalled();
    // …must not mutate the active phase/waiting/streaming state…
    expect(harness.deps.setActivePhase).not.toHaveBeenCalled();
    expect(harness.deps.setIsWaiting).not.toHaveBeenCalled();
    expect(harness.deps.setIsStreaming).not.toHaveBeenCalled();
    // …and must not replace the active run-2 visible tail.
    expect(harness.messages().some((message) => message.content === 'RUN-2 LIVE TAIL')).toBe(true);
  });

  it('seals the active run exactly once for a matching compatibility terminal (finding D positive control)', () => {
    const harness = makeApplierHarness();
    harness.deps.markActiveRunTerminal = vi.fn(() => true);

    expect(harness.applyEvent(canonicalTextEvent(1, 'RUN-2 LIVE TAIL', 'run-2'))).toBeTruthy();
    const matchingTerminal = compatibilityCarrier(2, 'assistant_message', {
      content: 'MATCHING RUN-2 FINAL',
      legacy_run_id: 'run-2',
      metadata: {},
    });
    harness.applyEvent(matchingTerminal);

    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-2');
    expect(harness.deps.setChatMessagesAfterQueued).toHaveBeenCalledTimes(1);
    expect(harness.messages().some((message) => message.content === 'MATCHING RUN-2 FINAL')).toBe(true);
  });

  it('keeps prior answer bytes exact-once when a live run terminal beats the current canonical final', () => {
    let frameId = 0;
    const messageStore = createSessionMessageStore({
      requestFrame: () => ++frameId,
      cancelFrame: () => undefined,
    });
    const firstTurn = [
      canonicalInputEvent(1, 'FIRST PROMPT'),
      canonicalAssistantEvent(2, 'assistant_text', 'snapshot', 'turn-1', 'run-1', 'text-item-turn-1', {
        phase: 'unknown', content: 'FIRST ANSWER',
      }),
      canonicalAssistantEvent(3, 'assistant_final', 'completed', 'turn-1', 'run-1', 'final-item-turn-1', {
        phase: 'final',
        render_owner_id: 'render-owner-turn-1',
        zero_copy: true,
        source_blocks: [{ item_id: 'text-item-turn-1', block_index: 0, content_hash: 'hash-turn-1' }],
      }),
      canonicalRunLifecycleEvent(4, 'completed', 'run-1'),
    ];
    const projected = projectCanonicalTranscriptSnapshot({
      existing: [],
      snapshot: firstTurn,
      session: { id: 'session-1' } as unknown as ChatSession,
      parseMessage: (message) => message,
    });
    const harness = makeApplierHarness(messageStore);
    harness.refs.transcriptEvents[KEY] = projected.events;
    if (projected.store) harness.refs.eventStores[KEY] = projected.store;
    harness.refs.replayStates[KEY] = projected.replay;
    harness.refs.compatibilityTimelines[KEY] = projected.compatibilityTimeline;
    harness.refs.visibilityBoundaries[KEY] = projected.visibilityBoundary;
    harness.refs.uiStates[KEY] = projected.ui;
    harness.deps.setChatMessagesAfterQueued('session-1', () => projected.messages);

    // Mirror the production ordering: canonical accepted input + queued run +
    // private reasoning are frame-queued, then a live run_completed witness
    // settles the active registry before assistant_final reaches the durable
    // socket tail. updateAfterQueued must flush the real store without using
    // the prior turn's final as this turn's answer.
    expect(harness.applyEvent(canonicalInputEvent(5, 'SECOND PROMPT'))).toBeTruthy();
    expect(harness.applyEvent(canonicalRunLifecycleEvent(6, 'queued', 'run-2', 'turn-2'))).toBeTruthy();
    expect(harness.applyEvent(canonicalAssistantEvent(
      7,
      'assistant_reasoning_private',
      'delta',
      'turn-2',
      'run-2',
      'reasoning-item-turn-2',
      { phase: 'reasoning_private', content: 'CURRENT PROCESS' },
    ))).toBeTruthy();
    expect(harness.applyEvent({
      id: 'live-run-completed-turn-2',
      event_type: 'run_completed',
      run_id: 'run-2',
      content: '',
    })).toBe(true);

    const liveMessages = harness.messages();
    expect(harness.deps.markActiveRunTerminal).toHaveBeenLastCalledWith(KEY, 'run-2');
    expect(liveMessages.filter((message) => message.content === 'FIRST ANSWER')).toHaveLength(1);
    let latestUserIndex = -1;
    for (let index = liveMessages.length - 1; index >= 0; index -= 1) {
      if (liveMessages[index]?.content !== 'SECOND PROMPT') continue;
      latestUserIndex = index;
      break;
    }
    expect(liveMessages.slice(latestUserIndex + 1).map((message) => message.content))
      .not.toContain('FIRST ANSWER');

    const timeline = buildThreadTimeline({
      messages: liveMessages,
      isWaiting: false,
      isStreaming: true,
      activeRunStatus: 'running',
      activeRunId: 'run-2',
      runtimePhase: 'responding',
    });
    expect(timeline.cells.filter((cell) => (
      cell.kind === 'active_run'
      && (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
    ))).toHaveLength(1);

    const secondTurnTail = [
      canonicalAssistantEvent(8, 'assistant_text', 'snapshot', 'turn-2', 'run-2', 'text-item-turn-2', {
        phase: 'unknown', content: 'SECOND ANSWER',
      }),
      canonicalAssistantEvent(9, 'assistant_final', 'completed', 'turn-2', 'run-2', 'final-item-turn-2', {
        phase: 'final',
        render_owner_id: 'render-owner-turn-2',
        zero_copy: true,
        source_blocks: [{ item_id: 'text-item-turn-2', block_index: 0, content_hash: 'hash-turn-2' }],
      }),
      canonicalRunLifecycleEvent(10, 'completed', 'run-2', 'turn-2'),
    ];
    for (const event of secondTurnTail) expect(harness.applyEvent(event)).toBeTruthy();

    const terminalMessages = harness.messages();
    const reloaded = projectCanonicalTranscriptSnapshot({
      existing: [],
      snapshot: [
        ...firstTurn,
        canonicalInputEvent(5, 'SECOND PROMPT'),
        canonicalRunLifecycleEvent(6, 'queued', 'run-2', 'turn-2'),
        canonicalAssistantEvent(
          7,
          'assistant_reasoning_private',
          'delta',
          'turn-2',
          'run-2',
          'reasoning-item-turn-2',
          { phase: 'reasoning_private', content: 'CURRENT PROCESS' },
        ),
        ...secondTurnTail,
      ],
      session: { id: 'session-1' } as unknown as ChatSession,
      parseMessage: (message) => message,
    });
    const visibleConversation = (messages: AgentChatMessage[]) => messages
      .filter((message) => (message.role === 'user' || message.role === 'assistant') && message.content)
      .map((message) => ({ id: message.id, role: message.role, content: message.content }));
    expect(visibleConversation(terminalMessages)).toEqual(visibleConversation(reloaded.messages));
    expect(terminalMessages.filter((message) => message.content === 'FIRST ANSWER')).toHaveLength(1);
    expect(terminalMessages.filter((message) => message.content === 'SECOND ANSWER')).toHaveLength(1);

    const terminalTimeline = buildThreadTimeline({
      messages: terminalMessages,
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
      activeRunId: null,
      runtimePhase: 'done',
    });
    expect(terminalTimeline.cells.filter((cell) => cell.kind === 'user_turn')).toHaveLength(2);
    expect(terminalTimeline.cells.filter((cell) => cell.kind === 'assistant_final')).toHaveLength(2);
    expect(terminalTimeline.cells.filter((cell) => (
      cell.kind === 'active_run'
      && (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
    ))).toHaveLength(0);
  });

  it('isolates a rejected stale legacy terminal from the active run-2 projection while keeping durable evidence (Codex REQUEST_CHANGES #4 finding A)', () => {
    const harness = makeApplierHarness();
    // Registry semantics with run-2 active: a run-1 terminal is recorded but
    // rejected for the active run.
    harness.deps.markActiveRunTerminal = vi.fn((_key: string, runId?: string | null) => runId !== 'run-1');
    const run2Ui = uiForPhase('responding');
    harness.refs.uiStates[KEY] = run2Ui;

    // Active run-2 visible tail and UI state.
    expect(harness.applyEvent(canonicalTextEvent(1, 'RUN-2 LIVE TAIL', 'run-2'))).toBeTruthy();
    expect(harness.messages().some((message) => message.content === 'RUN-2 LIVE TAIL')).toBe(true);

    harness.applyEvent(compatibilityCarrier(2, 'assistant_message', {
      content: 'STALE RUN-1 FINAL',
      legacy_run_id: 'run-1',
      metadata: {},
    }));

    // Durable evidence retained on every durable plane: transcript backfill,
    // legacy replay, and the compatibility timeline bookkeeping.
    expect(harness.transcriptEvents().some((existing) => existing.sequence === 2)).toBe(true);
    const staleMessage = harness.refs.replayStates[KEY]?.messages
      .find((message) => message.content === 'STALE RUN-1 FINAL');
    expect(staleMessage).toBeDefined();
    const staleIdentity = String(staleMessage?.transcriptEventId || staleMessage?.messageId || staleMessage?.id || '');
    expect(staleIdentity).not.toBe('');
    expect(harness.refs.compatibilityTimelines[KEY]?.sequenceByIdentity.has(staleIdentity)).toBe(true);

    // The registry observed the stale identity exactly once…
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    // …the run-2 UI state object is byte-identical (untouched)…
    expect(harness.refs.uiStates[KEY]).toBe(run2Ui);
    // …zero phase/waiting/streaming mutation and zero terminal seal commit…
    expect(harness.deps.setActivePhase).not.toHaveBeenCalled();
    expect(harness.deps.setIsWaiting).not.toHaveBeenCalled();
    expect(harness.deps.setIsStreaming).not.toHaveBeenCalled();
    expect(harness.deps.setChatMessagesAfterQueued).not.toHaveBeenCalled();
    // …and the stale terminal content never enters the active visible list.
    expect(harness.messages().some((message) => message.content === 'STALE RUN-1 FINAL')).toBe(false);

    // A later run-2 live delta recomposes without leaking the stale content.
    expect(harness.applyEvent(canonicalTextEvent(3, 'RUN-2 NEXT DELTA', 'run-2'))).toBeTruthy();
    const visible = harness.messages().map((message) => message.content);
    expect(visible).not.toContain('STALE RUN-1 FINAL');
    expect(visible).toContain('RUN-2 LIVE TAIL');
    expect(visible).toContain('RUN-2 NEXT DELTA');
  });

  it('keeps stored-fallback legacy history before the first live canonical event (Codex REQUEST_CHANGES #4 finding E)', () => {
    const harness = makeApplierHarness();
    const legacyHistory: AgentChatMessage[] = [
      { role: 'user', content: 'LEGACY STORED PROMPT', id: 'legacy-stored-1' },
      { role: 'assistant', content: 'LEGACY STORED ANSWER', id: 'legacy-stored-2' },
    ];
    // Mirror the production empty-transcript fallback (AgentDetail
    // getSessionMessages path): stored ChatMessages become the replay
    // baseline AND receive deterministic pre-live timeline sequences, so the
    // first live canonical event composes after the legacy history.
    harness.refs.replayStates[KEY] = { ...createEmptyTranscriptReplayState(), messages: legacyHistory };
    const fallbackTimeline = createCompatibilityMessageTimeline();
    seedCompatibilityTimelineIdentities(fallbackTimeline, legacyHistory);
    harness.refs.compatibilityTimelines[KEY] = fallbackTimeline;
    harness.deps.enqueueChatMessagesUpdate('session-1', () => legacyHistory);

    expect(harness.applyEvent(canonicalTextEvent(1, 'NEW CANONICAL PROGRESS'))).toBeTruthy();
    expect(harness.messages().map((message) => message.content)).toEqual([
      'LEGACY STORED PROMPT',
      'LEGACY STORED ANSWER',
      'NEW CANONICAL PROGRESS',
    ]);
  });

  it('keeps rewind-hidden messages hidden when a live canonical delta arrives after hydration (Codex REQUEST_CHANGES #4 finding F)', () => {
    const rewindSession = {
      id: 'session-1',
      transcript_metadata_json: {
        active_projection: {
          projection_reason: 'rewind',
          checkpoint_event_id: 'event-input-3',
          draft_content: '',
        },
      },
    } as unknown as ChatSession;
    const projected = projectCanonicalTranscriptSnapshot({
      existing: [],
      snapshot: [
        canonicalInputEvent(1, 'FIRST PROMPT'),
        canonicalTextEvent(2, 'FIRST ANSWER'),
        canonicalInputEvent(3, 'SECOND PROMPT'),
        canonicalTextEvent(4, 'SECOND ANSWER'),
      ],
      session: rewindSession,
      parseMessage: (message) => message,
    });
    // The hydration publish itself trims to the rewind boundary.
    expect(projected.messages.map((message) => message.content)).toEqual(['FIRST PROMPT', 'FIRST ANSWER']);

    // Seed the real applier refs exactly like the production publish path.
    const harness = makeApplierHarness();
    harness.refs.transcriptEvents[KEY] = projected.events;
    if (projected.store) harness.refs.eventStores[KEY] = projected.store;
    harness.refs.replayStates[KEY] = projected.replay;
    harness.refs.compatibilityTimelines[KEY] = projected.compatibilityTimeline;
    harness.refs.visibilityBoundaries[KEY] = projected.visibilityBoundary;
    harness.deps.enqueueChatMessagesUpdate('session-1', () => projected.messages);

    expect(harness.applyEvent(canonicalTextEvent(5, 'POST-REWIND PROGRESS'))).toBeTruthy();
    expect(harness.messages().map((message) => message.content)).toEqual([
      'FIRST PROMPT',
      'FIRST ANSWER',
      'POST-REWIND PROGRESS',
    ]);
  });

  it('keeps an active raw compatibility stream intact across a rejected stale run-1 terminal (Codex REQUEST_CHANGES #4 finding A raw-stream case)', () => {
    const harness = makeApplierHarness();
    harness.deps.markActiveRunTerminal = vi.fn((_key: string, runId?: string | null) => runId !== 'run-1');
    harness.refs.uiStates[KEY] = uiForPhase('responding');

    // Active run-2 raw compatibility stream (anonymous _streaming assistant).
    expect(harness.applyEvent(compatibilityCarrier(1, 'chunk', {
      content: 'RUN-2 PARTIAL',
      legacy_run_id: 'run-2',
      metadata: {},
    }))).toBeTruthy();
    expect(harness.messages().filter((message) => message.content === 'RUN-2 PARTIAL')).toHaveLength(1);
    // The non-terminal stream setup legitimately owns the UI state; the stale
    // terminal must keep that exact object untouched.
    const uiAfterStreamSetup = harness.refs.uiStates[KEY];
    expect(uiAfterStreamSetup).toBeDefined();

    vi.mocked(harness.deps.setActivePhase).mockClear();
    vi.mocked(harness.deps.setIsWaiting).mockClear();
    vi.mocked(harness.deps.setIsStreaming).mockClear();
    vi.mocked(harness.deps.setChatMessagesAfterQueued).mockClear();
    harness.applyEvent(compatibilityCarrier(2, 'assistant_message', {
      content: 'STALE RUN-1 FINAL',
      legacy_run_id: 'run-1',
      metadata: {},
    }));

    // Durable evidence on all three planes: transcript, replay, timeline.
    expect(harness.transcriptEvents().some((existing) => existing.sequence === 2)).toBe(true);
    const staleMessage = harness.refs.replayStates[KEY]?.messages
      .find((message) => message.content === 'STALE RUN-1 FINAL');
    expect(staleMessage).toBeDefined();
    const staleIdentity = String(staleMessage?.transcriptEventId || staleMessage?.messageId || staleMessage?.id || '');
    expect(staleIdentity).not.toBe('');
    expect(harness.refs.compatibilityTimelines[KEY]?.sequenceByIdentity.has(staleIdentity)).toBe(true);

    // Registry observed and rejected run-1 exactly once; zero active mutation.
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledWith(KEY, 'run-1');
    expect(harness.refs.uiStates[KEY]).toBe(uiAfterStreamSetup);
    expect(harness.deps.setActivePhase).not.toHaveBeenCalled();
    expect(harness.deps.setIsWaiting).not.toHaveBeenCalled();
    expect(harness.deps.setIsStreaming).not.toHaveBeenCalled();
    expect(harness.deps.setChatMessagesAfterQueued).not.toHaveBeenCalled();

    // The exact pre-terminal run-2 partial remains visible exactly once; the
    // stale terminal content never enters the active visible list.
    const visible = harness.messages().map((message) => message.content);
    expect(visible.filter((content) => content === 'RUN-2 PARTIAL')).toHaveLength(1);
    expect(visible).not.toContain('STALE RUN-1 FINAL');

    // A later run-2 delta continues the SAME stream: combined content, no
    // split, no loss.
    expect(harness.applyEvent(compatibilityCarrier(3, 'chunk', {
      content: ' CONTINUED',
      legacy_run_id: 'run-2',
      metadata: {},
    }))).toBeTruthy();
    const after = harness.messages().map((message) => message.content);
    expect(after.filter((content) => content === 'RUN-2 PARTIAL CONTINUED')).toHaveLength(1);
    expect(after).not.toContain('RUN-2 PARTIAL');
    expect(after).not.toContain(' CONTINUED');
    expect(after).not.toContain('STALE RUN-1 FINAL');
  });
});
