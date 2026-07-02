import { describe, expect, it } from 'vitest';

import {
  CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
  buildChatSocketKeepaliveMessage,
  buildRuntimeSummary,
  applyStreamingChunkEvent,
  computeComposerHeight,
  getCompactionDisplayContent,
  getRuntimeEventMessage,
  getTerminalRunIdFromTranscriptEvent,
  getTransportNotice,
  applySessionActiveRunState,
  applySessionActiveRunObservedState,
  appendToolCallMessage,
  applyRuntimeDoneEvent,
  extractArtifactParts,
  isA2ASession,
  isDraftHumanChatSession,
  isReadOnlySessionForCurrentUser,
  shouldUseWritableSessionSurface,
  shouldPreserveActiveSessionForRequestedId,
  normalizeStoredChatMessage,
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
  filterSessionsForAgent,
  mergePendingUserMessages,
  replayTranscriptEvents,
  sessionBelongsToAgent,
  isTerminalRealtimeChatEvent,
  shouldClearStaleRuntimeState,
  shouldIgnoreObservedActiveRun,
} from './chatRuntime';

describe('chatRuntime helpers', () => {
  it('treats draft human sessions as writable local composer state, not read-only A2A state', () => {
    const draftSession = {
      id: 'draft:local-session',
      agent_id: 'agent-1',
      source_channel: 'web',
      session_kind: 'human_chat',
      is_draft: true,
    };

    expect(isDraftHumanChatSession(draftSession)).toBe(true);
    expect(isA2ASession(draftSession)).toBe(false);
    expect(isReadOnlySessionForCurrentUser(draftSession, 'user-1')).toBe(false);
    expect(shouldUseWritableSessionSurface(draftSession, 'user-1')).toBe(true);
    expect(shouldPreserveActiveSessionForRequestedId(draftSession, 'draft:local-session')).toBe(false);
  });

  it('replays durable transcript events idempotently across history and websocket', () => {
    const initial = createEmptyTranscriptReplayState();
    const event = {
      id: 'evt-1',
      sequence: 10,
      type: 'assistant_message',
      event_type: 'assistant_message',
      actor_type: 'assistant',
      role: 'assistant',
      content: 'Report is ready.',
      parts: [{ type: 'text', text: 'Report is ready.' }],
      created_at: '2026-06-20T12:00:00Z',
    };

    const fromHistory = applyTranscriptEvent(initial, event);
    const fromSocket = applyTranscriptEvent(fromHistory, event);

    expect(fromHistory.messages).toHaveLength(1);
    expect(fromSocket.messages).toEqual(fromHistory.messages);
    expect(fromSocket.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('preserves transcript event id separately from durable message id during replay', () => {
    const state = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-user-1',
      message_id: 'msg-user-1',
      sequence: 11,
      type: 'user_message',
      event_type: 'user_message',
      actor_type: 'user',
      role: 'user',
      content: 'Run the rewind checkpoint test.',
      created_at: '2026-06-29T12:00:00Z',
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      role: 'user',
      id: 'msg-user-1',
      messageId: 'msg-user-1',
      transcriptEventId: 'evt-user-1',
    });
  });

  it('restores reasoning from persisted assistant parts during transcript replay', () => {
    const state = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-reasoning-answer',
      sequence: 12,
      type: 'assistant_message',
      event_type: 'assistant_message',
      actor_type: 'assistant',
      role: 'assistant',
      content: 'Report is ready.',
      parts: [
        { type: 'reasoning', text: 'Checked the worker output and verified artifact metadata.' },
        { type: 'text', text: 'Report is ready.' },
      ],
      created_at: '2026-06-29T12:00:00Z',
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      role: 'assistant',
      content: 'Report is ready.',
      thinking: 'Checked the worker output and verified artifact metadata.',
    });

    expect(normalizeStoredChatMessage({
      role: 'assistant',
      content: 'Report is ready.',
      parts: [
        { type: 'reasoning', text: 'Checked persisted parts.' },
        { type: 'text', text: 'Report is ready.' },
      ],
    })).toMatchObject({
      role: 'assistant',
      thinking: 'Checked persisted parts.',
    });
  });

  it('treats blocking tool-card transcript events as terminal user-awaiting states', () => {
    const state = {
      ...createEmptyTranscriptReplayState(),
      messages: [{ role: 'assistant' as const, content: '', thinking: 'Need more input', _streaming: true } as any],
      ui: { isWaiting: true, isStreaming: true },
    };

    const next = applyTranscriptEvent(state, {
      id: 'evt-tool',
      sequence: 11,
      type: 'tool_result',
      event_type: 'tool_result',
      actor_type: 'agent',
      role: 'tool_call',
      content: JSON.stringify({
        status: 'awaiting_user_clarification',
        blocking: true,
        questions: [{ question: 'Cadence?', options: [{ label: 'Weekly' }] }],
      }),
      metadata: { tool_name: 'ask_user_question' },
      created_at: '2026-06-20T12:00:01Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolMeta: { kind: 'user_clarification', blocking: true },
    });
    expect(next.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('keeps only the oldest unresolved session permission gate visible', () => {
    const state = replayTranscriptEvents([
      {
        id: 'evt-permission-1',
        sequence: 1,
        type: 'permission',
        event_type: 'permission',
        role: 'system',
        content: 'first permission',
        metadata: {
          status: 'session_permission_required',
          permission_request_id: 'permission-1',
          permission_request: {
            permission_request_id: 'permission-1',
            tool_name: 'track_todo',
          },
        },
        created_at: '2026-06-26T16:20:00Z',
      },
      {
        id: 'evt-permission-2',
        sequence: 2,
        type: 'permission',
        event_type: 'permission',
        role: 'system',
        content: 'second permission',
        metadata: {
          status: 'session_permission_required',
          permission_request_id: 'permission-2',
          permission_request: {
            permission_request_id: 'permission-2',
            tool_name: 'track_todo',
          },
        },
        created_at: '2026-06-26T16:21:00Z',
      },
    ]);

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      role: 'event',
      eventStatus: 'session_permission_required',
      sessionPermissionRequest: { permission_request_id: 'permission-1' },
    });
  });

  it('reveals the next queued session permission gate after the visible one is resolved', () => {
    const state = replayTranscriptEvents([
      {
        id: 'evt-permission-1',
        sequence: 1,
        type: 'permission',
        event_type: 'permission',
        role: 'system',
        content: 'first permission',
        metadata: {
          status: 'session_permission_required',
          permission_request_id: 'permission-1',
          permission_request: {
            permission_request_id: 'permission-1',
            tool_name: 'track_todo',
          },
        },
        created_at: '2026-06-26T16:20:00Z',
      },
      {
        id: 'evt-permission-2',
        sequence: 2,
        type: 'permission',
        event_type: 'permission',
        role: 'system',
        content: 'second permission',
        metadata: {
          status: 'session_permission_required',
          permission_request_id: 'permission-2',
          permission_request: {
            permission_request_id: 'permission-2',
            tool_name: 'track_todo',
          },
        },
        created_at: '2026-06-26T16:21:00Z',
      },
      {
        id: 'evt-decision-1',
        sequence: 3,
        type: 'session_permission_decision',
        event_type: 'session_permission_decision',
        role: 'system',
        content: JSON.stringify({
          permission_request_id: 'permission-1',
          decision: 'allow_session',
        }),
        metadata: {
          permission_request_id: 'permission-1',
          decision: 'allow_session',
        },
        created_at: '2026-06-26T16:22:00Z',
      },
    ]);

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      role: 'event',
      eventStatus: 'session_permission_required',
      sessionPermissionRequest: { permission_request_id: 'permission-2' },
    });
  });

  it('removes a resolved session permission gate from replayed transcript state', () => {
    const state = replayTranscriptEvents([
      {
        id: 'evt-permission',
        sequence: 1,
        type: 'permission',
        event_type: 'permission',
        role: 'system',
        content: 'permission',
        metadata: {
          status: 'session_permission_required',
          permission_request_id: 'permission-1',
          permission_request: {
            permission_request_id: 'permission-1',
            tool_name: 'track_todo',
          },
        },
        created_at: '2026-06-26T16:20:00Z',
      },
      {
        id: 'evt-decision',
        sequence: 2,
        type: 'session_permission_decision',
        event_type: 'session_permission_decision',
        role: 'system',
        content: JSON.stringify({
          permission_request_id: 'permission-1',
          decision: 'allow_session',
        }),
        metadata: {
          permission_request_id: 'permission-1',
          decision: 'allow_session',
        },
        created_at: '2026-06-26T16:21:00Z',
      },
    ]);

    expect(state.messages).toHaveLength(0);
    expect(state.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('removes a resolved session permission gate from live broadcast payloads', () => {
    const state = replayTranscriptEvents([
      {
        id: 'evt-permission',
        sequence: 1,
        type: 'permission',
        event_type: 'permission',
        role: 'system',
        content: 'permission',
        metadata: {
          status: 'session_permission_required',
          permission_request_id: 'permission-1',
          permission_request: {
            permission_request_id: 'permission-1',
            tool_name: 'track_todo',
          },
        },
        created_at: '2026-06-26T16:20:00Z',
      },
      {
        id: 'evt-resolved',
        sequence: 2,
        type: 'permission_resolved',
        event_type: 'permission_resolved',
        role: 'system',
        permission_request_id: 'permission-1',
        status: 'allowed',
        created_at: '2026-06-26T16:21:00Z',
      },
    ]);

    expect(state.messages).toHaveLength(0);
  });

  it('unwraps persisted tool-result envelopes before rendering clarification cards', () => {
    const state = {
      ...createEmptyTranscriptReplayState(),
      messages: [{ role: 'assistant' as const, content: '', thinking: 'Need more input', _streaming: true } as any],
      ui: { isWaiting: true, isStreaming: true },
    };

    const next = applyTranscriptEvent(state, {
      id: 'evt-tool-envelope',
      sequence: 12,
      type: 'tool_result',
      event_type: 'tool_result',
      actor_type: 'tool',
      content: JSON.stringify({
        name: 'ask_user_question',
        args: { questions: [] },
        status: 'done',
        result: JSON.stringify({
          status: 'awaiting_user_clarification',
          blocking: true,
          questions: [{
            header: '核心职责',
            question: '这个 AI 产品经理的核心职责是什么？',
            options: [{ label: '全都要', description: '覆盖全栈产品经理职责' }],
          }],
        }),
      }),
      metadata: { tool_name: 'ask_user_question' },
      created_at: '2026-06-21T09:09:09Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolMeta: { kind: 'user_clarification', blocking: true },
    });
    expect(next.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('hydrates durable clarification answer metadata from transcript events', () => {
    const next = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-answered-question',
      sequence: 13,
      type: 'tool_result',
      event_type: 'tool_result',
      actor_type: 'tool',
      content: JSON.stringify({
        name: 'ask_user_question',
        status: 'done',
        result: JSON.stringify({
          status: 'awaiting_user_clarification',
          blocking: true,
          questions: [{ question: 'Scope?', options: [{ label: 'Mine' }] }],
        }),
      }),
      metadata: {
        tool_name: 'ask_user_question',
        answered: true,
        answered_by_event_id: 'evt-user-answer',
        answer_text: 'Scope: Mine',
      },
      created_at: '2026-06-21T09:10:09Z',
    });

    expect(next.messages[0]).toMatchObject({
      role: 'tool_call',
      toolMeta: {
        kind: 'user_clarification',
        answered: true,
        answeredByEventId: 'evt-user-answer',
        answerText: 'Scope: Mine',
      },
    });
    expect(next.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('preserves runtime step metadata from persisted tool transcript events', () => {
    const next = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-tool-step',
      sequence: 42,
      type: 'tool_result',
      event_type: 'tool_result',
      actor_type: 'tool',
      content: JSON.stringify({
        name: 'read_file',
        args: { path: 'workspace/a.md' },
        status: 'done',
        result: 'file content',
        tool_call_id: 'toolu_123',
        step_id: 'tool:toolu_123',
        duration_ms: 2500,
        visibility: 'collapsed',
      }),
      metadata: {
        tool_name: 'read_file',
        status: 'done',
        tool_call_id: 'toolu_123',
        step_id: 'tool:toolu_123',
        duration_ms: 2500,
        visibility: 'collapsed',
      },
      created_at: '2026-06-22T10:00:02.500Z',
    });

    expect(next.messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'read_file',
      toolStatus: 'done',
      toolArgs: { path: 'workspace/a.md' },
      toolMeta: {
        kind: 'runtime_step',
        toolCallId: 'toolu_123',
        stepId: 'tool:toolu_123',
        durationMs: 2500,
        visibility: 'collapsed',
      },
    });
  });

  it('keeps websocket sessions alive while a user is waiting for output', () => {
    expect(CHAT_SOCKET_KEEPALIVE_INTERVAL_MS).toBeGreaterThan(0);
    expect(CHAT_SOCKET_KEEPALIVE_INTERVAL_MS).toBeLessThanOrEqual(30_000);
    expect(buildChatSocketKeepaliveMessage()).toEqual({ type: 'ping' });
  });

  it('clears stale waiting state when the backend reports no active run', () => {
    const result = applySessionActiveRunState(
      { 'agent-1:session-1': { runId: 'run-1', status: 'running' } },
      { 'agent-1:session-1': { isWaiting: true, isStreaming: false } },
      'agent-1:session-1',
      null,
    );

    expect(result.activeRuns).toEqual({});
    expect(result.uiStates).toEqual({});
  });

  it('marks a session waiting when an active run is observed', () => {
    const result = applySessionActiveRunState(
      {},
      {},
      'agent-1:session-1',
      { runId: 'run-1', status: 'running' },
    );

    expect(result.activeRuns).toEqual({ 'agent-1:session-1': { runId: 'run-1', status: 'running' } });
    expect(result.uiStates).toEqual({ 'agent-1:session-1': { isWaiting: true, isStreaming: false } });
  });

  it('does not let active-run polling overwrite an already streaming transcript', () => {
    const result = applySessionActiveRunObservedState(
      {},
      { 'agent-1:session-1': { isWaiting: false, isStreaming: true } },
      'agent-1:session-1',
      { runId: 'run-1', status: 'running' },
    );

    expect(result.activeRuns).toEqual({ 'agent-1:session-1': { runId: 'run-1', status: 'running' } });
    expect(result.uiStates).toEqual({ 'agent-1:session-1': { isWaiting: false, isStreaming: true } });
  });

  it('ignores observed active runs once a terminal event closed the same session', () => {
    expect(shouldIgnoreObservedActiveRun({
      key: 'agent-1:session-1',
      run: { runId: 'run-1', status: 'running' },
      terminalRunIds: new Set(['run-1']),
      terminalSessionKeys: new Set(),
    })).toBe(true);

    expect(shouldIgnoreObservedActiveRun({
      key: 'agent-1:session-1',
      run: { runId: 'run-late-without-terminal-id', status: 'running' },
      terminalRunIds: new Set(),
      terminalSessionKeys: new Set(['agent-1:session-1']),
    })).toBe(true);

    expect(shouldIgnoreObservedActiveRun({
      key: 'agent-1:session-2',
      run: { runId: 'run-2', status: 'running' },
      terminalRunIds: new Set(['run-1']),
      terminalSessionKeys: new Set(['agent-1:session-1']),
    })).toBe(false);
  });

  it('keeps an optimistic user prompt visible until transcript replay confirms it', () => {
    const merged = mergePendingUserMessages(
      [
        { role: 'user', content: 'previous request' },
        { role: 'assistant', content: 'previous answer' },
        { role: 'tool_call', content: '', toolName: 'tool_search', toolStatus: 'running' },
      ],
      [{
        message: { role: 'user', content: 'Plan a source-grounded market report' },
        anchorMessageCount: 2,
      }],
    );

    expect(merged.messages.map((message) => [message.role, message.content || message.toolName])).toEqual([
      ['user', 'previous request'],
      ['assistant', 'previous answer'],
      ['user', 'Plan a source-grounded market report'],
      ['tool_call', 'tool_search'],
    ]);
    expect(merged.pending).toHaveLength(1);
  });

  it('drops an optimistic user prompt once durable transcript contains the same user message', () => {
    const merged = mergePendingUserMessages(
      [
        {
          id: 'durable-user-event',
          role: 'user',
          content: 'Plan a source-grounded market report',
        },
        { role: 'tool_call', content: '', toolName: 'tool_search', toolStatus: 'running' },
      ],
      [{
        message: { role: 'user', content: 'Plan a source-grounded market report' },
        anchorMessageCount: 0,
      }],
    );

    expect(merged.messages).toHaveLength(2);
    expect(merged.messages[0]).toMatchObject({ id: 'durable-user-event' });
    expect(merged.pending).toEqual([]);
  });

  it('defers stale active-run clearing during recent runtime activity', () => {
    expect(shouldClearStaleRuntimeState({
      hasStaleRuntimeState: true,
      lastRuntimeActivityAt: 10_000,
      now: 12_000,
      graceMs: 8_000,
    })).toBe(false);
    expect(shouldClearStaleRuntimeState({
      hasStaleRuntimeState: true,
      lastRuntimeActivityAt: 10_000,
      now: 19_000,
      graceMs: 8_000,
    })).toBe(true);
  });

  it('identifies realtime terminal events that must refresh durable session history', () => {
    expect(isTerminalRealtimeChatEvent({ type: 'done' })).toBe(true);
    expect(isTerminalRealtimeChatEvent({ type: 'error' })).toBe(true);
    expect(isTerminalRealtimeChatEvent({ type: 'quota_exceeded' })).toBe(true);
    expect(isTerminalRealtimeChatEvent({ type: 'run_cancelled' })).toBe(true);
    expect(isTerminalRealtimeChatEvent({ type: 'assistant_message' })).toBe(true);
    expect(isTerminalRealtimeChatEvent({ type: 'run_completed' })).toBe(true);
    expect(isTerminalRealtimeChatEvent({ event_type: 'done' })).toBe(true);

    expect(isTerminalRealtimeChatEvent({ type: 'run_started' })).toBe(false);
    expect(isTerminalRealtimeChatEvent({ type: 'thinking' })).toBe(false);
    expect(isTerminalRealtimeChatEvent({ type: 'chunk' })).toBe(false);
    expect(isTerminalRealtimeChatEvent({ type: 'tool_call' })).toBe(false);
  });

  it('extracts terminal run ids from replayed assistant transcript events', () => {
    expect(getTerminalRunIdFromTranscriptEvent({
      event_type: 'assistant_message',
      run_id: 'run-1',
      content: 'final answer',
    })).toBe('run-1');
    expect(getTerminalRunIdFromTranscriptEvent({
      event_type: 'thinking',
      run_id: 'run-1',
      content: 'partial reasoning',
    })).toBeNull();
  });

  it('filters sessions that belong to a different route agent', () => {
    expect(isA2ASession({ source_channel: 'agent' })).toBe(true);
    expect(isA2ASession({ participant_type: 'agent' })).toBe(true);
    expect(isA2ASession({ session_kind: 'agent_chat' })).toBe(true);
    expect(isA2ASession({ session_kind: 'delegation_run' })).toBe(true);
    expect(isA2ASession({ session_kind: 'human_chat', source_channel: 'web' })).toBe(false);

    expect(sessionBelongsToAgent({ id: 'session-1', agent_id: 'agent-1' }, 'agent-1')).toBe(true);
    expect(sessionBelongsToAgent({ id: 'session-1', agent_id: 'agent-2' }, 'agent-1')).toBe(false);
    expect(sessionBelongsToAgent({
      id: 'a2a-session-1',
      agent_id: 'agent-b',
      peer_agent_id: 'agent-a',
      session_kind: 'delegation_run',
    }, 'agent-a')).toBe(true);
    expect(sessionBelongsToAgent({ id: 'legacy-session-without-agent-id' }, 'agent-1')).toBe(true);
    expect(filterSessionsForAgent([
      { id: 'session-1', agent_id: 'agent-1' },
      { id: 'session-2', agent_id: 'agent-2' },
      { id: 'a2a-session-1', agent_id: 'agent-b', peer_agent_id: 'agent-1', source_channel: 'agent' },
      { id: 'session-3' },
    ], 'agent-1')).toEqual([
      { id: 'session-1', agent_id: 'agent-1' },
      { id: 'a2a-session-1', agent_id: 'agent-b', peer_agent_id: 'agent-1', source_channel: 'agent' },
      { id: 'session-3' },
    ]);
  });

  it('treats A2A and pending session lookups as read-only until canonical metadata loads', () => {
    expect(isReadOnlySessionForCurrentUser({ source_channel: 'agent' }, 'user-1')).toBe(true);
    expect(isReadOnlySessionForCurrentUser({ read_only: true }, 'user-1')).toBe(true);
    expect(isReadOnlySessionForCurrentUser({ is_pending_session_lookup: true, source_channel: 'unknown' }, 'user-1')).toBe(true);
    expect(isReadOnlySessionForCurrentUser({ user_id: 'user-2', source_channel: 'web' }, 'user-1')).toBe(true);
    expect(isReadOnlySessionForCurrentUser({ user_id: 'user-1', source_channel: 'web' }, 'user-1')).toBe(false);
  });

  it('keeps newly created web sessions writable while preserving A2A read-only sessions', () => {
    expect(shouldUseWritableSessionSurface({ id: 'new-session', source_channel: 'web' }, 'user-1')).toBe(true);
    expect(shouldUseWritableSessionSurface({ id: 'new-session', source_channel: 'web', user_id: 'user-1' }, 'user-1')).toBe(true);
    expect(shouldUseWritableSessionSurface({
      id: 'new-session',
      source_channel: 'web',
      user_id: 'server-user-id',
      is_current_user_session: true,
    }, 'client-user-id')).toBe(true);
    expect(shouldUseWritableSessionSurface({ id: 'other-session', source_channel: 'web', user_id: 'user-2' }, 'user-1')).toBe(false);
    expect(shouldUseWritableSessionSurface({ id: 'a2a-session', source_channel: 'agent' }, 'user-1')).toBe(false);
    expect(shouldUseWritableSessionSurface({ id: 'unknown-session', source_channel: 'unknown', is_pending_session_lookup: true }, 'user-1')).toBe(false);
  });

  it('preserves an active canonical session during requested-session lookup races', () => {
    expect(shouldPreserveActiveSessionForRequestedId({
      id: 'new-session',
      source_channel: 'web',
    }, 'new-session')).toBe(true);
    expect(shouldPreserveActiveSessionForRequestedId({
      id: 'new-session',
      source_channel: 'unknown',
      read_only: true,
      is_pending_session_lookup: true,
    }, 'new-session')).toBe(false);
    expect(shouldPreserveActiveSessionForRequestedId({
      id: 'other-session',
      source_channel: 'web',
    }, 'new-session')).toBe(false);
  });

  it('resets the streaming assistant when a chunk tombstone arrives', () => {
    const current = [{ role: 'assistant' as const, content: 'partial ', _streaming: true } as any];

    const reset = applyStreamingChunkEvent(current, { type: 'chunk', content: '', reset: true });
    const retried = applyStreamingChunkEvent(reset, { type: 'chunk', content: 'partial answer' });

    expect(reset).toEqual([{ role: 'assistant', content: '', _streaming: true }]);
    expect(retried).toEqual([{ role: 'assistant', content: 'partial answer', _streaming: true }]);
  });

  it('maps compaction runtime events into event messages', () => {
    const message = getRuntimeEventMessage({
      type: 'session_compact',
      summary: 'Trimmed older turns and kept the latest working set.',
      title: 'Context Compacted',
      status: 'info',
      original_message_count: 18,
      kept_message_count: 6,
      continuity_sections_injected: ['Current State', 'Pending Work'],
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      eventStatus: 'info',
      content: 'Trimmed older turns and kept the latest working set.',
      originalMessageCount: 18,
      keptMessageCount: 6,
      continuitySectionsInjected: ['Current State', 'Pending Work'],
    });
  });

  it('maps deferred tool discovery deltas into event messages', () => {
    const message = getRuntimeEventMessage({
      type: 'deferred_tools_delta',
      message: 'Discovered deferred tools: web_search',
      status: 'info',
      tool_groups: [{ name: 'web_pack' }],
      trigger_tool: 'tool_search',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'deferred_tools_delta',
      eventStatus: 'info',
      content: 'Discovered deferred tools: web_search',
      activatedToolGroupCount: 1,
      triggerTool: 'tool_search',
    });
  });

  it('maps enriched permission runtime events into event messages', () => {
    const message = getRuntimeEventMessage({
      type: 'permission',
      message: 'Approval is required before writing to the workspace.',
      title: 'Permission Gate',
      status: 'approval_required',
      tool_name: 'write_file',
      approval_id: 'approval-123',
      security_zone: 'workspace',
      capability: 'filesystem.write',
      approval_required: true,
      reason: 'Repository files will be modified.',
      next_step: 'Open Approvals to approve or reject this action.',
      retryable: true,
      retry_reason: 'auth',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'permission',
      eventTitle: 'Permission Gate',
      eventStatus: 'approval_required',
      eventToolName: 'write_file',
      eventApprovalId: 'approval-123',
      eventSecurityZone: 'workspace',
      eventCapability: 'filesystem.write',
      eventApprovalRequired: true,
      eventReason: 'Repository files will be modified.',
      eventNextStep: 'Open Approvals to approve or reject this action.',
      eventRetryable: true,
      eventRetryReason: 'auth',
    });
  });

  it('maps session-native hook and child runtime events into event messages', () => {
    const hook = getRuntimeEventMessage({
      type: 'hook_progress',
      message: 'Running PreToolUse hook',
      title: 'Hook Progress',
      status: 'running',
      hook_event: 'PreToolUse',
      hook_key: 'guard',
      runtime_task_id: 'rt-1',
      turn_id: 'turn-1',
    });

    expect(hook).toMatchObject({
      role: 'event',
      eventType: 'hook_progress',
      eventTitle: 'Hook Progress',
      eventStatus: 'running',
      content: 'Running PreToolUse hook',
      eventRuntimeTaskId: 'rt-1',
      eventTurnId: 'turn-1',
      eventHookEvent: 'PreToolUse',
      eventHookKey: 'guard',
    });

    const child = getRuntimeEventMessage({
      type: 'child_session',
      message: 'Research worker completed.',
      title: 'Child Session',
      status: 'completed',
      child_session_id: 'child-1',
      parent_session_id: 'parent-1',
      runtime_task_id: 'rt-child',
    });

    expect(child).toMatchObject({
      role: 'event',
      eventType: 'child_session',
      eventTitle: 'Child Session',
      eventStatus: 'completed',
      eventChildSessionId: 'child-1',
      eventParentSessionId: 'parent-1',
      eventRuntimeTaskId: 'rt-child',
    });

    const schedule = getRuntimeEventMessage({
      type: 'schedule',
      message: 'Schedule created: daily briefing',
      title: 'Schedule',
      status: 'created',
      schedule_id: 'schedule-1',
      runtime_task_id: 'rt-schedule',
    });

    expect(schedule).toMatchObject({
      role: 'event',
      eventType: 'schedule',
      eventTitle: 'Schedule',
      eventStatus: 'created',
      eventScheduleId: 'schedule-1',
      eventRuntimeTaskId: 'rt-schedule',
    });
  });

  it('treats websocket info events as transport notices instead of chat messages', () => {
    expect(
      getTransportNotice({
        type: 'info',
        content: 'Connection closed due to inactivity. Reconnect to continue.',
      }),
    ).toBe('Connection closed due to inactivity. Reconnect to continue.');
    expect(getRuntimeEventMessage({ type: 'info', content: 'ignored' })).toBeNull();
  });

  it('preserves stored event metadata from history payloads', () => {
    const message = normalizeStoredChatMessage({
      role: 'event',
      content: 'Context window compacted.',
      created_at: '2026-04-02T10:00:00Z',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      eventStatus: 'info',
      parts: [
        {
          type: 'event',
          original_message_count: 32,
          kept_message_count: 8,
        },
      ],
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      originalMessageCount: 32,
      keptMessageCount: 8,
      timestamp: '2026-04-02T10:00:00Z',
    });
  });

  it('extracts artifact parts from persisted assistant payloads', () => {
    const artifacts = extractArtifactParts({
      role: 'assistant',
      content: 'Report is ready.',
      artifacts: [
        {
          id: 'artifact-root',
          type: 'artifact',
          name: 'root-report.md',
          path: 'workspace/root-report.md',
          preview_kind: 'markdown',
        },
      ],
      parts: [
        { type: 'text', text: 'Report is ready.' },
        {
          type: 'artifact',
          artifact_id: 'artifact-part',
          name: 'report.md',
          path: 'workspace/report.md',
          preview_kind: 'markdown',
          source: 'workspace_write',
        },
      ],
    });

    expect(artifacts).toEqual([
      {
        id: 'artifact-root',
        name: 'root-report.md',
        path: 'workspace/root-report.md',
        previewKind: 'markdown',
        source: undefined,
        mimeType: undefined,
        size: undefined,
      },
      {
        id: 'artifact-part',
        name: 'report.md',
        path: 'workspace/report.md',
        previewKind: 'markdown',
        source: 'workspace_write',
        mimeType: undefined,
        size: undefined,
      },
    ]);

    expect(normalizeStoredChatMessage({
      role: 'assistant',
      content: 'Report is ready.',
      parts: [
        {
          type: 'artifact',
          id: 'artifact-part',
          name: 'report.md',
          path: 'workspace/report.md',
          preview_kind: 'markdown',
        },
      ],
    })).toMatchObject({
      role: 'assistant',
      artifacts: [
        {
          id: 'artifact-part',
          name: 'report.md',
          path: 'workspace/report.md',
          previewKind: 'markdown',
        },
      ],
    });
  });

  it('preserves artifact revision metadata for session-native delivery', () => {
    const artifacts = extractArtifactParts({
      role: 'assistant',
      content: 'Updated report.',
      parts: [
        {
          type: 'artifact',
          artifact_id: 'artifact-1',
          name: 'report.md',
          path: 'workspace/report.md',
          preview_kind: 'markdown',
          source: 'workflow',
          runtime_task_id: 'rt-1',
          owner_agent_id: 'agent-b',
          source_agent_id: 'agent-b',
          download_agent_id: 'agent-b',
          revision_id: 'rev-2',
          action: 'updated',
          tool_call_id: 'tool-9',
          diff_summary: '+3 -1',
          content_hash: 'sha256:delivery-content',
          snapshot_hash: 'sha256:snapshot-content',
          snapshot_storage_path: 'runtime_artifacts/chat_artifact_snapshots/session/run/artifact.md',
          preview_snapshot_content: '# Report\n',
        },
      ],
    });

    expect(artifacts).toEqual([
      {
        id: 'artifact-1',
        name: 'report.md',
        path: 'workspace/report.md',
        previewKind: 'markdown',
        source: 'workflow',
        mimeType: undefined,
        size: undefined,
        runtimeTaskId: 'rt-1',
        ownerAgentId: 'agent-b',
        sourceAgentId: 'agent-b',
        downloadAgentId: 'agent-b',
        revisionId: 'rev-2',
        action: 'updated',
        toolCallId: 'tool-9',
        diffSummary: '+3 -1',
        contentHash: 'sha256:delivery-content',
        snapshotHash: 'sha256:snapshot-content',
        snapshotStoragePath: 'runtime_artifacts/chat_artifact_snapshots/session/run/artifact.md',
        previewSnapshotContent: '# Report\n',
      },
    ]);
  });

  it('preserves destructive permission metadata from runtime events', () => {
    const message = getRuntimeEventMessage({
      type: 'permission',
      status: 'session_permission_required',
      content: "Tool 'run_command' requires session permission",
      permission_request_id: 'permission-1',
      permission_request: {
        permission_request_id: 'permission-1',
        tool_name: 'run_command',
        risk_class: 'destructive_delete',
        confirmation_kind: 'destructive_once',
        allow_session_allowed: false,
      },
    });

    expect(message?.sessionPermissionRequest).toMatchObject({
      permission_request_id: 'permission-1',
      tool_name: 'run_command',
      risk_class: 'destructive_delete',
      confirmation_kind: 'destructive_once',
      allow_session_allowed: false,
    });
  });

  it('replays artifact delivery transcript events as session artifact cards', () => {
    const next = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-artifact',
      sequence: 42,
      type: 'artifact_delivery',
      event_type: 'artifact_delivery',
      actor_type: 'system',
      role: 'system',
      content: 'artifact_delivery',
      parts: [
        {
          type: 'artifact',
          artifact_id: 'artifact-doc',
          name: 'proposal.docx',
          path: 'workspace/proposal.docx',
          preview_kind: 'office',
          source: 'workspace_write',
        },
      ],
      created_at: '2026-06-25T12:00:00Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'assistant',
      content: '',
      id: 'evt-artifact',
      timestamp: '2026-06-25T12:00:00Z',
      artifacts: [
        {
          id: 'artifact-doc',
          name: 'proposal.docx',
          path: 'workspace/proposal.docx',
          previewKind: 'office',
          source: 'workspace_write',
        },
      ],
    });
  });

  it('replays file changes transcript events as runtime events instead of artifact cards', () => {
    const next = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-file-changes',
      sequence: 43,
      type: 'file_changes',
      event_type: 'file_changes',
      actor_type: 'system',
      role: 'system',
      content: 'file_changes',
      metadata: {
        file_change_paths: ['workspace/report.md', 'workspace/scratch.md'],
        attached_artifact_paths: ['workspace/report.md'],
        rejected_artifact_paths: ['workspace/stale.md'],
      },
      created_at: '2026-06-25T12:00:01Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'event',
      content: 'file_changes',
      id: 'evt-file-changes',
      eventType: 'file_changes',
      eventTitle: 'File Changes',
      timestamp: '2026-06-25T12:00:01Z',
    });
    expect(next.messages[0].artifacts).toBeUndefined();
  });

  it('replays task notification transcript events as runtime events even when legacy role is user', () => {
    const next = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-task-notification',
      sequence: 44,
      type: 'agent_task_notification',
      event_type: 'agent_task_notification',
      actor_type: 'agent',
      role: 'user',
      content: '<task-notification><task-id>task-1</task-id></task-notification>',
      metadata: {
        message: 'Web3研究员 completed: report ready',
        status: 'completed',
        notification_source: 'a2a_delegation',
        task_id: 'task-1',
        task_type: 'a2a_delegation',
        child_session_id: 'child-session-1',
      },
      created_at: '2026-06-25T12:00:00Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'event',
      eventType: 'agent_task_notification',
      eventStatus: 'completed',
      eventNotificationSource: 'a2a_delegation',
      eventChildSessionId: 'child-session-1',
      content: 'Web3研究员 completed: report ready',
    });
    expect(next.messages[0].content).not.toContain('<task-notification>');
  });

  it('attaches artifact parts from tool result transcript events to the tool card', () => {
    const next = applyTranscriptEvent(createEmptyTranscriptReplayState(), {
      id: 'evt-tool-artifact',
      sequence: 43,
      type: 'tool_result',
      event_type: 'tool_result',
      actor_type: 'tool',
      role: 'tool_call',
      content: JSON.stringify({
        name: 'office_document_apply',
        args: { path: 'workspace/proposal.docx' },
        status: 'done',
        result: '{"ok": true}',
      }),
      parts: [
        {
          type: 'artifact',
          artifact_id: 'artifact-doc',
          name: 'proposal.docx',
          path: 'workspace/proposal.docx',
          preview_kind: 'office',
        },
      ],
      created_at: '2026-06-25T12:00:00Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'office_document_apply',
      artifacts: [
        {
          id: 'artifact-doc',
          name: 'proposal.docx',
          path: 'workspace/proposal.docx',
          previewKind: 'office',
        },
      ],
    });
  });

  it('does not duplicate artifact delivery when the assistant message already carries the artifact', () => {
    const assistantEvent = {
      id: 'evt-assistant',
      sequence: 41,
      type: 'assistant_message',
      event_type: 'assistant_message',
      actor_type: 'assistant',
      role: 'assistant',
      content: 'Proposal updated.',
      parts: [
        { type: 'text', text: 'Proposal updated.' },
        {
          type: 'artifact',
          artifact_id: 'artifact-doc',
          name: 'proposal.docx',
          path: 'workspace/proposal.docx',
          preview_kind: 'office',
        },
      ],
      created_at: '2026-06-25T12:00:00Z',
    };
    const artifactEvent = {
      ...assistantEvent,
      id: 'evt-artifact',
      sequence: 42,
      type: 'artifact_delivery',
      event_type: 'artifact_delivery',
      actor_type: 'system',
      role: 'system',
      content: 'artifact_delivery',
      parts: assistantEvent.parts.slice(1),
    };

    const withAssistant = applyTranscriptEvent(createEmptyTranscriptReplayState(), assistantEvent);
    const withArtifactDelivery = applyTranscriptEvent(withAssistant, artifactEvent);

    expect(withArtifactDelivery.messages).toHaveLength(1);
    expect(withArtifactDelivery.messages[0]).toMatchObject({
      role: 'assistant',
      content: 'Proposal updated.',
      artifacts: [{ path: 'workspace/proposal.docx' }],
    });
  });

  it('rebuilds structured tool cards from persisted tool_call history', () => {
    const message = normalizeStoredChatMessage({
      id: 'tool-1',
      role: 'tool_call',
      content: '',
      toolName: 'ask_user_question',
      toolStatus: 'done',
      toolResult: JSON.stringify({
        status: 'awaiting_user_clarification',
        blocking: true,
        questions: [
          {
            question: 'Which cadence should this employee use?',
            header: 'Cadence',
            options: [{ label: 'Weekly', description: 'Run every week' }],
          },
        ],
      }),
      created_at: '2026-06-20T12:00:00Z',
    });

    expect(message).toMatchObject({
      id: 'tool-1',
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolMeta: {
        kind: 'user_clarification',
        blocking: true,
        questions: [
          {
            question: 'Which cadence should this employee use?',
            header: 'Cadence',
            options: [{ label: 'Weekly', description: 'Run every week' }],
          },
        ],
      },
    });
  });

  it('does not append an empty assistant bubble for terminal tool-card done events', () => {
    const current = [
      {
        role: 'tool_call' as const,
        content: '',
        toolName: 'ask_user_question',
        toolStatus: 'done' as const,
        toolResult: '{"status":"awaiting_user_clarification","questions":[{"question":"Scope?","options":[{"label":"A"}]}]}',
      },
    ];

    expect(applyRuntimeDoneEvent(current, { type: 'done', content: '' })).toBe(current);
  });

  it('removes dangling thinking placeholders when a terminal tool card arrives', () => {
    const current = [
      { role: 'assistant' as const, content: '', thinking: 'Need a cadence.', _streaming: true } as any,
    ];
    const toolMessage = normalizeStoredChatMessage({
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolStatus: 'done',
      toolResult: JSON.stringify({
        status: 'awaiting_user_clarification',
        questions: [{ question: 'Cadence?', options: [{ label: 'Weekly' }] }],
      }),
    });

    expect(appendToolCallMessage(current, toolMessage)).toEqual([toolMessage]);
  });

  it('normalizes persisted system compaction events from JSON content', () => {
    const message = normalizeStoredChatMessage({
      role: 'system',
      content: JSON.stringify({
        type: 'session_compact',
        summary: 'Compacted older turns and kept the active work context.',
        original_message_count: 42,
        kept_message_count: 9,
        continuity_sections_injected: ['Current Work'],
      }),
      created_at: '2026-05-18T08:00:00Z',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      content: 'Compacted older turns and kept the active work context.',
      originalMessageCount: 42,
      keptMessageCount: 9,
      continuitySectionsInjected: ['Current Work'],
      timestamp: '2026-05-18T08:00:00Z',
    });
  });

  it('normalizes legacy assistant recovery summaries into compaction events', () => {
    const message = normalizeStoredChatMessage({
      role: 'assistant',
      content: [
        '**Primary Request and Intent:** Repair the web chat after context compression.',
        '**Tool Outcomes:** Search ran successfully.',
        '**Current Work:** Hide the recovery summary from the default chat transcript.',
        '**Recovery Context:** Raw session log available at logs/ for full detail',
      ].join('\n'),
      created_at: '2026-05-18T08:05:00Z',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      content: expect.stringContaining('Hide the recovery summary'),
      timestamp: '2026-05-18T08:05:00Z',
    });
  });

  it('keeps raw compaction summaries out of the default visible event body', () => {
    const display = getCompactionDisplayContent([
      '**Primary Request and Intent:** Repair the web chat after context compression.',
      '**Tool Outcomes:** Search ran successfully.',
      '**Current Work:** Hide the recovery summary from the default chat transcript while preserving details.',
      '**Recovery Context:** Raw session log available at logs/ for full detail',
    ].join('\n'));

    expect(display.compacted).toBe(true);
    expect(display.visible).toBe('');
    expect(display.visible).not.toContain('Recovery Context');
    expect(display.details).toContain('Recovery Context');
  });

  it('clamps composer height to the configured min and max', () => {
    expect(computeComposerHeight(20)).toBe(44);
    expect(computeComposerHeight(96)).toBe(96);
    expect(computeComposerHeight(260)).toBe(160);
  });

  it('prefers backend runtime estimates and model metadata when available', () => {
    const summary = buildRuntimeSummary({
      persistedSummary: {
        activated_tool_groups: ['web-runtime'],
        used_tools: ['search_query'],
        blocked_capabilities: [],
        compaction_count: 1,
        permission_event_count: 2,
        team_memory_hit_count: 3,
        last_tool_budget_event: {
          reason: 'pre_compaction_truncation',
          tool_name: 'web_fetch',
          created_at: '2026-04-02T10:05:00Z',
        },
        last_retry_reason: 'auth',
        last_team_memory_hit: {
          workspace_key: 'workspace-alpha',
          query: 'deploy checklist',
          matched_keys: ['deploy-playbook'],
          created_at: '2026-04-02T10:00:00Z',
        },
        model: {
          label: 'Claude Sonnet',
          provider: 'anthropic',
          name: 'claude-sonnet-4',
          context_window_tokens: 200000,
        },
        runtime: {
          connected: true,
          estimated_input_tokens: 4200,
          remaining_tokens_estimate: 195800,
        },
      },
      activeModel: {
        label: 'GPT-5.4',
        provider: 'openai',
        model: 'gpt-5.4',
        max_input_tokens: 128000,
      },
      agentPrimaryModelId: 'fallback-model',
      agentContextWindowSize: 32000,
      messages: [{ role: 'user', content: 'hello world' }],
      connected: false,
    });

    expect(summary.model).toMatchObject({
      label: 'Claude Sonnet',
      provider: 'anthropic',
      name: 'claude-sonnet-4',
      context_window_tokens: 200000,
    });
    expect(summary.runtime).toMatchObject({
      connected: true,
      estimated_input_tokens: 4200,
      remaining_tokens_estimate: 195800,
    });
    expect(summary.activated_tool_groups).toEqual(['web-runtime']);
    expect('activated_packs' in summary).toBe(false);
    expect(summary.permission_event_count).toBe(2);
    expect(summary.team_memory_hit_count).toBe(3);
    expect(summary.last_tool_budget_event).toMatchObject({
      reason: 'pre_compaction_truncation',
      tool_name: 'web_fetch',
    });
    expect(summary.last_retry_reason).toBe('auth');
    expect(summary.last_team_memory_hit).toMatchObject({
      workspace_key: 'workspace-alpha',
      matched_keys: ['deploy-playbook'],
    });
  });
});
