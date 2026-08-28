import { describe, expect, it } from 'vitest';

import {
  isTerminalRunAcceptedForActiveRun,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type SessionRunState,
} from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import {
  applyCanonicalSessionSnapshot,
  consumeSessionEnvelope,
  hydrateSessionTranscriptEvents,
  mergeCanonicalTerminalMessages,
  projectCanonicalSessionSnapshot,
  projectSessionEventStoreToMessages,
} from './sessionEventConsumer';
import {
  sessionPayloadContent,
  type SessionCompatibilityEvent,
  type SessionEventStore,
  type SessionEventV2,
} from '../session-workbench/sessionEventStore';
import { normalizeThreadItemPayload } from '../session-workbench/threadItemReducer';
import { shouldRenderThreadItemInConversation } from '../session-workbench/ThreadItemRenderer';

function event(sequence: number, lifecycle: 'started' | 'delta' | 'completed'): SessionEventV2 {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-${sequence}`,
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
    item_id: 'assistant-1',
    item_kind: 'assistant_text',
    kind: `assistant_text.${lifecycle}`,
    lifecycle,
    payload_schema: `hive.session.payload.assistant_text.${lifecycle}.v2`,
    actor: { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: { phase: 'unknown', content: lifecycle === 'delta' ? 'exact bytes' : '' },
    occurred_at: '2026-07-16T00:00:00Z',
    persisted_at: '2026-07-16T00:00:00Z',
  };
}

function replay(events: SessionEventV2[]): SessionEventStore {
  let store: SessionEventStore | undefined;
  for (const envelope of events) {
    store = consumeSessionEnvelope(
      envelope as unknown as ChatTranscriptEventPayload,
      store,
      0,
    ).store;
  }
  if (!store) throw new Error('fixture_did_not_create_store');
  return store;
}

describe('canonical Session event consumer', () => {
  it('seals the already-visible live process with the canonical final without rebuilding gapped history', () => {
    const user: AgentChatMessage = {
      id: 'input-live-1',
      role: 'user',
      content: 'Run the production canary.',
    };
    const liveProgress: AgentChatMessage = {
      id: 'live-progress-1',
      role: 'assistant',
      content: 'LIVE_TERMINAL_PROCESS_0718',
      eventType: 'assistant_commentary',
      eventStatus: 'completed',
    };
    const unrelatedHistoricalFinal: AgentChatMessage = {
      id: 'historical-final-1',
      role: 'assistant',
      content: 'Earlier answer.',
      eventType: 'assistant_message',
    };
    const canonicalFinal: AgentChatMessage = {
      id: 'canonical-final-1',
      role: 'assistant',
      content: 'LIVE_TERMINAL_FINAL_0718',
      sessionItem: {
        id: 'assistant-final-item-1',
        kind: 'assistant_final',
        scope: {
          level: 'run',
          session_id: 'session-1',
          thread_id: 'session-1',
          turn_id: 'turn-1',
          run_id: 'run-1',
        },
        lifecycle: 'completed',
        terminal: true,
        revision: 1,
        content: 'LIVE_TERMINAL_FINAL_0718',
        payload: {},
        actor: { type: 'assistant' },
        visibility: { audience: 'direct_user' },
        occurredAt: '2026-07-18T00:00:03Z',
        first_sequence: 3536,
        last_sequence: 3536,
      },
    };

    const merged = mergeCanonicalTerminalMessages(
      [unrelatedHistoricalFinal, user, liveProgress],
      [canonicalFinal],
      'run-1',
    );

    expect(merged).toEqual([
      unrelatedHistoricalFinal,
      user,
      liveProgress,
      canonicalFinal,
    ]);
    expect(buildRunTimelineFromMessages(merged.slice(2))).toMatchObject({
      status: 'done',
      steps: [expect.objectContaining({ kind: 'commentary' })],
    });
  });

  it('uses one canonical render owner when the compatibility final arrived first', () => {
    const prior = [
      { id: 'input-1', role: 'user', content: 'Do it.' },
      {
        id: 'progress-1',
        role: 'assistant',
        content: 'Checking production.',
        eventType: 'assistant_commentary',
      },
      {
        id: 'legacy-final-event-1',
        role: 'assistant',
        content: 'Exact final bytes.',
        eventType: 'assistant_message',
      },
    ] as AgentChatMessage[];
    const canonical = [{
      id: 'render-owner-1',
      role: 'assistant',
      content: 'Exact final bytes.',
      sessionItem: {
        id: 'final-item-1',
        kind: 'assistant_final',
        scope: {
          level: 'round',
          session_id: 'session-1',
          thread_id: 'session-1',
          turn_id: 'turn-1',
          run_id: 'run-1',
          round_id: 'round-1',
        },
        lifecycle: 'completed',
        terminal: true,
        revision: 1,
        content: 'Exact final bytes.',
        payload: {},
        actor: { type: 'assistant' },
        visibility: { audience: 'direct_user' },
        occurredAt: '2026-07-18T00:00:03Z',
        first_sequence: 100,
        last_sequence: 100,
      },
    }] as AgentChatMessage[];

    const merged = mergeCanonicalTerminalMessages(prior, canonical, 'run-1');

    expect(merged.filter((message) => message.content === 'Exact final bytes.')).toHaveLength(1);
    expect(merged.at(-1)).toMatchObject({
      id: 'render-owner-1',
      sessionItem: { kind: 'assistant_final' },
    });
    expect(merged).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'progress-1', eventType: 'assistant_commentary' }),
    ]));
  });

  it('seals compatibility process already visible beside a partial canonical tail', () => {
    const legacyThinking = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-thinking-1',
      sequence: 3400,
      reason: 'rolling_v1_projection',
      legacy_event_type: 'thinking',
      payload: {
        content: 'LIVE_TERMINAL_PROCESS_0718',
        metadata: { role: 'assistant' },
      },
    } as unknown as ChatTranscriptEventPayload;
    const source: SessionEventV2 = {
      ...event(3401, 'completed'),
      ordinal: undefined,
      item_id: 'terminal-source-1',
      payload: { phase: 'unknown', content: 'LIVE_TERMINAL_FINAL_0718' },
    };
    const final: SessionEventV2 = {
      ...event(3402, 'completed'),
      ordinal: undefined,
      item_id: 'terminal-final-1',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      payload: {
        phase: 'final',
        render_owner_id: 'terminal-owner-1',
        source_blocks: [{ item_id: 'terminal-source-1', block_index: 0, content_hash: 'hash-1' }],
      },
    };
    const runCompleted: SessionEventV2 = {
      ...event(3403, 'completed'),
      ordinal: undefined,
      item_id: 'run-1',
      item_kind: 'run',
      kind: 'run.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.run.completed.v2',
      scope: {
        level: 'run',
        session_id: 'session-1',
        thread_id: 'session-1',
        turn_id: 'turn-1',
        run_id: 'run-1',
      },
      actor: { type: 'runtime' },
      payload: {},
    };
    const terminalCommitted: SessionEventV2 = {
      ...event(3404, 'completed'),
      ordinal: undefined,
      item_id: 'outcome-1',
      item_kind: 'run_outcome',
      kind: 'run_outcome.terminal_committed',
      lifecycle: 'terminal_committed',
      payload_schema: 'hive.session.payload.run_outcome.terminal_committed.v2',
      scope: {
        level: 'run',
        session_id: 'session-1',
        thread_id: 'session-1',
        turn_id: 'turn-1',
        run_id: 'run-1',
      },
      actor: { type: 'runtime' },
      payload: { outcome_id: 'outcome-1', terminal_result_id: 'result-1', terminal_event_count: 4 },
    } as SessionEventV2;
    const transcriptEvents = [
      legacyThinking,
      source as unknown as ChatTranscriptEventPayload,
      final as unknown as ChatTranscriptEventPayload,
      runCompleted as unknown as ChatTranscriptEventPayload,
      terminalCommitted as unknown as ChatTranscriptEventPayload,
    ];
    let store: SessionEventStore | undefined;
    for (const envelope of transcriptEvents) {
      store = consumeSessionEnvelope(envelope, store, 3399).store;
    }
    if (!store) throw new Error('fixture_did_not_create_store');
    let projectedMessages = [{
      id: 'legacy-thinking-message-1',
      role: 'assistant',
      content: '',
      thinking: 'LIVE_TERMINAL_PROCESS_0718',
    }] as ReturnType<typeof projectSessionEventStoreToMessages>;

    applyCanonicalSessionSnapshot({
      events: [runCompleted],
      store,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: () => undefined,
      onMessages: (messages, terminal, runId) => {
        projectedMessages = terminal
          ? mergeCanonicalTerminalMessages(projectedMessages, messages, runId)
          : messages;
      },
    });

    expect(projectedMessages).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'assistant', thinking: 'LIVE_TERMINAL_PROCESS_0718' }),
      expect.objectContaining({ role: 'assistant', content: 'LIVE_TERMINAL_FINAL_0718' }),
    ]));
    expect(buildRunTimelineFromMessages(projectedMessages)).toMatchObject({
      status: 'done',
      steps: [expect.objectContaining({ kind: 'reasoning' })],
    });

    let terminalMetadataProjected = false;
    applyCanonicalSessionSnapshot({
      events: [terminalCommitted],
      store,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: () => undefined,
      onMessages: () => { terminalMetadataProjected = true; },
    });
    expect(terminalMetadataProjected).toBe(false);
    expect(buildRunTimelineFromMessages(projectedMessages)).toMatchObject({
      status: 'done',
      steps: [expect.objectContaining({ kind: 'reasoning' })],
    });
  });

function runtimeFailureEvent(scope: Record<string, unknown>, sequence = 1): SessionEventV2 {
  const message = '[LLM Error] AI 模型额度或余额不足，请联系管理员检查账户余额、模型额度或切换模型。';
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-runtime-failure-${sequence}`,
    sequence,
    tenant_id: 'tenant-1',
    scope,
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
      content: message,
      message,
    },
    display: { title: 'Run failed' },
    occurred_at: '2026-08-28T00:00:00Z',
    persisted_at: '2026-08-28T00:00:00Z',
  } as unknown as SessionEventV2;
}

describe('canonical runtime_failure consumption (DAY1-PROVIDER-402-TERMINAL-CONSUMPTION-001)', () => {
  const runScope = { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' };

  it('treats a canonical runtime_failure event as the run terminal witness on live and replay', () => {
    const failureEvent = runtimeFailureEvent(runScope);
    const store = replay([failureEvent]);

    // Replay/hydration renders the persisted terminal event card — the
    // failure stays recoverable after a reload without any assistant message.
    const messages = projectSessionEventStoreToMessages(store);
    expect(messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'event', eventType: 'runtime_failure', eventTitle: 'Run failed' }),
    ]));
    expect(messages.some((entry) => entry.role === 'assistant')).toBe(false);

    // The canonical snapshot seals the run terminal with its typed run id.
    let terminalRunId: string | null | undefined;
    let terminalFlag: boolean | undefined;
    applyCanonicalSessionSnapshot({
      events: [failureEvent],
      store,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: (runId) => { terminalRunId = runId; },
      onMessages: (_messages, terminal) => { terminalFlag = terminal; },
    });
    expect(terminalRunId).toBe('run-1');
    expect(terminalFlag).toBe(true);
  });

  it('projects a replayed canonical runtime_failure as a visible user error card with the safe quota message (Codex finding 1)', () => {
    const store = replay([runtimeFailureEvent(runScope)]);
    const messages = projectSessionEventStoreToMessages(store);
    const failureMessage = messages.find((entry) => entry.eventType === 'runtime_failure');
    expect(failureMessage).toBeDefined();
    expect(messages.some((entry) => entry.role === 'assistant')).toBe(false);

    // The exact AgentChatSection render seam: msg.threadItem wins, otherwise
    // the hand-picked legacy subset normalizes through the thread-item map.
    const item = failureMessage!.threadItem || normalizeThreadItemPayload({
      id: failureMessage!.transcriptEventId || failureMessage!.id || 'legacy-event-0',
      eventType: failureMessage!.eventType,
      content: failureMessage!.content,
      status: failureMessage!.eventStatus,
      title: failureMessage!.eventTitle,
      created_at: failureMessage!.timestamp,
    });
    expect(item).not.toBeNull();
    expect(shouldRenderThreadItemInConversation(item!, false)).toBe(true);
    expect(item!.item_type).toBe('error');
    expect(item!.item_status).toBe('failed');
    // The card itself carries the safe humanized quota message — never a
    // generic summary and never NL-scanned content.
    expect(item!.user_summary).toContain('额度或余额不足');
    expect(item!.item_data).toMatchObject({
      code: 'quota_exhausted',
      reason: 'provider_error',
      retryable: true,
    });
  });

  it('never clears or terminal-merges an active run-2 when a stale run-1 runtime_failure replays through the real snapshot seam (Codex path proof)', () => {
    const failureEvent = runtimeFailureEvent(runScope);
    const store = replay([failureEvent]);
    // Real active-run state: run-2 is the currently active run in this session.
    const activeRuns: Record<string, SessionRunState> = {
      'agent-1:session-1': { runId: 'run-2', status: 'running' } as SessionRunState,
    };
    const clearedRunIds: Array<string | null> = [];
    const messageMerges: Array<{ terminal: boolean; runId: string | null }> = [];

    applyCanonicalSessionSnapshot({
      events: [failureEvent],
      store,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: (runId) => {
        // The exact AgentDetail markActiveRunTerminal identity contract: a
        // nonempty stale terminal run id is recorded but never clears the
        // active run, and reports not-accepted.
        if (!isTerminalRunAcceptedForActiveRun(activeRuns['agent-1:session-1']?.runId ?? null, runId)) return false;
        clearedRunIds.push(runId);
        delete activeRuns['agent-1:session-1'];
        return true;
      },
      onMessages: (_messages, terminal, runId) => {
        messageMerges.push({ terminal, runId });
      },
    });

    expect(activeRuns['agent-1:session-1']?.runId).toBe('run-2');
    expect(clearedRunIds).toEqual([]);
    // The run-1 failure card may enter the durable projection, but it must
    // not seal or replace the active run-2 tail as a terminal merge — a
    // rejected terminal binds no run id at all (Codex finding E).
    expect(messageMerges).toEqual([{ terminal: false, runId: null }]);
  });

  it('still clears and terminal-merges the matching active run for a fresh run-scoped runtime_failure', () => {
    const failureEvent = runtimeFailureEvent(runScope);
    const store = replay([failureEvent]);
    const activeRuns: Record<string, SessionRunState> = {
      'agent-1:session-1': { runId: 'run-1', status: 'running' } as SessionRunState,
    };
    const clearedRunIds: Array<string | null> = [];
    const messageMerges: Array<{ terminal: boolean; runId: string | null }> = [];

    applyCanonicalSessionSnapshot({
      events: [failureEvent],
      store,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: (runId) => {
        if (!isTerminalRunAcceptedForActiveRun(activeRuns['agent-1:session-1']?.runId ?? null, runId)) return false;
        clearedRunIds.push(runId);
        delete activeRuns['agent-1:session-1'];
        return true;
      },
      onMessages: (_messages, terminal, runId) => {
        messageMerges.push({ terminal, runId });
      },
    });

    expect(activeRuns['agent-1:session-1']).toBeUndefined();
    expect(clearedRunIds).toEqual(['run-1']);
    expect(messageMerges).toEqual([{ terminal: true, runId: 'run-1' }]);
  });

  it.each([
    ['session', { level: 'session', session_id: 'session-1', thread_id: 'session-1' }],
    ['turn', { level: 'turn', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1' }],
    ['round', { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' }],
  ])('never terminates the whole run for a %s-scoped runtime_failure (Codex finding 2)', (_level, scope) => {
    const failureEvent = runtimeFailureEvent(scope);
    const store = replay([failureEvent]);

    const snapshot = projectCanonicalSessionSnapshot(
      failureEvent as unknown as ChatTranscriptEventPayload,
      store,
    );

    expect(snapshot.runTerminal).toBe(false);
    expect(snapshot.terminal).toBe(false);
    expect(snapshot.runId).toBeNull();
  });
});

  it('uses the backend canonical rendering contract for multipart user input', () => {
    expect(sessionPayloadContent({
      content_parts: [
        { type: 'text', text: '研究这个文件' },
        { type: 'file', z: 2, a: 'report.pdf' },
      ],
    })).toBe('[{"text":"研究这个文件","type":"text"},{"a":"report.pdf","type":"file","z":2}]');
  });

  it('uses the same highest-contiguous reducer for history, live, reconnect, and duplicate delivery', () => {
    const started = event(1, 'started');
    const delta = event(2, 'delta');
    const completed = event(3, 'completed');

    const history = replay([started, delta, completed]);
    const reconnect = replay([started, completed, delta, delta, completed]);

    expect(reconnect.items).toEqual(history.items);
    expect(reconnect.highestContiguousSequence).toBe(3);
    expect(reconnect.projection.phase).toBe('current');
    expect(reconnect.items['assistant-1']).toMatchObject({
      content: 'exact bytes',
      lifecycle: 'completed',
      terminal: true,
    });
  });

  it('advances live public commentary across a redacted provider-private continuity event', () => {
    const started = event(1, 'started');
    const privateContinuity: SessionEventV2 = {
      ...event(2, 'delta'),
      item_id: 'private-reasoning-1',
      item_kind: 'assistant_reasoning_private',
      kind: 'assistant_reasoning_private.delta',
      payload_schema: 'hive.session.payload.assistant_reasoning_private.delta.v2',
      visibility: {
        audience: 'private_provider',
        redacted_fields: ['/payload/content'],
      },
      payload: { phase: 'reasoning_private' },
    };
    const publicDelta = event(3, 'delta');

    const store = replay([started, privateContinuity, publicDelta]);
    const messages = projectSessionEventStoreToMessages(store);

    expect(store.highestContiguousSequence).toBe(3);
    expect(store.projection).toMatchObject({ phase: 'current', buffered_sequences: [] });
    expect(store.items['private-reasoning-1']).toMatchObject({
      content: '',
      visibility: { audience: 'private_provider' },
    });
    expect(messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'event', content: '', eventType: 'assistant_reasoning_private' }),
      expect.objectContaining({ role: 'assistant', content: 'exact bytes' }),
    ]));
    expect(JSON.stringify(messages)).not.toContain('provider-private-reasoning-secret');
  });

  it('projects canonical items from the shared reducer without reclassifying unknown text as final', () => {
    const assistantStarted = event(2, 'started');
    const assistantDelta = event(3, 'delta');
    const assistantCompleted = event(4, 'completed');
    const acceptedInput: SessionEventV2 = {
      ...event(1, 'completed'),
      ordinal: undefined,
      item_id: 'input-1',
      item_kind: 'human_input',
      kind: 'human_input.accepted',
      lifecycle: 'accepted',
      payload_schema: 'hive.session.payload.human_input.accepted.v2',
      scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
      actor: { type: 'user', id: 'user-1' },
      payload: { content_parts: [{ type: 'text', text: 'do the work' }], intent: 'start_turn' },
    };
    const finalEnvelope: SessionEventV2 = {
      ...event(5, 'completed'),
      item_id: 'final-1',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      payload: {
        phase: 'final',
        render_owner_id: 'render-owner-1',
        source_blocks: [
          { item_id: 'assistant-1', block_index: 0, content_hash: 'hash-1' },
        ],
      },
    };

    const store = replay([
      acceptedInput,
      assistantStarted,
      assistantDelta,
      assistantCompleted,
      finalEnvelope,
    ]);
    const messages = projectSessionEventStoreToMessages(store);

    expect(messages).toHaveLength(2);
    expect(messages[0]).toMatchObject({
      role: 'user',
      content: 'do the work',
      id: 'input-1',
      sessionItem: { kind: 'human_input' },
    });
    expect(messages[1]).toMatchObject({
      role: 'assistant',
      content: 'exact bytes',
      id: 'render-owner-1',
      sessionItem: {
        id: 'final-1',
        kind: 'assistant_final',
        lifecycle: 'completed',
      },
    });
    expect(messages.filter((message) => message.content === 'exact bytes')).toHaveLength(1);
  });

  it('keeps HumanInput item identity separate from the accepted/revised checkpoint event id across lifecycles', () => {
    const humanInputEvent = (sequence: number, lifecycle: string, payload: Record<string, unknown>): SessionEventV2 => ({
      ...event(sequence, 'completed'),
      ordinal: undefined,
      item_id: 'input-1',
      item_kind: 'human_input',
      kind: `human_input.${lifecycle}`,
      lifecycle,
      payload_schema: `hive.session.payload.human_input.${lifecycle}.v2`,
      scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
      actor: { type: 'user', id: 'user-1' },
      payload,
    } as SessionEventV2);
    const accepted = humanInputEvent(1, 'accepted', { content_parts: [{ type: 'text', text: 'J-06 hello' }] });
    const revised = humanInputEvent(2, 'revised', { content_parts: [{ type: 'text', text: 'J-06 hello v2' }] });
    const queued = humanInputEvent(3, 'queued', { state: 'queued' });
    const bound = humanInputEvent(4, 'bound', { round_id: 'round-1' });
    const applied = humanInputEvent(5, 'applied', { turn_id: 'turn-1' });

    const midStore = replay([accepted]);
    const midUser = projectSessionEventStoreToMessages(midStore).find((message) => message.role === 'user');
    expect(midUser).toMatchObject({
      id: 'input-1',
      transcriptEventId: 'event-1',
      content: 'J-06 hello',
    });

    const store = replay([accepted, revised, queued, bound, applied]);
    const messages = projectSessionEventStoreToMessages(store);
    const userMessages = messages.filter((message) => message.role === 'user');
    expect(userMessages).toHaveLength(1);
    expect(userMessages[0]).toMatchObject({
      id: 'input-1',
      transcriptEventId: 'event-2',
      content: 'J-06 hello v2',
    });
  });

  it('projects the assistant final transcriptEventId as the completed event id, not the item id', () => {
    const finalStarted = { ...event(1, 'started') };
    const finalCompleted: SessionEventV2 = {
      ...event(2, 'completed'),
      item_id: 'final-item-1',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      payload: {
        phase: 'final',
        render_owner_id: 'render-owner-1',
        zero_copy: true,
        source_blocks: [
          { item_id: 'assistant-1', block_index: 0, content_hash: 'hash-1' },
        ],
      },
    } as SessionEventV2;

    const store = replay([finalStarted, finalCompleted]);
    const messages = projectSessionEventStoreToMessages(store);
    const assistantFinal = messages.find((message) => message.role === 'assistant');

    // The branch/regenerate API anchors on an actual ChatTranscriptEvent id;
    // the completed event id is the durable anchor, never the item id.
    expect(assistantFinal).toMatchObject({
      id: 'render-owner-1',
      transcriptEventId: 'event-2',
    });
    expect(assistantFinal?.transcriptEventId).not.toBe('final-item-1');
  });

  it('projects a tool call and its exactly-one result as one stable timeline message', () => {
    const toolCall: SessionEventV2 = {
      ...event(1, 'started'),
      ordinal: undefined,
      item_id: 'tool-call-1',
      item_kind: 'tool_call',
      kind: 'tool_call.started',
      lifecycle: 'started',
      payload_schema: 'hive.session.payload.tool_call.started.v2',
      invocation_id: 'invocation-1',
      actor: { type: 'assistant' },
      payload: { tool_name: 'read_file', arguments: { path: 'workspace/report.md' } },
    };
    const toolResult: SessionEventV2 = {
      ...event(2, 'completed'),
      ordinal: undefined,
      item_id: 'tool-result-1',
      item_kind: 'tool_result',
      kind: 'tool_result.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'invocation-1',
      parent_item_id: 'tool-call-1',
      actor: { type: 'tool' },
      payload: { outcome: 'completed', result: 'file bytes' },
    };

    const messages = projectSessionEventStoreToMessages(replay([toolCall, toolResult]));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: 'tool_call',
      id: 'invocation-1',
      toolName: 'read_file',
      toolArgs: { path: 'workspace/report.md' },
      toolStatus: 'done',
      toolResult: 'file bytes',
      sessionItem: { id: 'tool-call-1', kind: 'tool_call' },
    });
  });

  it('recovers a legacy persisted tool envelope during a rolling canonical replay', () => {
    const progress = 'LIVE_PROGRESS_REPLAY_0717: checking the durable Session path.';
    const legacyToolCall: SessionEventV2 = {
      ...event(1, 'started'),
      ordinal: undefined,
      item_id: 'legacy-progress-call-1',
      item_kind: 'tool_call',
      kind: 'tool_call.started',
      lifecycle: 'started',
      payload_schema: 'hive.session.payload.tool_call.started.v2',
      actor: { type: 'tool' },
      payload: {
        content: JSON.stringify({
          name: 'report_progress',
          args: { message: progress },
          status: 'running',
          tool_call_id: 'progress-call-1',
        }),
        parts: [],
        metadata: { tool_name: 'report_progress', tool_call_id: 'progress-call-1' },
        legacy: true,
      },
    };

    const messages = projectSessionEventStoreToMessages(replay([legacyToolCall]));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'report_progress',
      toolArgs: { message: progress },
      toolStatus: 'running',
    });
    expect(buildRunTimelineFromMessages(messages).steps).toEqual([
      expect.objectContaining({
        kind: 'commentary',
        title: 'Progress update',
        details: progress,
      }),
    ]);
  });

  it('projects a native report_progress commentary item identically after live delivery and replay', () => {
    const progress = 'LIVE_PROGRESS_NATIVE_0717: validating live delivery and reload.';
    const commentary: SessionEventV2 = {
      ...event(1, 'completed'),
      ordinal: undefined,
      item_id: 'progress-commentary-1',
      item_kind: 'assistant_commentary',
      kind: 'assistant_commentary.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_commentary.completed.v2',
      invocation_id: 'progress-invocation-1',
      parent_item_id: 'progress-tool-call-1',
      actor: { type: 'assistant' },
      payload: { phase: 'commentary', content: progress },
    };

    const messages = projectSessionEventStoreToMessages(replay([commentary]));
    const timeline = buildRunTimelineFromMessages(messages);

    expect(messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: progress,
        eventType: 'assistant_commentary',
      }),
    ]);
    expect(timeline.steps).toEqual([
      expect.objectContaining({
        kind: 'commentary',
        title: 'Progress update',
        details: progress,
      }),
    ]);
  });

  it('preserves no-phase public model text as assistant_text without forging commentary semantics', () => {
    const progress = 'I found the failing path. Next I am checking the durable task state.';
    const delta = {
      ...event(1, 'delta'),
      payload: { phase: 'unknown', content: progress },
    } as SessionEventV2;
    const snapshot = {
      ...event(2, 'completed'),
      item_kind: 'assistant_text',
      kind: 'assistant_text.snapshot',
      lifecycle: 'snapshot',
      payload_schema: 'hive.session.payload.assistant_text.snapshot.v2',
      payload: { phase: 'unknown', content: progress },
    } as SessionEventV2;
    const completed = {
      ...event(3, 'completed'),
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      payload: { phase: 'unknown', content: '' },
    } as SessionEventV2;

    const messages = projectSessionEventStoreToMessages(replay([delta, snapshot, completed]));

    expect(messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: progress,
        eventType: 'assistant_text',
        eventStatus: 'completed',
        sessionItem: expect.objectContaining({ kind: 'assistant_text' }),
      }),
    ]);
  });

  it('projects durable artifact parts on the canonical final message', () => {
    const source = {
      ...event(1, 'completed'),
      item_id: 'assistant-source-1',
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      actor: { type: 'assistant' as const },
      payload: { content: 'Final answer' },
    };
    const final = {
      ...event(2, 'completed'),
      item_id: 'assistant-final-1',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      actor: { type: 'assistant' as const },
      payload: {
        source_blocks: [{ item_id: 'assistant-source-1', block_index: 0, content_hash: 'hash-1' }],
        parts: [
          {
            type: 'artifact',
            artifact_id: 'artifact-1',
            path: 'workspace/final-report.md',
            name: 'final-report.md',
            preview_kind: 'markdown',
            source: 'workspace_write',
            runtime_task_id: 'run-1',
          },
        ],
      },
    };

    const messages = projectSessionEventStoreToMessages(replay([source, final]));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: 'assistant',
      content: 'Final answer',
      artifacts: [
        {
          id: 'artifact-1',
          path: 'workspace/final-report.md',
          name: 'final-report.md',
          previewKind: 'markdown',
          source: 'workspace_write',
          runtimeTaskId: 'run-1',
        },
      ],
    });
  });

  it('keeps a legacy message at its original sequence when later compatibility events add no message', () => {
    const legacyAssistant = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-assistant-1',
      sequence: 1,
      reason: 'legacy_generation',
      legacy_event_type: 'assistant_message',
      payload: {
        content: 'OLDER_LEGACY_FINAL',
        legacy_run_id: 'run-old',
        metadata: {},
      },
    } as unknown as ChatTranscriptEventPayload;
    const acceptedInput: SessionEventV2 = {
      ...event(2, 'completed'),
      ordinal: undefined,
      item_id: 'input-new',
      item_kind: 'human_input',
      kind: 'human_input.accepted',
      lifecycle: 'accepted',
      payload_schema: 'hive.session.payload.human_input.accepted.v2',
      scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
      actor: { type: 'user', id: 'user-1' },
      payload: { content: 'NEWER_USER_PROMPT' },
    };
    const latestProgress: SessionEventV2 = {
      ...event(3, 'completed'),
      ordinal: undefined,
      item_id: 'latest-progress',
      item_kind: 'assistant_commentary',
      kind: 'assistant_commentary.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_commentary.completed.v2',
      payload: { phase: 'commentary', content: 'LATEST_NATIVE_PROGRESS' },
    };
    const laterCompatibilityPhase = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-phase-4',
      sequence: 4,
      reason: 'legacy_generation',
      legacy_event_type: 'phase',
      payload: { content: '', metadata: { phase: 'done' } },
    } as unknown as ChatTranscriptEventPayload;

    const hydrated = hydrateSessionTranscriptEvents([
      legacyAssistant,
      acceptedInput as unknown as ChatTranscriptEventPayload,
      latestProgress as unknown as ChatTranscriptEventPayload,
      laterCompatibilityPhase,
    ]);

    expect(hydrated.messages.map((message) => message.content)).toEqual([
      'OLDER_LEGACY_FINAL',
      'NEWER_USER_PROMPT',
      'LATEST_NATIVE_PROGRESS',
    ]);
  });

  it('lets a canonical assistant_final supersede a legacy assistant_message bound to the same run', () => {
    const unrelatedLegacy = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-final-unrelated',
      sequence: 1,
      reason: 'legacy_generation',
      legacy_event_type: 'assistant_message',
      payload: {
        content: 'UNRELATED_LEGACY_FINAL',
        legacy_run_id: 'run-unrelated',
        metadata: {},
      },
    } as unknown as ChatTranscriptEventPayload;
    const source: SessionEventV2 = {
      ...event(2, 'completed'),
      ordinal: undefined,
      item_id: 'canonical-source',
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      payload: { phase: 'unknown', content: 'CANONICAL_FINAL_BYTES' },
    };
    const final: SessionEventV2 = {
      ...event(3, 'completed'),
      ordinal: undefined,
      item_id: 'canonical-final',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      payload: {
        phase: 'final',
        render_owner_id: 'canonical-render-owner',
        source_blocks: [
          { item_id: 'canonical-source', block_index: 0, content_hash: 'hash-1' },
        ],
      },
    };
    const legacyDuplicate = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-final-duplicate',
      sequence: 4,
      reason: 'legacy_generation',
      legacy_event_type: 'assistant_message',
      payload: {
        content: 'STALE_LEGACY_PROJECTION',
        legacy_run_id: 'run-1',
        metadata: {},
      },
    } as unknown as ChatTranscriptEventPayload;

    const hydrated = hydrateSessionTranscriptEvents([
      unrelatedLegacy,
      source as unknown as ChatTranscriptEventPayload,
      final as unknown as ChatTranscriptEventPayload,
      legacyDuplicate,
    ]);

    expect(hydrated.messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: 'UNRELATED_LEGACY_FINAL',
      }),
      expect.objectContaining({
        role: 'assistant',
        content: 'CANONICAL_FINAL_BYTES',
        sessionItem: expect.objectContaining({ kind: 'assistant_final' }),
      }),
    ]);
  });
});

describe('canonical application facts at the shared consumer seam (Codex REQUEST_CHANGES #3)', () => {
  type ApplicationFacts = {
    canonicalEvents: SessionEventV2[];
    compatibilityApplied: boolean;
    compatibilityEvents: SessionCompatibilityEvent[];
  } | null;

  function factsOf(consumed: ReturnType<typeof consumeSessionEnvelope>): ApplicationFacts {
    return (consumed as { application?: ApplicationFacts }).application ?? null;
  }

  function appliedEventIds(facts: ApplicationFacts): string[] {
    // A missing facts object on the pristine baseline is a loud RED, never a silent pass.
    return facts ? facts.canonicalEvents.map((applied) => applied.event_id) : ['APPLICATION_FACTS_MISSING'];
  }

  function runCompletedEvent(sequence: number, runId = 'run-1'): SessionEventV2 {
    return {
      ...event(sequence, 'completed'),
      ordinal: undefined,
      item_id: `run-${runId}`,
      item_kind: 'run',
      kind: 'run.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.run.completed.v2',
      scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: runId },
      actor: { type: 'runtime' },
      payload: {},
    } as SessionEventV2;
  }

  function compatibilityFiller(sequence: number): ChatTranscriptEventPayload {
    return {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: `legacy-${sequence}`,
      sequence,
      reason: 'legacy_generation',
      legacy_event_type: 'phase',
      payload: { content: '', metadata: { phase: 'done' } },
    } as unknown as ChatTranscriptEventPayload;
  }

  function runScopedFailureEvent(sequence: number, runId = 'run-1'): SessionEventV2 {
    return {
      schema: 'hive.session_event',
      schema_version: 2,
      event_id: `event-runtime-failure-${sequence}`,
      sequence,
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
    } as unknown as SessionEventV2;
  }

  it('reports applied canonical events per transition and nothing for buffered, conflicted, or duplicate arrivals', () => {
    const first = consumeSessionEnvelope(event(1, 'started') as unknown as ChatTranscriptEventPayload, undefined, 0);
    expect(appliedEventIds(factsOf(first))).toEqual(['event-1']);

    // Buffered-only arrival changes the store identity (gap state) without
    // applying anything — the exact mechanism e3 mistook for newlyApplied.
    const buffered = consumeSessionEnvelope(event(3, 'delta') as unknown as ChatTranscriptEventPayload, first.store, 0);
    expect(buffered.store).not.toBe(first.store);
    expect(factsOf(buffered)).toBeNull();

    const closed = consumeSessionEnvelope(event(2, 'delta') as unknown as ChatTranscriptEventPayload, buffered.store, 0);
    expect(appliedEventIds(factsOf(closed))).toEqual(['event-2', 'event-3']);

    const duplicate = consumeSessionEnvelope(event(2, 'delta') as unknown as ChatTranscriptEventPayload, closed.store, 0);
    // Duplicate delivery changes store identity but exposes an empty
    // transition (finding D) — still no application facts.
    expect(duplicate.store).not.toBe(closed.store);
    expect(duplicate.store?.lastTransition.appliedEvents).toEqual([]);
    expect(factsOf(duplicate)).toBeNull();
  });

  it('surfaces drained canonical events when a compatibility carrier fills the gap', () => {
    const buffered = consumeSessionEnvelope(
      runCompletedEvent(2) as unknown as ChatTranscriptEventPayload,
      undefined,
      0,
    );
    expect(factsOf(buffered)).toBeNull();

    const filled = consumeSessionEnvelope(compatibilityFiller(1), buffered.store, 0);
    expect(filled.canonical).toBe(false);
    expect(appliedEventIds(factsOf(filled))).toEqual(['event-2']);
    expect(factsOf(filled)?.compatibilityApplied).toBe(true);
  });

  it('excludes ignored terminal-item contiguous events from the application facts', () => {
    const terminal = consumeSessionEnvelope(event(1, 'completed') as unknown as ChatTranscriptEventPayload, undefined, 0);
    const ignored = consumeSessionEnvelope(event(2, 'delta') as unknown as ChatTranscriptEventPayload, terminal.store, 0);
    expect(ignored.store?.ignoredEventIds).toEqual(['event-2']);
    expect(factsOf(ignored)).toBeNull();
  });

  it('applies terminal semantics per applied event when one transition drains a terminal tail', () => {
    // runtime_failure at seq 3 sits buffered behind a missing seq 2.
    const failure = runScopedFailureEvent(3);
    const buffered = consumeSessionEnvelope(failure as unknown as ChatTranscriptEventPayload, undefined, 0);
    expect(factsOf(buffered)).toBeNull();

    const closed = consumeSessionEnvelope(event(1, 'started') as unknown as ChatTranscriptEventPayload,
      consumeSessionEnvelope(event(2, 'delta') as unknown as ChatTranscriptEventPayload, buffered.store, 0).store, 0);
    const facts = factsOf(closed);
    expect(appliedEventIds(facts)).toEqual(['event-1', 'event-2', 'event-runtime-failure-3']);

    const terminalRunIds: Array<string | null> = [];
    let terminalFlag: boolean | undefined;
    let messagesProjected = false;
    applyCanonicalSessionSnapshot({
      events: facts!.canonicalEvents,
      store: closed.store!,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: (runId) => { terminalRunIds.push(runId); },
      onMessages: (_messages, terminal) => { terminalFlag = terminal; messagesProjected = true; },
    });
    expect(terminalRunIds).toEqual(['run-1']);
    expect(terminalFlag).toBe(true);
    expect(messagesProjected).toBe(true);
  });

  it('keeps per-event stale-run safety for a drained run-1 terminal against an active run-2', () => {
    const staleTerminal = runCompletedEvent(2, 'run-1');
    const filler = event(1, 'started');
    const closed = consumeSessionEnvelope(filler as unknown as ChatTranscriptEventPayload,
      consumeSessionEnvelope(staleTerminal as unknown as ChatTranscriptEventPayload, undefined, 0).store, 0);
    const facts = factsOf(closed);
    expect(appliedEventIds(facts)).toEqual(['event-1', 'event-2']);

    const activeRuns: Record<string, SessionRunState> = {
      'agent-1:session-1': { runId: 'run-2', status: 'running' } as SessionRunState,
    };
    const messageMerges: Array<{ terminal: boolean; runId: string | null }> = [];
    applyCanonicalSessionSnapshot({
      events: facts!.canonicalEvents,
      store: closed.store!,
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: (runId) => isTerminalRunAcceptedForActiveRun(activeRuns['agent-1:session-1']?.runId ?? null, runId),
      onMessages: (_messages, terminal, runId) => { messageMerges.push({ terminal, runId }); },
    });

    expect(activeRuns['agent-1:session-1']?.runId).toBe('run-2');
    // No terminal was accepted: the merge carries no run binding at all.
    expect(messageMerges).toEqual([{ terminal: false, runId: null }]);
  });

  it('binds the terminal merge to the latest accepted terminal, never to a later rejected stale terminal (Codex finding E)', () => {
    // Drain order: matching run-2 terminal first, then a stale run-1 terminal
    // rejected by the stale-run guard.
    const accepted = runCompletedEvent(1, 'run-2');
    const rejectedStale = runCompletedEvent(2, 'run-1');
    const messageMerges: Array<{ terminal: boolean; runId: string | null }> = [];
    applyCanonicalSessionSnapshot({
      events: [accepted, rejectedStale],
      store: replay([accepted, rejectedStale]),
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: (runId) => runId === 'run-2',
      onMessages: (_messages, terminal, runId) => { messageMerges.push({ terminal, runId }); },
    });

    expect(messageMerges).toEqual([{ terminal: true, runId: 'run-2' }]);
  });

  it('binds the terminal merge to the latest accepted terminal when several terminals apply in one transition', () => {
    const first = runCompletedEvent(1, 'run-1');
    const second = runCompletedEvent(2, 'run-2');
    const messageMerges: Array<{ terminal: boolean; runId: string | null }> = [];
    applyCanonicalSessionSnapshot({
      events: [first, second],
      store: replay([first, second]),
      active: true,
      onTranscript: () => undefined,
      onActivity: () => undefined,
      onTerminal: () => true,
      onMessages: (_messages, terminal, runId) => { messageMerges.push({ terminal, runId }); },
    });

    expect(messageMerges).toEqual([{ terminal: true, runId: 'run-2' }]);
  });

  it('reports no application facts for a buffered compatibility carrier (Codex finding B seam)', () => {
    const buffered = consumeSessionEnvelope(compatibilityFiller(3), undefined, 0);
    expect(buffered.canonical).toBe(false);
    expect(buffered.store?.projection.phase).toBe('gap_detected');
    expect(factsOf(buffered)).toBeNull();
  });

  it('reports drained compatibility events in the application facts for exact-once live projection (Codex finding C seam)', () => {
    const bufferedFirst = consumeSessionEnvelope(compatibilityFiller(2), undefined, 0);
    const bufferedBoth = consumeSessionEnvelope(compatibilityFiller(3), bufferedFirst.store, 0);
    expect(factsOf(bufferedBoth)).toBeNull();

    const closed = consumeSessionEnvelope(
      event(1, 'started') as unknown as ChatTranscriptEventPayload,
      bufferedBoth.store,
      0,
    );
    const facts = factsOf(closed);
    expect(appliedEventIds(facts)).toEqual(['event-1']);
    expect(facts?.compatibilityEvents.map((applied) => applied.event_id)).toEqual(['legacy-2', 'legacy-3']);
  });
});
