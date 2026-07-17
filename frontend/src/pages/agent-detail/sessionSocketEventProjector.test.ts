import { describe, expect, it, vi } from 'vitest';

import { projectSessionSocketEvent, type SessionSocketProjectionDependencies } from './sessionSocketEventProjector';
import type { AgentChatMessage } from './chatRuntime';
import type { SessionSocketMessageContext } from './useSessionTransportController';

function makeHarness(data: Record<string, unknown>, isActiveRuntime = true) {
  let messages: AgentChatMessage[] = [];
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
    applyTranscriptToSession: vi.fn(),
    selectSession: vi.fn(),
    fetchMySessions: vi.fn(),
    setSessionPhase: vi.fn(),
    sessionPhaseOf: vi.fn(() => 'responding' as const),
    syncActivePhase: vi.fn(),
    setActiveRunState: vi.fn(),
    markActiveRunTerminal: vi.fn(),
    invalidateSessionRuntimeQueries: vi.fn(),
    shouldInvalidateToolCall: vi.fn(() => true),
    isTerminalTranscriptToolMessage: vi.fn(() => false),
    normalizeToolCallMessage: vi.fn((message) => message),
    parseChatMsg: vi.fn((message) => message),
    setChatMessagesSessionId: vi.fn(),
    setTransportNotice: vi.fn(),
    enqueueChatMessagesUpdate: (updater) => { messages = updater(messages); },
    setChatMessagesAfterQueued: (updater) => { messages = updater(messages); },
    setCreatedAgentId: vi.fn(),
    setAgentExpired: vi.fn(),
    invalidateQuery: vi.fn(),
  };
  return {
    context,
    dependencies,
    closeSessionSocket,
    failAuthentication,
    messages: () => messages,
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
      payload: { tool_name: 'task_update', arguments: { task_id: 'task-1', status: 'completed' } },
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
