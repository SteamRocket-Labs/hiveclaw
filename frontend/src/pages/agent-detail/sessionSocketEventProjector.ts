import {
  TERMINAL_RUNTIME_PHASES,
  appendToolCallMessage,
  applyRuntimeDoneEvent,
  applyStreamingChunkEvent,
  extractArtifactParts,
  getRuntimeEventMessage,
  getTerminalRunIdFromTranscriptEvent,
  getTransportNotice,
  isRuntimePhase,
  isTerminalRealtimeChatEvent,
  reduceRuntimePhase,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type RuntimePhase,
  type SessionRunState,
} from './chatRuntime';
import { normalizeToolCallResult } from './toolResultEnvelope';
import { sessionRunStateFromPayload } from './runtimeBudgetState';
import type { SessionSocketMessageContext } from './useSessionTransportController';

type MessageUpdater = (updater: (messages: AgentChatMessage[]) => AgentChatMessage[]) => void;

export interface SessionSocketProjectionDependencies {
  applyTranscriptToSession: (
    agentId: string,
    sessionId: string,
    event: ChatTranscriptEventPayload,
    isActiveRuntime: boolean,
  ) => void;
  selectSession: (session: any) => void | Promise<unknown>;
  fetchMySessions: (silent: boolean, agentId: string) => void | Promise<unknown>;
  setSessionPhase: (key: string, phase: RuntimePhase) => void;
  sessionPhaseOf: (key: string) => RuntimePhase;
  syncActivePhase: (phase: RuntimePhase) => void;
  setActiveRunState: (key: string, run: SessionRunState | null) => void;
  markActiveRunTerminal: (key: string, runId?: string | null) => void;
  invalidateSessionRuntimeQueries: (agentId: string, sessionId: string, includeActiveRun?: boolean) => void;
  shouldInvalidateToolCall: (key: string) => boolean;
  isTerminalTranscriptToolMessage: (message: AgentChatMessage | undefined) => boolean;
  normalizeToolCallMessage: (message: AgentChatMessage) => AgentChatMessage;
  parseChatMsg: (message: AgentChatMessage) => AgentChatMessage;
  setChatMessagesSessionId: (sessionId: string) => void;
  setTransportNotice: (message: string) => void;
  enqueueChatMessagesUpdate: MessageUpdater;
  setChatMessagesAfterQueued: MessageUpdater;
  setCreatedAgentId: (agentId: string) => void;
  setAgentExpired: (expired: boolean) => void;
  invalidateQuery: (queryKey: unknown[]) => void;
}

function isCanonicalSessionEvent(value: any): boolean {
  return Boolean(
    value
    && value.schema === 'hive.session_event'
    && value.schema_version === 2
    && typeof value.event_id === 'string'
    && typeof value.sequence === 'number'
    && typeof value.kind === 'string',
  );
}

export function projectSessionSocketEvent(
  context: SessionSocketMessageContext,
  dependencies: SessionSocketProjectionDependencies,
): void {
  const { data, agentId, sessionId, key, isActiveRuntime } = context;
  const d = data;
  const {
    applyTranscriptToSession,
    fetchMySessions,
    setSessionPhase,
    sessionPhaseOf,
    syncActivePhase,
    setActiveRunState,
    markActiveRunTerminal,
    invalidateSessionRuntimeQueries,
    isTerminalTranscriptToolMessage,
    normalizeToolCallMessage,
    parseChatMsg,
  } = dependencies;

  if (isCanonicalSessionEvent(d)) {
    const transcriptEvent: ChatTranscriptEventPayload = {
      ...d,
      id: d.event_id,
      event_type: d.kind,
      content: typeof d.payload?.content === 'string' ? d.payload.content : '',
      metadata: {
        ...(d.payload?.metadata || {}),
        session_event: d,
        item_id: d.item_id,
        item_kind: d.item_kind,
        lifecycle: d.lifecycle,
      },
    };
    applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    return;
  }

  if (
    typeof d === 'object'
    && d
    && (typeof d.sequence === 'number' || d.transcript_event_id || d.metadata?.transcript_event_id)
    && (d.event_type || d.type)
  ) {
    const transcriptEvent: ChatTranscriptEventPayload = {
      ...d,
      id: d.id || d.transcript_event_id || d.metadata?.transcript_event_id,
      event_type: d.event_type || d.type,
      metadata: d.metadata || d.metadata_json || {},
    };
    applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    if (isActiveRuntime && isTerminalRealtimeChatEvent(transcriptEvent)) {
      void fetchMySessions(true, agentId);
    }
    return;
  }

  if (d?.type === 'session.error') {
    const error = d.error && typeof d.error === 'object' ? d.error : {};
    const code = typeof error.code === 'string' ? error.code : 'event_store_retryable';
    dependencies.setTransportNotice(
      typeof error.message_key === 'string' ? error.message_key : `session.${code}`,
    );
    if (code === 'auth_failed') context.failAuthentication(key, false);
    return;
  }

  if (d?.type === 'session.control_receipt') {
    return;
  }

  if (d.type === 'phase') {
    if (isRuntimePhase(d.phase)) {
      setSessionPhase(key, d.phase);
      if (TERMINAL_RUNTIME_PHASES.has(d.phase)) {
        markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
      }
      if (isActiveRuntime) syncActivePhase(d.phase);
    }
    return;
  }

  if ((d.type === 'run_queued' || d.type === 'run_started') && d.run_id) {
    setActiveRunState(key, sessionRunStateFromPayload(d));
    invalidateSessionRuntimeQueries(agentId, sessionId);
    const phase = d.type === 'run_queued' ? 'queued' : 'starting';
    setSessionPhase(key, phase);
    if (isActiveRuntime) syncActivePhase(phase);
    return;
  }

  if (d.type === 'run_cancelled') {
    markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
    invalidateSessionRuntimeQueries(agentId, sessionId);
    setSessionPhase(key, 'cancelled');
    if (isActiveRuntime) {
      syncActivePhase('cancelled');
    }
    void fetchMySessions(true, agentId);
    return;
  }

  const lifecycleEvent = ['thinking', 'chunk', 'tool_call', 'done', 'error', 'quota_exceeded'].includes(d.type);
  const reducedPhase = lifecycleEvent ? reduceRuntimePhase(sessionPhaseOf(key), d) : null;
  if (lifecycleEvent && reducedPhase) {
    setSessionPhase(key, reducedPhase);
    if (d.type === 'tool_call' && dependencies.shouldInvalidateToolCall(key)) {
      invalidateSessionRuntimeQueries(agentId, sessionId, false);
    }
    if (['done', 'error', 'quota_exceeded'].includes(d.type)) {
      markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
      invalidateSessionRuntimeQueries(agentId, sessionId);
    }
  }

  if (!isActiveRuntime) {
    if (['done', 'error', 'quota_exceeded', 'trigger_notification'].includes(d.type)) {
      void fetchMySessions(true, agentId);
    }
    if (['done', 'error', 'quota_exceeded'].includes(d.type)) context.closeSessionSocket(key, true);
    return;
  }

  dependencies.setChatMessagesSessionId(sessionId);
  if (d.type === 'dreaming') return;

  const transportMessage = getTransportNotice(d);
  if (transportMessage) {
    dependencies.setTransportNotice(transportMessage);
    return;
  }
  if (reducedPhase) syncActivePhase(reducedPhase);

  const runtimeEvent = getRuntimeEventMessage({ ...d, timestamp: new Date().toISOString() });
  if (runtimeEvent) {
    dependencies.enqueueChatMessagesUpdate((messages) => [...messages, parseChatMsg(runtimeEvent)]);
    return;
  }

  if (d.type === 'thinking') {
    dependencies.enqueueChatMessagesUpdate((messages) => {
      const last = messages[messages.length - 1];
      if (last && last.role === 'assistant' && (last as any)._streaming) {
        return [...messages.slice(0, -1), { ...last, thinking: (last.thinking || '') + d.content } as any];
      }
      return [...messages, { role: 'assistant', content: '', thinking: d.content, _streaming: true } as any];
    });
    return;
  }

  if (d.type === 'tool_call') {
    const normalizedResult = normalizeToolCallResult(d.name, d.result);
    const toolMessage = normalizeToolCallMessage({
      role: 'tool_call',
      content: '',
      toolName: d.name,
      toolArgs: d.args,
      toolStatus: d.status,
      toolResult: normalizedResult.displayResult,
      toolRawResult: normalizedResult.raw,
      toolMeta: normalizedResult.toolMeta,
      artifacts: extractArtifactParts(d),
    });
    if (isTerminalTranscriptToolMessage(toolMessage)) {
      markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
      setSessionPhase(key, 'awaiting_approval');
      syncActivePhase('awaiting_approval');
    }
    if (normalizedResult.createdAgentId) dependencies.setCreatedAgentId(normalizedResult.createdAgentId);
    dependencies.enqueueChatMessagesUpdate((messages) => appendToolCallMessage(messages, toolMessage));
    return;
  }

  if (d.type === 'chunk') {
    dependencies.enqueueChatMessagesUpdate((messages) => applyStreamingChunkEvent(messages, d));
    return;
  }

  if (d.type === 'done') {
    dependencies.setChatMessagesAfterQueued((messages) => applyRuntimeDoneEvent(messages, d).map(parseChatMsg));
    void fetchMySessions(true, agentId);
    return;
  }

  if (d.type === 'error' || d.type === 'quota_exceeded') {
    const message = d.content || d.detail || d.message || 'Request denied';
    dependencies.setTransportNotice(String(message));
    return;
  }

  if (d.type === 'trigger_notification') {
    dependencies.enqueueChatMessagesUpdate((messages) => [
      ...messages,
      parseChatMsg({
        role: 'event',
        content: typeof d.content === 'string' ? d.content : '',
        eventType: 'trigger_notification',
        eventStatus: 'trigger_notification',
      }),
    ]);
    void fetchMySessions(true, agentId);
    dependencies.invalidateQuery(['autonomy-overview', agentId]);
    dependencies.invalidateQuery(['triggers', agentId]);
    return;
  }

  if (typeof d.content === 'string' && (d.role === 'assistant' || d.role === 'user')) {
    dependencies.enqueueChatMessagesUpdate((messages) => [
      ...messages,
      parseChatMsg({ role: d.role, content: d.content }),
    ]);
  }
}
