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
import { sessionPayloadContent } from '../session-workbench/sessionEventStore';
import type { SessionSocketMessageContext } from './useSessionTransportController';

type MessageUpdater = (
  sessionId: string,
  updater: (messages: AgentChatMessage[]) => AgentChatMessage[],
) => void;

const TASK_LEDGER_MUTATION_TOOLS = new Set(['task_create', 'task_update', 'task_stop', 'track_todo']);
const RUNTIME_QUERY_EVENT_KINDS = new Set([
  'run',
  'subagent',
  'a2a_delegation',
  'a2a_receipt',
  'workflow_run',
  'workflow_step',
  'workflow_gate',
]);
const TERMINAL_RUN_LIFECYCLES = new Set(['completed', 'failed', 'cancelled']);

function terminalPhaseForRunLifecycle(lifecycle: string): RuntimePhase | null {
  if (lifecycle === 'completed') return 'done';
  if (lifecycle === 'failed') return 'failed';
  if (lifecycle === 'cancelled') return 'cancelled';
  return null;
}

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
  reconcileSessionTranscript: (agentId: string, sessionId: string) => void | Promise<unknown>;
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

function isCompatibilitySessionEvent(value: any): boolean {
  return Boolean(
    value
    && value.schema === 'hive.session_event_compatibility'
    && value.schema_version === 1
    && typeof value.event_id === 'string'
    && typeof value.sequence === 'number'
    && typeof value.legacy_event_type === 'string',
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
      content: sessionPayloadContent(d.payload),
      metadata: {
        ...(d.payload?.metadata || {}),
        session_event: d,
        item_id: d.item_id,
        item_kind: d.item_kind,
        lifecycle: d.lifecycle,
      },
    };
    applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    const itemKind = String(d.item_kind || '').toLowerCase();
    const lifecycle = String(d.lifecycle || '').toLowerCase();
    const payload = d.payload && typeof d.payload === 'object' ? d.payload : {};
    const toolName = String(payload.tool_name || payload.name || '').toLowerCase();
    if (
      itemKind === 'tool_call'
      && TASK_LEDGER_MUTATION_TOOLS.has(toolName)
      && dependencies.shouldInvalidateToolCall(key)
    ) {
      invalidateSessionRuntimeQueries(agentId, sessionId, false);
    } else if (itemKind === 'tool_result') {
      // The started tool_call can arrive before the tool mutates the Work
      // Ledger. The committed result is the authoritative refresh boundary.
      invalidateSessionRuntimeQueries(agentId, sessionId, false);
    } else if (RUNTIME_QUERY_EVENT_KINDS.has(itemKind)) {
      invalidateSessionRuntimeQueries(agentId, sessionId);
    }
    if (itemKind === 'run' && TERMINAL_RUN_LIFECYCLES.has(lifecycle)) {
      const terminalPhase = terminalPhaseForRunLifecycle(lifecycle);
      if (terminalPhase) {
        setSessionPhase(key, terminalPhase);
        if (isActiveRuntime) syncActivePhase(terminalPhase);
      }
      void fetchMySessions(true, agentId);
    }
    return;
  }

  if (isCompatibilitySessionEvent(d)) {
    const payload = d.payload && typeof d.payload === 'object' ? d.payload : {};
    const transcriptEvent: ChatTranscriptEventPayload = {
      ...d,
      id: d.event_id,
      event_type: d.legacy_event_type,
      content: sessionPayloadContent(payload),
      metadata: payload.metadata && typeof payload.metadata === 'object'
        ? payload.metadata
        : {},
    };
    applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    if (isActiveRuntime && isTerminalRealtimeChatEvent(transcriptEvent)) {
      void fetchMySessions(true, agentId);
    }
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
      // Stream frames and canonical session events ride independent
      // at-least-once channels. A terminal stream frame is the last guaranteed
      // live witness of the turn; reconcile the authoritative transcript so a
      // lost canonical tail (e.g. the final structured tool result) still
      // projects without a reload.
      if (isActiveRuntime) dependencies.reconcileSessionTranscript(agentId, sessionId);
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
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => [...messages, parseChatMsg(runtimeEvent)]);
    return;
  }

  if (d.type === 'thinking') {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => {
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
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => appendToolCallMessage(messages, toolMessage));
    return;
  }

  if (d.type === 'chunk') {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => applyStreamingChunkEvent(messages, d));
    return;
  }

  if (d.type === 'done') {
    dependencies.setChatMessagesAfterQueued(
      sessionId,
      (messages) => applyRuntimeDoneEvent(messages, d).map(parseChatMsg),
    );
    void fetchMySessions(true, agentId);
    return;
  }

  if (d.type === 'error' || d.type === 'quota_exceeded') {
    const message = d.content || d.detail || d.message || 'Request denied';
    dependencies.setTransportNotice(String(message));
    return;
  }

  if (d.type === 'trigger_notification') {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => [
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
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => [
      ...messages,
      parseChatMsg({ role: d.role, content: d.content }),
    ]);
  }
}
