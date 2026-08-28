import { describe, expect, it, vi } from 'vitest';

import { projectSessionSocketEvent, type SessionSocketProjectionDependencies } from './sessionSocketEventProjector';
import {
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
  type AgentChatMessage,
} from './chatRuntime';
import { consumeSessionEnvelope } from './sessionEventConsumer';
import { applyTranscriptToSessionRuntime, type SessionTranscriptApplierRefs } from './sessionTranscriptApplier';
import { buildSessionRightPanelModel } from '../session-workbench/timelineModel';
import type { SessionSocketMessageContext } from './useSessionTransportController';
import type { SessionEventStore } from '../session-workbench/sessionEventStore';

function makeHarness(data: Record<string, unknown>, isActiveRuntime = true) {
  const messagesBySession = new Map<string, AgentChatMessage[]>();
  const targetedSessions: string[] = [];
  const closeSessionSocket = vi.fn();
  const failAuthentication = vi.fn();
  const context: SessionSocketMessageContext = {
    data,
    session: { id: 'session-1' },
    agentId: 'agent-1',
    sessionId: 'session-1',
    key: 'agent-1:session-1',
    isActiveRuntime,
    closeSessionSocket,
    failAuthentication,
  };
  const dependencies: SessionSocketProjectionDependencies = {
    applyTranscriptToSession: vi.fn(() => true),
    selectSession: vi.fn(),
    fetchMySessions: vi.fn(),
    setSessionPhase: vi.fn(),
    sessionPhaseOf: vi.fn(() => 'responding' as const),
    syncActivePhase: vi.fn(),
    setActiveRunState: vi.fn(),
    markActiveRunTerminal: vi.fn(() => true),
    activeRunIdOf: vi.fn(() => null),
    invalidateSessionRuntimeQueries: vi.fn(),
    reconcileSessionTranscript: vi.fn(),
    shouldInvalidateToolCall: vi.fn(() => true),
    isTerminalTranscriptToolMessage: vi.fn(() => false),
    normalizeToolCallMessage: vi.fn((message) => message),
    parseChatMsg: vi.fn((message) => message),
    setChatMessagesSessionId: vi.fn(),
    setTransportNotice: vi.fn(),
    enqueueChatMessagesUpdate: (sessionId, updater) => {
      targetedSessions.push(sessionId);
      messagesBySession.set(sessionId, updater(messagesBySession.get(sessionId) || []));
    },
    setChatMessagesAfterQueued: (sessionId, updater) => {
      targetedSessions.push(sessionId);
      messagesBySession.set(sessionId, updater(messagesBySession.get(sessionId) || []));
    },
    setCreatedAgentId: vi.fn(),
    setAgentExpired: vi.fn(),
    invalidateQuery: vi.fn(),
  };
  return {
    context,
    dependencies,
    closeSessionSocket,
    failAuthentication,
    messages: (sessionId = 'session-1') => messagesBySession.get(sessionId) || [],
    targetedSessions: () => targetedSessions,
  };
}

function runtimeFailureEnvelope(runId: string, eventId = 'event-runtime-failure-1', sequence = 9) {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: eventId,
    sequence,
    item_id: `failure-item-${eventId}`,
    item_kind: 'runtime_failure',
    lifecycle: 'recorded',
    kind: 'runtime_failure.recorded',
    payload_schema: 'hive.session.payload.runtime_failure.recorded.v2',
    tenant_id: 'tenant-1',
    scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: runId },
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
  };
}

function runTerminalEnvelope(runId: string, eventId = 'event-run-terminal-1', sequence = 11) {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: eventId,
    sequence,
    item_id: `run-item-${eventId}`,
    item_kind: 'run',
    lifecycle: 'completed',
    kind: 'run.completed',
    payload_schema: 'hive.session.payload.run.completed.v2',
    tenant_id: 'tenant-1',
    scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: runId },
    actor: { type: 'runtime' },
    visibility: { audience: 'direct_user' },
    payload: {},
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  };
}

function legacyAssistantTerminalEnvelope(runId: string, eventId = 'event-legacy-final-1', sequence = 12) {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: eventId,
    sequence,
    item_id: `assistant-item-${eventId}`,
    item_kind: 'assistant_text',
    lifecycle: 'completed',
    kind: 'assistant_text.completed',
    payload_schema: 'hive.session.payload.assistant_text.completed.v2',
    tenant_id: 'tenant-1',
    scope: { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: runId, round_id: 'round-1' },
    actor: { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: { content: 'final answer', parts: [], metadata: { status: 'completed' }, legacy: true, phase: 'unknown' },
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  };
}

function wireRealSessionApplication(
  harness: ReturnType<typeof makeHarness>,
  acceptTerminalRunId?: (runId: string | null | undefined) => boolean,
) {
  // The REAL AgentDetail consumption path: the extracted production applier
  // owns envelope reduction, application facts, and legacy projection alike
  // — no test-only mirror of the contract may drift from the page wiring.
  const refs: SessionTranscriptApplierRefs = {
    transcriptEvents: {},
    eventStores: {},
    fullHydrationKeys: new Set(),
    replayStates: {},
    compatibilityTimelines: {},
    visibilityBoundaries: {},
    uiStates: {},
    runtimeActivityAt: {},
    pendingUserMessages: {},
  };
  harness.dependencies.applyTranscriptToSession = vi.fn((agentId, sessionId, event, isActiveRuntime) => (
    applyTranscriptToSessionRuntime({
      refs,
      markActiveRunTerminal: (key, runId) => {
        harness.dependencies.markActiveRunTerminal(key, runId);
        // The registry acceptance contract: only an explicit false rejects.
        return acceptTerminalRunId ? acceptTerminalRunId(runId) : true;
      },
      isTerminalTranscriptToolMessage: () => false,
      mergePendingMessages: (_key, messages) => messages,
      setChatMessagesSessionId: () => undefined,
      enqueueChatMessagesUpdate: (sessionId, updater) => {
        harness.dependencies.enqueueChatMessagesUpdate(sessionId, updater);
      },
      setChatMessagesAfterQueued: (sessionId, updater) => {
        harness.dependencies.setChatMessagesAfterQueued(sessionId, updater);
      },
      setActivePhase: () => undefined,
      setIsWaiting: () => undefined,
      setIsStreaming: () => undefined,
      parseChatMsg: (message) => message,
    }, agentId, sessionId, event, isActiveRuntime)
  ));
  return {
    store: (): SessionEventStore | undefined => refs.eventStores['agent-1:session-1'],
    replayMessages: () => refs.replayStates['agent-1:session-1']?.messages ?? [],
  };
}

describe('session socket event projector', () => {
  it('projects canonical SessionEventV2 without terminal array hydration', () => {
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-1',
      sequence: 7,
      item_id: 'run-1',
      item_kind: 'run',
      lifecycle: 'completed',
      kind: 'run.completed',
      payload_schema: 'hive.session.payload.run.completed.v2',
      tenant_id: 'tenant-1',
      scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' },
      actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' },
      payload: {},
      occurred_at: '2026-07-16T00:00:00Z',
      persisted_at: '2026-07-16T00:00:00Z',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledWith(
      'agent-1',
      'session-1',
      expect.objectContaining({ event_id: 'event-1', kind: 'run.completed' }),
      true,
    );
    expect(harness.dependencies.selectSession).not.toHaveBeenCalled();
  });

  it('projects a live compatibility assistant_message instead of dropping it until refresh', () => {
    const harness = makeHarness({
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      reason: 'unprovable_scope',
      event_id: 'legacy-final-1',
      sequence: 48,
      session_id: 'session-1',
      run_id: 'run-1',
      legacy_event_type: 'assistant_message',
      legacy_kind: 'assistant_final',
      legacy_lifecycle: 'completed',
      payload: {
        content: 'This answer must appear without a page refresh.',
        metadata: { legacy_run_id: 'run-1' },
      },
    });
    let store: SessionEventStore | undefined;
    let replay = createEmptyTranscriptReplayState();
    harness.dependencies.applyTranscriptToSession = vi.fn((_agentId, _sessionId, event) => {
      const consumed = consumeSessionEnvelope(event, store, 47);
      store = consumed.store;
      replay = applyTranscriptEvent(replay, consumed.projectionEvent);
      return true;
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledWith(
      'agent-1',
      'session-1',
      expect.objectContaining({
        id: 'legacy-final-1',
        event_type: 'assistant_message',
        content: 'This answer must appear without a page refresh.',
      }),
      true,
    );
    expect(store?.highestContiguousSequence).toBe(48);
    expect(replay.messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: 'This answer must appear without a page refresh.',
      }),
    ]);
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
  });

  it('invalidates the live task ledger when a canonical task tool event arrives', () => {
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-task-1',
      sequence: 8,
      item_id: 'tool-call-1',
      item_kind: 'tool_call',
      lifecycle: 'completed',
      kind: 'tool_call.completed',
      payload_schema: 'hive.session.payload.tool_call.completed.v2',
      tenant_id: 'tenant-1',
      scope: { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' },
      actor: { type: 'assistant' },
      visibility: { audience: 'direct_user' },
      payload: { tool_name: 'track_todo', arguments: { todo_id: 'task-1', status: 'completed' } },
      occurred_at: '2026-07-16T00:00:00Z',
      persisted_at: '2026-07-16T00:00:00Z',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledOnce();
    expect(harness.dependencies.shouldInvalidateToolCall).toHaveBeenCalledWith('agent-1:session-1');
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith(
      'agent-1',
      'session-1',
      false,
    );
  });

  it('routes legacy live commentary to the exact Session carried by the socket event', () => {
    const harness = makeHarness({ type: 'thinking', content: 'Inspecting the exact live Session path.' });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.targetedSessions()).toEqual(['session-1']);
    expect(harness.messages()).toEqual([
      expect.objectContaining({ thinking: 'Inspecting the exact live Session path.' }),
    ]);
  });

  it('refreshes session read models after a canonical tool result commits', () => {
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-tool-result-1',
      sequence: 9,
      item_id: 'tool-result-1',
      item_kind: 'tool_result',
      lifecycle: 'completed',
      kind: 'tool_result.completed',
      payload_schema: 'hive.session.payload.tool_result.completed.v2',
      tenant_id: 'tenant-1',
      scope: { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' },
      actor: { type: 'tool' },
      visibility: { audience: 'direct_user' },
      payload: { invocation_id: 'invocation-1', outcome: 'success' },
      occurred_at: '2026-07-16T00:00:01Z',
      persisted_at: '2026-07-16T00:00:01Z',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith(
      'agent-1',
      'session-1',
      false,
    );
  });

  it('refreshes run truth immediately when a canonical terminal run event arrives', () => {
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-run-terminal',
      sequence: 9,
      item_id: 'run-1',
      item_kind: 'run',
      lifecycle: 'completed',
      kind: 'run.completed',
      payload_schema: 'hive.session.payload.run.completed.v2',
      tenant_id: 'tenant-1',
      scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' },
      actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' },
      payload: {},
      occurred_at: '2026-07-16T00:00:00Z',
      persisted_at: '2026-07-16T00:00:00Z',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith(
      'agent-1',
      'session-1',
    );
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'done');
    expect(harness.dependencies.syncActivePhase).toHaveBeenCalledWith('done');
  });

  it('closes the active run with failed phase and the quota notice on a canonical runtime_failure event (DAY1-PROVIDER-402-TERMINAL-CONSUMPTION-001)', () => {
    const message = '[LLM Error] AI 模型额度或余额不足，请联系管理员检查账户余额、模型额度或切换模型。';
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-runtime-failure-1',
      sequence: 1,
      item_id: 'failure-item-1',
      item_kind: 'runtime_failure',
      lifecycle: 'recorded',
      kind: 'runtime_failure.recorded',
      payload_schema: 'hive.session.payload.runtime_failure.recorded.v2',
      tenant_id: 'tenant-1',
      scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' },
      actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' },
      payload: {
        status: 'failed',
        terminal_reason: 'provider_error',
        failure_code: 'quota_exhausted',
        delivery_state: 'rejected',
        requires_user_decision: true,
        retryable: true,
        content: message,
        message,
      },
      occurred_at: '2026-08-28T00:00:00Z',
      persisted_at: '2026-08-28T00:00:00Z',
    });
    // Drive the real consumption path so the single terminal acceptance
    // (applier onTerminal) and the projector refresh effects both run.
    wireRealSessionApplication(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    // The no-reload terminal consumption contract: close the active run once
    // (applier onTerminal), pin the failed phase, refresh runtime read
    // models, reconcile the durable transcript, and surface the existing
    // quota/余额 notice banner.
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'failed');
    expect(harness.dependencies.syncActivePhase).toHaveBeenCalledWith('failed');
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledWith(message);
  });

  function wireRealCanonicalDedupe(harness: ReturnType<typeof makeHarness>) {
    // The REAL AgentDetail dedupe contract through the production applier: a
    // duplicate delivery reports an empty application (false) — no effects.
    const wired = wireRealSessionApplication(harness);
    return () => wired.store();
  }

  it('executes runtime_failure terminal side effects exactly once for a duplicate canonical delivery (Codex finding: at-least-once idempotency)', () => {
    const harness = makeHarness(runtimeFailureEnvelope('run-1', 'event-runtime-failure-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    // The outbox may legally redeliver the same immutable event_id.
    projectSessionSocketEvent(harness.context, harness.dependencies);

    // One owner: the applier's onTerminal performs the single terminal
    // acceptance/active-run mutation for the accepted failure.
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
  });

  it('never clears, fails, or surfaces a stale run-1 runtime_failure against an active run-2 (Codex finding: run identity safety)', () => {
    const harness = makeHarness(runtimeFailureEnvelope('run-1', 'event-runtime-failure-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    // The durable event still enters the session projection, but no terminal
    // side effect may fire against the newer active run. The registry call
    // fires once (consumption callback) and records/rejects safely.
    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(harness.dependencies.setTransportNotice).not.toHaveBeenCalled();
    expect(harness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();
  });

  it('still executes the fresh run-scoped runtime_failure terminal consumption for the matching active run', () => {
    const harness = makeHarness(runtimeFailureEnvelope('run-1', 'event-runtime-failure-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'failed');
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledWith('quota message');
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
  });

  it.each([
    ['session', { level: 'session', session_id: 'session-1', thread_id: 'session-1' }],
    ['turn', { level: 'turn', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1' }],
    ['round', { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' }],
    ['blank run', { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: '' }],
  ])('never closes the active run for a %s-scoped runtime_failure live event (Codex finding 2)', (_level, scope) => {
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: `event-runtime-failure-${String(_level)}`,
      sequence: 10,
      item_id: `failure-item-${String(_level)}`,
      item_kind: 'runtime_failure',
      lifecycle: 'recorded',
      kind: 'runtime_failure.recorded',
      payload_schema: 'hive.session.payload.runtime_failure.recorded.v2',
      tenant_id: 'tenant-1',
      scope,
      actor: { type: 'runtime' },
      visibility: { audience: 'direct_user' },
      payload: {
        status: 'failed',
        terminal_reason: 'provider_error',
        failure_code: 'quota_exhausted',
        delivery_state: 'rejected',
        requires_user_decision: true,
        retryable: true,
        content: 'scoped failure',
        message: 'scoped failure',
      },
      occurred_at: '2026-08-28T00:00:00Z',
      persisted_at: '2026-08-28T00:00:00Z',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    // The event card still projects, but no terminal action fires: no active
    // run close (in particular no null run_id fallback that would clear an
    // unrelated active run), no failed phase, no quota notice.
    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalled();
    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(harness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
    expect(harness.dependencies.setTransportNotice).not.toHaveBeenCalled();
  });

  it('executes canonical run terminal side effects exactly once for a duplicate delivery (same-root-cause closure)', () => {
    const harness = makeHarness(runTerminalEnvelope('run-1', 'event-run-terminal-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
  });

  it('executes legacy assistant terminal side effects exactly once for a duplicate delivery (same-root-cause closure)', () => {
    const harness = makeHarness(legacyAssistantTerminalEnvelope('run-1', 'event-legacy-final-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledTimes(1);
  });

  it('invalidates runtime queries exactly once for a duplicate canonical tool_result delivery (same-root-cause closure)', () => {
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-tool-result-dup',
      sequence: 1,
      item_id: 'tool-result-dup',
      item_kind: 'tool_result',
      lifecycle: 'completed',
      kind: 'tool_result.completed',
      payload_schema: 'hive.session.payload.tool_result.completed.v2',
      tenant_id: 'tenant-1',
      scope: { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' },
      actor: { type: 'tool' },
      visibility: { audience: 'direct_user' },
      payload: { invocation_id: 'invocation-1', outcome: 'success' },
      occurred_at: '2026-08-28T00:00:00Z',
      persisted_at: '2026-08-28T00:00:00Z',
    });
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
  });

  it('performs zero side effects for a canonical envelope rejected during consumption (same-root-cause closure)', () => {
    const harness = makeHarness(runTerminalEnvelope('run-1'));
    harness.dependencies.applyTranscriptToSession = vi.fn(() => false);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(harness.dependencies.setTransportNotice).not.toHaveBeenCalled();
  });

  it('never applies a stale run-1 canonical run terminal against an active run-2 (same-root-cause closure)', () => {
    const harness = makeHarness(runTerminalEnvelope('run-1'));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    // The durable event still projects, but no terminal phase/refresh may
    // fire against the newer active run.
    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();
    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
  });

  it('never applies a stale run-1 legacy assistant terminal against an active run-2 (same-root-cause closure)', () => {
    const harness = makeHarness(legacyAssistantTerminalEnvelope('run-1'));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(harness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
  });

  it('still applies a canonical run terminal for the matching active run', () => {
    const harness = makeHarness(runTerminalEnvelope('run-1', 'event-run-terminal-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'done');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
  });

  it('still applies a legacy assistant terminal for the matching active run', () => {
    const harness = makeHarness(legacyAssistantTerminalEnvelope('run-1', 'event-legacy-final-1', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealCanonicalDedupe(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'done');
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
  });

  it('clears the active run and runtime read models when the legacy-adapted canonical assistant terminal arrives (DAY1-KNOWLEDGE-UI-TRUTH-001)', () => {
    // Production Run2 fresh-retry shape: the web-chat assistant_message
    // finalizer settles the RuntimeTask and its transcript event arrives as a
    // canonical envelope adapted from the legacy row (payload.legacy). No
    // run.completed item event follows on this path, so this terminal witness
    // must itself clear the active run and invalidate the workbench read
    // models the right panel renders from. The single active-run clearing is
    // owned by the applier's onTerminal; the projector refreshes read models.
    const terminalEnvelope = {
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-150',
      sequence: 1,
      item_id: 'assistant-final-1',
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      tenant_id: 'tenant-1',
      scope: {
        level: 'round',
        session_id: 'session-1',
        thread_id: 'session-1',
        turn_id: 'turn-1',
        run_id: 'run-5ab9b0a8',
        round_id: 'round-9',
      },
      actor: { type: 'assistant' },
      visibility: { audience: 'direct_user' },
      payload: {
        content: '升级颜色为琥珀色，续订间隔 37 天。',
        parts: [],
        metadata: { status: 'completed' },
        legacy: true,
        phase: 'unknown',
      },
      occurred_at: '2026-08-27T13:47:35.453028Z',
      persisted_at: '2026-08-27T13:47:35.453028Z',
    };
    const harness = makeHarness(terminalEnvelope);
    wireRealSessionApplication(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith(
      'agent-1:session-1',
      'run-5ab9b0a8',
    );
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'done');
    expect(harness.dependencies.syncActivePhase).toHaveBeenCalledWith('done');

    // The invalidation above exists so the refetched workbench read model
    // clears the stale running row. Connect that consumption in the same test:
    // the pre-terminal snapshot is exactly what kept rendering 运行中, and the
    // post-terminal refetch shape must leave the running state.
    const stalePanel = buildSessionRightPanelModel({
      messages: [],
      sessionWorkbench: {
        runtime_sections: {
          runs: [
            {
              runtime_task_id: 'run-5ab9b0a8',
              id: 'run-5ab9b0a8',
              runtime_kind: 'runtime_task',
              label: 'Web chat turn',
              status: 'running',
            },
          ],
        },
      },
    });
    expect(stalePanel.runtimeConsole.summary.runningCount).toBe(1);
    expect(stalePanel.runtimeConsole.summary.state).toBe('running');

    const refreshedPanel = buildSessionRightPanelModel({
      messages: [],
      sessionWorkbench: {
        runtime_sections: {
          runs: [
            {
              runtime_task_id: 'run-5ab9b0a8',
              id: 'run-5ab9b0a8',
              runtime_kind: 'runtime_task',
              label: 'Web chat turn',
              status: 'completed',
            },
          ],
        },
      },
    });
    expect(refreshedPanel.runtimeConsole.summary.runningCount).toBe(0);
    expect(refreshedPanel.runtimeConsole.summary.state).toBe('idle');
    expect(refreshedPanel.runs.items[0]?.status).toBe('completed');
  });

  it('does not terminalize a native V2 assistant item completion mid-run', () => {
    // Native V2 turns own run terminality through the run item lifecycle; a
    // mid-run assistant message completing must not clear the active run.
    const harness = makeHarness({
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: 'event-native-text',
      sequence: 21,
      item_id: 'assistant-text-1',
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      tenant_id: 'tenant-1',
      scope: {
        level: 'round',
        session_id: 'session-1',
        thread_id: 'session-1',
        turn_id: 'turn-1',
        run_id: 'run-native',
        round_id: 'round-2',
      },
      actor: { type: 'assistant' },
      visibility: { audience: 'direct_user' },
      payload: { content: 'Interim synthesis before the next tool round.', phase: 'unknown' },
      occurred_at: '2026-08-27T13:40:00Z',
      persisted_at: '2026-08-27T13:40:00Z',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
  });

  it('maps failed and cancelled legacy assistant terminal lifecycles to their terminal phases', () => {
    for (const lifecycle of ['failed', 'cancelled'] as const) {
      const harness = makeHarness({
        schema: 'hive.session_event',
        schema_version: 2,
        event_id: `event-assistant-${lifecycle}`,
        sequence: 1,
        item_id: `assistant-${lifecycle}-1`,
        item_kind: 'assistant_final',
        kind: `assistant_final.${lifecycle}`,
        lifecycle,
        payload_schema: `hive.session.payload.assistant_final.${lifecycle}.v2`,
        tenant_id: 'tenant-1',
        scope: {
          level: 'round',
          session_id: 'session-1',
          thread_id: 'session-1',
          turn_id: 'turn-1',
          run_id: 'run-5ab9b0a8',
          round_id: 'round-9',
        },
        actor: { type: 'assistant' },
        visibility: { audience: 'direct_user' },
        payload: { content: '', legacy: true, phase: 'final' },
        occurred_at: '2026-08-27T13:47:35Z',
        persisted_at: '2026-08-27T13:47:35Z',
      });
      wireRealSessionApplication(harness);

      projectSessionSocketEvent(harness.context, harness.dependencies);

      expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
      expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-5ab9b0a8');
      expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
      const expectedPhase = lifecycle === 'failed' ? 'failed' : 'cancelled';
      expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', expectedPhase);
    }
  });

  it('refreshes runtime read models when a raw legacy terminal transcript frame arrives', () => {
    const harness = makeHarness({
      sequence: 150,
      event_type: 'assistant_message',
      run_id: 'run-5ab9b0a8',
      content: '升级颜色为琥珀色，续订间隔 37 天。',
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
  });

  it('refreshes runtime read models when a compatibility terminal transcript event arrives', () => {
    const harness = makeHarness({
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      reason: 'unprovable_scope',
      event_id: 'legacy-terminal-1',
      sequence: 60,
      session_id: 'session-1',
      run_id: 'run-5ab9b0a8',
      legacy_event_type: 'assistant_message',
      payload: { content: 'Reconciled final answer.', metadata: {} },
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
  });

  it('closes a background session socket only after a terminal stream event is durably projected', () => {
    const harness = makeHarness({ type: 'done', content: 'complete', run_id: 'run-1' }, false);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith(
      'agent-1:session-1',
      'run-1',
    );
    expect(harness.closeSessionSocket).toHaveBeenCalledWith('agent-1:session-1', true);
    expect(harness.dependencies.selectSession).not.toHaveBeenCalled();
  });

  it('reconciles the authoritative transcript after a live terminal done frame for the active runtime', () => {
    const harness = makeHarness(
      {
        type: 'done',
        content: 'Final answer references the persisted blueprint preview card.',
        run_id: 'run-1',
      },
      true,
    );

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
  });

  it('does not reconcile the transcript when a background session socket terminally closes', () => {
    const harness = makeHarness({ type: 'done', content: 'complete', run_id: 'run-1' }, false);

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
  });

  it('keeps typed platform errors out of assistant-authored messages', () => {
    const harness = makeHarness({
      type: 'session.error',
      error: { code: 'auth_failed', retryable: false, message_key: 'session.auth_failed' },
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.failAuthentication).toHaveBeenCalledWith('agent-1:session-1', false);
    expect(harness.messages()).toEqual([]);
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledWith('session.auth_failed');
  });
});

describe('session socket projector contiguous-application side effects (Codex REQUEST_CHANGES #3)', () => {
  function canonicalTextEvent(sequence: number, content: string): Record<string, unknown> {
    return {
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: `event-text-${sequence}`,
      sequence,
      item_id: `text-item-${sequence}`,
      item_kind: 'assistant_text',
      lifecycle: 'delta',
      kind: 'assistant_text.delta',
      payload_schema: 'hive.session.payload.assistant_text.delta.v2',
      tenant_id: 'tenant-1',
      scope: {
        level: 'round',
        session_id: 'session-1',
        thread_id: 'session-1',
        turn_id: 'turn-1',
        run_id: 'run-1',
        round_id: 'round-1',
      },
      actor: { type: 'assistant' },
      visibility: { audience: 'direct_user' },
      payload: { content, phase: 'unknown' },
      occurred_at: '2026-08-28T00:00:00Z',
      persisted_at: '2026-08-28T00:00:00Z',
    };
  }

  function bufferedToolResultEvent(sequence: number): Record<string, unknown> {
    return {
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: `event-buffered-tool-result-${sequence}`,
      sequence,
      item_id: `buffered-tool-result-${sequence}`,
      item_kind: 'tool_result',
      lifecycle: 'completed',
      kind: 'tool_result.completed',
      payload_schema: 'hive.session.payload.tool_result.completed.v2',
      tenant_id: 'tenant-1',
      scope: {
        level: 'round',
        session_id: 'session-1',
        thread_id: 'session-1',
        turn_id: 'turn-1',
        run_id: 'run-1',
        round_id: 'round-1',
      },
      actor: { type: 'tool' },
      visibility: { audience: 'direct_user' },
      payload: { invocation_id: 'invocation-buffered', outcome: 'success' },
      occurred_at: '2026-08-28T00:00:00Z',
      persisted_at: '2026-08-28T00:00:00Z',
    };
  }

  function compatibilityFillerEvent(sequence: number): Record<string, unknown> {
    return {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      reason: 'legacy_generation',
      event_id: `legacy-filler-${sequence}`,
      sequence,
      legacy_event_type: 'phase',
      payload: { content: '', metadata: { phase: 'done' } },
    };
  }

  it('runs a buffered runtime_failure terminal exactly once after gap close and never on arrival or duplicate', () => {
    const harness = makeHarness(runtimeFailureEnvelope('run-1', 'event-runtime-failure-3', 3));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealSessionApplication(harness);

    // Arrival with seq 1 and 2 missing: the failure is only buffered.
    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.setTransportNotice).not.toHaveBeenCalled();
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();

    // Gap close drains seq 1 then seq 2; the drained failure fires exactly once.
    projectSessionSocketEvent(
      { ...harness.context, data: canonicalTextEvent(1, 'gap filler one') },
      harness.dependencies,
    );
    projectSessionSocketEvent(
      { ...harness.context, data: canonicalTextEvent(2, 'gap filler two') },
      harness.dependencies,
    );
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'failed');
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledTimes(1);

    // At-least-once redelivery of the drained failure is a duplicate: zero.
    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledTimes(1);
  });

  it('invalidates a buffered tool_result exactly once after gap close and never on arrival', () => {
    const harness = makeHarness(bufferedToolResultEvent(2));
    wireRealSessionApplication(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();

    projectSessionSocketEvent(
      { ...harness.context, data: canonicalTextEvent(1, 'gap filler') },
      harness.dependencies,
    );
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1', false);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
  });

  it('surfaces drained canonical terminal effects when a compatibility carrier fills the gap', () => {
    // Canonical run.completed sits buffered at seq 2; a compatibility envelope
    // at seq 1 fills the gap and drains it in the same transition.
    const harness = makeHarness(compatibilityFillerEvent(1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    const wired = wireRealSessionApplication(harness);

    projectSessionSocketEvent(
      { ...harness.context, data: runTerminalEnvelope('run-1', 'event-run-terminal-2', 2) },
      harness.dependencies,
    );
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(wired.store()?.highestContiguousSequence).toBe(2);
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'done');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
  });

  it('performs zero effects for a same-sequence different-event conflict while the typed incident remains', () => {
    const harness = makeHarness(runtimeFailureEnvelope('run-1', 'event-runtime-failure-conflict', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    const wired = wireRealSessionApplication(harness);

    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);

    const conflicting = runtimeFailureEnvelope('run-1', 'event-runtime-failure-conflicting', 1);
    projectSessionSocketEvent({ ...harness.context, data: conflicting }, harness.dependencies);

    expect(wired.store()?.consistencyIncident).toMatchObject({
      sequence: 1,
      existingEventId: 'event-runtime-failure-conflict',
      incomingEventId: 'event-runtime-failure-conflicting',
    });
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setTransportNotice).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
  });

  it('performs zero effects for a contiguous event ignored because the item was already terminal', () => {
    // seq 1 completes the legacy-adapted assistant item (terminal effects fire
    // once); seq 2 targets the same item with a new event id and lands only in
    // ignoredEventIds — zero new effects.
    const harness = makeHarness(legacyAssistantTerminalEnvelope('run-1', 'event-legacy-final-first', 1));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    const wired = wireRealSessionApplication(harness);
    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);

    const ignoredEvent = {
      ...legacyAssistantTerminalEnvelope('run-1', 'event-legacy-final-ignored', 2),
      // Same durable item as seq 1 — only the event id differs.
      item_id: 'assistant-item-event-legacy-final-first',
    };
    projectSessionSocketEvent({ ...harness.context, data: ignoredEvent }, harness.dependencies);

    expect(wired.store()?.ignoredEventIds).toEqual(['event-legacy-final-ignored']);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.setSessionPhase).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledTimes(1);
  });

  it('performs zero effects for a contiguous event ignored because its ordinal is stale', () => {
    // seq 1 is a non-terminal delta with ordinal 5; seq 2 regresses the
    // ordinal on the same item and lands only in ignoredEventIds.
    const firstEvent = {
      ...legacyAssistantTerminalEnvelope('run-1', 'event-legacy-final-open', 1),
      lifecycle: 'delta',
      kind: 'assistant_text.delta',
      payload_schema: 'hive.session.payload.assistant_text.delta.v2',
      payload: { content: 'live progress bytes', parts: [], metadata: {}, legacy: true, phase: 'unknown' },
      ordinal: 5,
    };
    const harness = makeHarness(firstEvent);
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    const wired = wireRealSessionApplication(harness);
    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();

    const ignoredEvent = {
      ...firstEvent,
      event_id: 'event-legacy-final-stale',
      sequence: 2,
      ordinal: 0,
      payload: { content: 'stale ordinal bytes', parts: [], metadata: {}, legacy: true, phase: 'unknown' },
    };
    projectSessionSocketEvent({ ...harness.context, data: ignoredEvent }, harness.dependencies);

    expect(wired.store()?.ignoredEventIds).toEqual(['event-legacy-final-stale']);
    expect(harness.dependencies.markActiveRunTerminal).not.toHaveBeenCalled();
    expect(harness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
  });

  it('never lets a drained run-1 terminal mutate an active run-2 while the matching run stays positive', () => {
    const staleHarness = makeHarness(canonicalTextEvent(1, 'gap filler'));
    staleHarness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    wireRealSessionApplication(staleHarness);
    projectSessionSocketEvent(
      { ...staleHarness.context, data: runTerminalEnvelope('run-1', 'event-stale-run-terminal-2', 2) },
      staleHarness.dependencies,
    );

    projectSessionSocketEvent(staleHarness.context, staleHarness.dependencies);

    expect(staleHarness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(staleHarness.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(staleHarness.dependencies.fetchMySessions).not.toHaveBeenCalled();
    // The drained stale terminal still reaches the identity-safe terminal
    // registry call (production markActiveRunTerminalInRegistry records and
    // rejects it without clearing run-2); no terminal EFFECT may fire.
    expect(staleHarness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(staleHarness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');

    const matchingHarness = makeHarness(canonicalTextEvent(1, 'gap filler'));
    matchingHarness.dependencies.activeRunIdOf = vi.fn(() => 'run-1');
    wireRealSessionApplication(matchingHarness);
    projectSessionSocketEvent(
      { ...matchingHarness.context, data: runTerminalEnvelope('run-1', 'event-matching-run-terminal-2', 2) },
      matchingHarness.dependencies,
    );

    projectSessionSocketEvent(matchingHarness.context, matchingHarness.dependencies);

    expect(matchingHarness.dependencies.setSessionPhase).toHaveBeenCalledWith('agent-1:session-1', 'done');
    expect(matchingHarness.dependencies.syncActivePhase).toHaveBeenCalledWith('done');
    expect(matchingHarness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
  });

  it('runs drained compatibility terminal effects exactly once when a canonical carrier closes the gap (Codex finding C)', () => {
    // A compatibility run_completed envelope buffers behind sequence 1; the
    // canonical carrier at sequence 1 closes the gap and drains it.
    const harness = makeHarness(canonicalTextEvent(1, 'gap filler'));
    const bufferedTerminal = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      reason: 'legacy_generation',
      event_id: 'legacy-run-completed-2',
      sequence: 2,
      legacy_event_type: 'run_completed',
      payload: { content: '', metadata: {}, legacy_run_id: 'run-1' },
    };
    wireRealSessionApplication(harness);

    projectSessionSocketEvent({ ...harness.context, data: bufferedTerminal }, harness.dependencies);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);

    // At-least-once redelivery of the drained envelope: zero effects.
    projectSessionSocketEvent({ ...harness.context, data: bufferedTerminal }, harness.dependencies);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);
  });
});

describe('session socket projector terminal run-identity safety (Codex REQUEST_CHANGES #4 finding D)', () => {
  function compatibilityTerminalEnvelope(sequence: number, runId: string): Record<string, unknown> {
    return {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      reason: 'legacy_generation',
      event_id: `legacy-terminal-${sequence}-${runId}`,
      sequence,
      session_id: 'session-1',
      run_id: runId,
      legacy_event_type: 'assistant_message',
      payload: { content: `LEGACY FINAL ${runId}`, legacy_run_id: runId, metadata: {} },
    };
  }

  function rawTerminalFrame(sequence: number, runId: string): Record<string, unknown> {
    return {
      sequence,
      transcript_event_id: `raw-terminal-${sequence}-${runId}`,
      event_type: 'assistant_message',
      run_id: runId,
      content: `RAW FINAL ${runId}`,
    };
  }

  it('runs zero terminal effects for a stale compatibility run-1 terminal while run-2 is active, and once for the matching run-2 terminal', () => {
    const staleHarness = makeHarness(compatibilityTerminalEnvelope(1, 'run-1'));
    staleHarness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');

    projectSessionSocketEvent(staleHarness.context, staleHarness.dependencies);

    expect(staleHarness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(staleHarness.dependencies.fetchMySessions).not.toHaveBeenCalled();
    expect(staleHarness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
    expect(staleHarness.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(staleHarness.dependencies.syncActivePhase).not.toHaveBeenCalled();

    const matchingHarness = makeHarness(compatibilityTerminalEnvelope(1, 'run-2'));
    matchingHarness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');

    projectSessionSocketEvent(matchingHarness.context, matchingHarness.dependencies);

    expect(matchingHarness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
    expect(matchingHarness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(matchingHarness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);
    expect(matchingHarness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');
  });

  it('runs zero terminal effects for a stale raw run-1 terminal frame while run-2 is active, through the real applier result', () => {
    const harness = makeHarness(rawTerminalFrame(1, 'run-1'));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    // Real consumption path with run-2 active registry semantics: the run-1
    // terminal is recorded but rejected for the active run.
    wireRealSessionApplication(harness, (runId) => runId !== 'run-1');

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(harness.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(harness.dependencies.fetchMySessions).not.toHaveBeenCalled();
    expect(harness.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
  });

  it('runs terminal effects exactly once for a matching raw run-2 terminal frame and zero times on redelivery', () => {
    const harness = makeHarness(rawTerminalFrame(1, 'run-2'));
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    wireRealSessionApplication(harness, (runId) => runId !== 'run-1');

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledWith(true, 'agent-1');

    // At-least-once redelivery: the real applier reports no new application,
    // so the terminal effects must not run again.
    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledTimes(1);
    expect(harness.dependencies.fetchMySessions).toHaveBeenCalledTimes(1);
  });

  it('records but performs zero active effects for stale run-1 terminal stream frames while run-2 is active', () => {
    // Terminal stream frames ride the non-transcript channel: the registry
    // still observes (and rejects) the stale run identity, but a rejected
    // frame must not set/sync terminal phase, invalidate/reconcile/fetch,
    // surface the stale error, or close/replace the active run-2 tail.
    const staleDone = makeHarness({ type: 'done', content: 'STALE RUN-1 DONE', run_id: 'run-1' });
    staleDone.dependencies.markActiveRunTerminal = vi.fn(() => false);
    projectSessionSocketEvent(staleDone.context, staleDone.dependencies);
    expect(staleDone.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(staleDone.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(staleDone.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(staleDone.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(staleDone.dependencies.reconcileSessionTranscript).not.toHaveBeenCalled();
    expect(staleDone.dependencies.fetchMySessions).not.toHaveBeenCalled();
    expect(staleDone.messages()).toEqual([]);

    const staleError = makeHarness({ type: 'error', content: 'STALE RUN-1 ERROR', run_id: 'run-1' });
    staleError.dependencies.markActiveRunTerminal = vi.fn(() => false);
    projectSessionSocketEvent(staleError.context, staleError.dependencies);
    expect(staleError.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(staleError.dependencies.setTransportNotice).not.toHaveBeenCalled();
    expect(staleError.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(staleError.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();

    const staleQuota = makeHarness({ type: 'quota_exceeded', content: 'STALE RUN-1 QUOTA', run_id: 'run-1' });
    staleQuota.dependencies.markActiveRunTerminal = vi.fn(() => false);
    projectSessionSocketEvent(staleQuota.context, staleQuota.dependencies);
    expect(staleQuota.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(staleQuota.dependencies.setTransportNotice).not.toHaveBeenCalled();
    expect(staleQuota.dependencies.setSessionPhase).not.toHaveBeenCalled();

    const staleCancelled = makeHarness({ type: 'run_cancelled', run_id: 'run-1' });
    staleCancelled.dependencies.markActiveRunTerminal = vi.fn(() => false);
    projectSessionSocketEvent(staleCancelled.context, staleCancelled.dependencies);
    expect(staleCancelled.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');
    expect(staleCancelled.dependencies.setSessionPhase).not.toHaveBeenCalled();
    expect(staleCancelled.dependencies.syncActivePhase).not.toHaveBeenCalled();
    expect(staleCancelled.dependencies.invalidateSessionRuntimeQueries).not.toHaveBeenCalled();
    expect(staleCancelled.dependencies.fetchMySessions).not.toHaveBeenCalled();
  });

  it('keeps full existing behavior for a matching run-2 terminal stream frame while run-2 is active', () => {
    const harness = makeHarness({ type: 'done', content: 'RUN-2 FINAL', run_id: 'run-2' });
    harness.dependencies.activeRunIdOf = vi.fn(() => 'run-2');
    projectSessionSocketEvent(harness.context, harness.dependencies);
    expect(harness.dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-2');
    expect(harness.dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(harness.messages().some((message) => message.content === 'RUN-2 FINAL')).toBe(true);
  });
});
