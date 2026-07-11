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
  it('projects durable transcript truth and refreshes the active session after a terminal event', () => {
    const harness = makeHarness({
      id: 'event-1',
      sequence: 7,
      event_type: 'run_completed',
      metadata_json: { run_id: 'run-1' },
    });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.dependencies.applyTranscriptToSession).toHaveBeenCalledWith(
      'agent-1',
      'session-1',
      expect.objectContaining({ id: 'event-1', event_type: 'run_completed' }),
      true,
    );
    expect(harness.dependencies.selectSession).toHaveBeenCalledWith({ id: 'session-1' });
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

  it('turns an expired authorization error into a visible terminal message and disables reconnect', () => {
    const harness = makeHarness({ type: 'error', message: 'Session token expired' });

    projectSessionSocketEvent(harness.context, harness.dependencies);

    expect(harness.failAuthentication).toHaveBeenCalledWith('agent-1:session-1', true);
    expect(harness.dependencies.setAgentExpired).toHaveBeenCalledWith(true);
    expect(harness.messages()).toEqual([
      expect.objectContaining({ role: 'assistant', content: '⚠️ Session token expired' }),
    ]);
  });
});
