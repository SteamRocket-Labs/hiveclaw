import { describe, expect, it, vi } from 'vitest';

import {
  createEmptyTranscriptReplayState,
  mergePendingUserMessages,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type PendingUserMessage,
  type RuntimePhase,
  type SessionUiState,
  type TranscriptReplayState,
} from './chatRuntime';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierDeps } from './sessionTranscriptApplier';
import type { SessionEventStore } from '../session-workbench/sessionEventStore';

const KEY = 'agent-1:session-1';

function canonicalTextEvent(sequence: number, content: string): ChatTranscriptEventPayload {
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
      run_id: 'run-1',
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

function makeApplierHarness() {
  const refs = {
    transcriptEvents: {} as Record<string, ChatTranscriptEventPayload[] | undefined>,
    eventStores: {} as Record<string, SessionEventStore | undefined>,
    fullHydrationKeys: new Set<string>(),
    replayStates: {} as Record<string, TranscriptReplayState | undefined>,
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
    mergePendingMessages: (key, messages) => mergePendingUserMessages(
      messages,
      refs.pendingUserMessages[key] || [],
    ).messages,
    setChatMessagesSessionId: vi.fn(),
    enqueueChatMessagesUpdate: (sessionId, updater) => {
      const result = updater(messagesBySession.get(sessionId) || []);
      commits.push({ sessionId, kind: 'enqueue', result });
      messagesBySession.set(sessionId, result);
    },
    setChatMessagesAfterQueued: (sessionId, updater) => {
      const result = updater(messagesBySession.get(sessionId) || []);
      commits.push({ sessionId, kind: 'afterQueued', result });
      messagesBySession.set(sessionId, result);
    },
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
  messagesBySession.set('session-1', [{ role: 'user', content: 'SEEDED USER', id: 'seeded-user' }]);
  const applyEvent = (event: ChatTranscriptEventPayload, isActiveRuntime = true) =>
    applyTranscriptToSessionRuntime(deps, 'agent-1', 'session-1', event, isActiveRuntime);
  return {
    refs,
    deps,
    commits,
    applyEvent,
    messages: () => messagesBySession.get('session-1') || [],
    transcriptEvents: () => refs.transcriptEvents[KEY] || [],
  };
}

describe('session transcript applier real consumption path (Codex REQUEST_CHANGES #3)', () => {
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

    // The canonical terminal message commit rendered the typed runtime_failure
    // card exactly once across the whole commit chain.
    const failureCards = harness.commits
      .flatMap((commit) => commit.result)
      .filter((message) => message.eventType === 'runtime_failure' && message.threadItem?.item_type === 'error');
    expect(failureCards).toHaveLength(1);
    expect(failureCards[0]).toMatchObject({ content: 'quota message' });

    // At-least-once redelivery of the drained failure: zero re-application.
    expect(harness.applyEvent(failure)).toBe(false);
    expect(harness.deps.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.transcriptEvents()
      .filter((existing) => (existing as unknown as { event_id?: string }).event_id === 'event-runtime-failure-2')).toHaveLength(1);
    expect(harness.commits
      .flatMap((commit) => commit.result)
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
});
